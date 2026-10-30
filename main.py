import time
import os
import json
import hmac
from collections import deque
import builtins
import threading
import requests
import pandas as pd
import numpy as np
import pyotp
from scipy.stats import norm
from scipy.optimize import brentq
from datetime import datetime
from SmartApi import SmartConnect
from flask import Flask, jsonify, render_template_string, request, Response

# --- TIMEZONE: server chahe UTC ho, saara code IST me chalega ---
os.environ["TZ"] = "Asia/Kolkata"
try:
    time.tzset()
except AttributeError:
    pass


def print(*args, **kwargs):
    kwargs['flush'] = True
    builtins.print(*args, **kwargs)


# --- CONFIG ---
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "")
CLIENT_ID = os.getenv("ANGEL_CLIENT_ID")
PIN = os.getenv("ANGEL_PIN")
API_KEY = os.getenv("ANGEL_API_KEY")
TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET")
DASH_USER = os.getenv("DASH_USER")          # optional basic auth
DASH_PASS = os.getenv("DASH_PASS")
REDIS_URL = os.getenv("REDIS_URL")          # optional (state save). Nahi ho to file use hogi
STATE_FILE = os.getenv("STATE_FILE", "/tmp/tracker_state.json")
STATE_KEY = "option_tracker_state"
MIN_API_GAP = 1.1                           # getMarketData ke beech minimum seconds
INDICES = ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"]
NEAR_STRIKES = 2     # imaginary line ke dusri taraf kitne strike (PE: upar wale strikes, CE: neeche wale strikes)
FAR_STRIKES = 10     # imaginary line ki apni taraf kitne strike (PE: neeche wale, CE: upar wale)


def window_sets(strikes_list, spot):
    """Imaginary line (spot) ke hisaab se CE / PE ke strike window (indexes). strikes_list ascending hona chahiye.
    'neeche' = strike <= spot, 'upar' = strike > spot."""
    below = [i for i, st in enumerate(strikes_list) if st <= spot]
    above = [i for i, st in enumerate(strikes_list) if st > spot]
    ce = set(below[-NEAR_STRIKES:]) | set(above[:FAR_STRIKES])
    pe = set(below[-FAR_STRIKES:]) | set(above[:NEAR_STRIKES])
    return ce, pe


debug_info = {}
INDEX_NAMES = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "SENSEX", "BANKEX", "SENSEX50"]
symbol_kind_cache = {}            # symbol -> "INDEX" / "MCX" / "STOCK"
INDEX_MAP = {
    "NIFTY": ("NSE", "99926000"),
    "BANKNIFTY": ("NSE", "99926009"),
    "FINNIFTY": ("NSE", "99926037"),
    "SENSEX": ("BSE", "99919000"),
    "BSX": ("BSE", "99919000"),
    "BSEX": ("BSE", "99919000"),
}

app = Flask(__name__)

redis_client = None
if REDIS_URL:
    try:
        import redis
        redis_client = redis.from_url(REDIS_URL)
    except Exception as e:
        print(f"⚠ Redis init failed, file use hogi: {e}")


def self_ping():
    while True:
        try:
            if RENDER_URL:
                requests.get(RENDER_URL.rstrip('/') + "/health", timeout=5)
        except Exception:
            pass
        time.sleep(180)


threading.Thread(target=self_ping, daemon=True).start()


def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


# --- GLOBAL STORAGE ---
global_run_date = ""
reset_done_date = ""
persistent_quotes_cache = {}      # "EXCH:token" -> quote
multi_symbol_runtime_states = {}
symbol_expiries_cache = {}
options_cache = {}
symbol_exch_cache = {}
chain_cache = {}
spot_source_cache = {}
full_scrip_df = pd.DataFrame()
morning_oi_dict = {}
current_active_symbol_ui = "NIFTY"
last_session_date = None
scrip_master_date = None
user_selected_expiries = {}
smart_api_global = None
symbol_states_memory = {}
last_ui_poll = [0.0]
need_relogin = [False]
last_login_ts = [0.0]
fail_streak = [0]
api_lock = threading.Lock()
_last_call = [0.0]
state_lock = threading.RLock()
sym_locks = {}
active_pending = [0]
wake_active = threading.Event()
event_log = {}
last_event_sig = {}


def get_sym_lock(sym):
    lk = sym_locks.get(sym)
    if lk is None:
        lk = sym_locks.setdefault(sym, threading.Lock())
    return lk


def log_event(sym, key, msg):
    event_log.setdefault(sym, deque(maxlen=400)).append(f"{datetime.now().strftime('%H:%M:%S')} {key} {msg}")


def get_symbol_memory(symbol, key, default=""):
    if symbol not in symbol_states_memory:
        symbol_states_memory[symbol] = {}
    return symbol_states_memory[symbol].get(key, default)


def set_symbol_memory(symbol, key, value):
    if symbol not in symbol_states_memory:
        symbol_states_memory[symbol] = {}
    symbol_states_memory[symbol][key] = str(value)


def del_symbol_memory(symbol, key):
    if symbol in symbol_states_memory:
        symbol_states_memory[symbol].pop(key, None)


def num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# --- STATE SAVE / LOAD (restart par history na jaye) ---
def save_state():
    try:
        payload = json.dumps({
            "run_date": global_run_date, "reset_done": reset_done_date,
            "states": symbol_states_memory, "morning_oi": morning_oi_dict
        })
        if redis_client:
            redis_client.set(STATE_KEY, payload, ex=172800)
        else:
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                f.write(payload)
            os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"⚠ State save error: {e}")


def load_state():
    global global_run_date, reset_done_date
    try:
        raw = None
        if redis_client:
            raw = redis_client.get(STATE_KEY)
            if isinstance(raw, bytes):
                raw = raw.decode()
        elif os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                raw = f.read()
        if not raw:
            return
        d = json.loads(raw)
        if d.get("run_date") == datetime.now().strftime("%Y-%m-%d"):
            symbol_states_memory.update(d.get("states", {}))
            morning_oi_dict.update(d.get("morning_oi", {}))
            global_run_date = d["run_date"]
            reset_done_date = d.get("reset_done", "")
            print(f"♻ State restored: {len(symbol_states_memory)} symbols")
    except Exception as e:
        print(f"⚠ State load error: {e}")


def maybe_daily_reset(now_dt):
    """Apps Script jaisa: roz 09:14:50 par saari history saaf (aur date badalne par)."""
    global global_run_date, reset_done_date
    today = now_dt.strftime("%Y-%m-%d")
    hms = now_dt.strftime("%H:%M:%S")
    if global_run_date != today:
        symbol_states_memory.clear()
        morning_oi_dict.clear()
        persistent_quotes_cache.clear()
        event_log.clear()
        last_event_sig.clear()
        global_run_date = today
        reset_done_date = today if hms >= "09:14:50" else ""
    elif hms >= "09:14:50" and reset_done_date != today:
        symbol_states_memory.clear()
        morning_oi_dict.clear()
        persistent_quotes_cache.clear()
        event_log.clear()
        last_event_sig.clear()
        reset_done_date = today


# --- BLACK-SCHOLES GREEKS (aapka original, bina badlav) ---
def calculate_iv_and_greeks(flag, S, K, t, r, ltp):
    if t <= 0 or S <= 0 or K <= 0 or ltp <= 0.05:
        delta = 1.0 if (flag == 'CE' and S > K) else (0.0 if flag == 'CE' else (-1.0 if S < K else 0.0))
        return {'iv': 0, 'delta': round(delta, 3), 'gamma': 0, 'theta': 0, 'vega': 0}

    intrinsic = max(0, S - K * np.exp(-r * t)) if flag == 'CE' else max(0, K * np.exp(-r * t) - S)
    if ltp < intrinsic: ltp = intrinsic + 0.05

    def objective_function(sigma):
        if sigma <= 0: sigma = 1e-6
        d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * t) / (sigma * np.sqrt(t))
        d2 = d1 - sigma * np.sqrt(t)
        if flag == 'CE':
            return S * norm.cdf(d1) - K * np.exp(-r * t) * norm.cdf(d2) - ltp
        else:
            return K * np.exp(-r * t) * norm.cdf(-d2) - S * norm.cdf(-d1) - ltp

    sigma = 0.15
    try:
        sigma = brentq(objective_function, 1e-6, 4.0, xtol=1e-8, maxiter=200)
    except Exception:
        sigma = 0.15

    iv_val = round(sigma * 100, 2)
    if iv_val < 1.0:
        iv_val = 1.0
    elif iv_val > 200.0:
        iv_val = 150.0

    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * t) / (sigma * np.sqrt(t))
    d2 = d1 - sigma * np.sqrt(t)
    gamma = norm.pdf(d1) / (S * sigma * np.sqrt(t)) if sigma > 0 and t > 0 else 0
    vega_val = S * norm.pdf(d1) * np.sqrt(t) / 100

    if flag == 'CE':
        delta = norm.cdf(d1)
        theta = (- (S * norm.pdf(d1) * sigma) / (2 * np.sqrt(t)) - r * K * np.exp(-r * t) * norm.cdf(d2)) / 365 if t > 0 else 0
    else:
        delta = norm.cdf(d1) - 1
        theta = (- (S * norm.pdf(d1) * sigma) / (2 * np.sqrt(t)) + r * K * np.exp(-r * t) * norm.cdf(-d2)) / 365 if t > 0 else 0

    return {'iv': iv_val, 'delta': round(delta, 3), 'gamma': round(gamma, 4), 'theta': round(theta, 2), 'vega': round(vega_val, 2)}


# --- APPS SCRIPT LOGIC (Python replica) ---
def execute_python_scenario_tracker(raw_rows=None, spot_price=0, lot_size=1, active_expiry="", symbol_name="NIFTY"):
    global multi_symbol_runtime_states
    try:
        now_dt = datetime.now()
        currentTimeStr = now_dt.strftime("%I:%M %p").lower()

        if not raw_rows: return

        strikes_list = [float(r['strike']) for r in raw_rows]
        spot_idx = 0
        if strikes_list:
            spot_idx = min(range(len(strikes_list)), key=lambda i: abs(strikes_list[i] - spot_price))

        ceVolArray, ceOiArray, peVolArray, peOiArray = [], [], [], []
        ce_idx_set, pe_idx_set = window_sets(strikes_list, spot_price)

        for idx, r_dict in enumerate(raw_rows):
            row_idx = 8 + idx
            strike = float(r_dict['strike'])
            if not strike: continue

            if idx in ce_idx_set:
                ceOiArray.append({'strike': strike, 'val': float(r_dict.get('ce_oi', 0)), 'row': row_idx})
                ceVolArray.append({'strike': strike, 'val': float(r_dict.get('ce_vol', 0)), 'row': row_idx})

            if idx in pe_idx_set:
                peVolArray.append({'strike': strike, 'val': float(r_dict.get('pe_vol', 0)), 'row': row_idx})
                peOiArray.append({'strike': strike, 'val': float(r_dict.get('pe_oi', 0)), 'row': row_idx})

        def extract_top_two(data_array):
            empty = {'highest': {'strike': 0, 'val': 0, 'row': 0}, 'second': {'strike': 0, 'val': 0, 'row': 0}, 'pct': 0}
            if not data_array: return empty
            filtered = [item for item in data_array if item['val'] > 0]
            if not filtered: return empty
            filtered.sort(key=lambda x: x['val'], reverse=True)
            highest = filtered[0]
            second = filtered[1] if len(filtered) > 1 else {'strike': highest['strike'], 'val': 0, 'row': highest['row']}
            pct = (second['val'] / highest['val']) * 100 if highest['val'] > 0 else 0
            return {'highest': highest, 'second': second, 'pct': pct}

        ceVolTop = extract_top_two(ceVolArray)
        ceOiTop = extract_top_two(ceOiArray)
        peVolTop = extract_top_two(peVolArray)
        peOiTop = extract_top_two(peOiArray)

        if not ceVolTop['highest'] or not ceOiTop['highest'] or not peVolTop['highest'] or not peOiTop['highest']: return

        debug_info[symbol_name] = {
            "time": now_dt.strftime("%H:%M:%S"), "spot": spot_price,
            "ce_window": [strikes_list[i] for i in sorted(ce_idx_set)], "pe_window": [strikes_list[i] for i in sorted(pe_idx_set)],
            "ce_vol_top2": [ceVolTop['highest']['strike'], ceVolTop['second']['strike'], round(ceVolTop['pct'], 2)],
            "ce_oi_top2": [ceOiTop['highest']['strike'], ceOiTop['second']['strike'], round(ceOiTop['pct'], 2)],
            "pe_vol_top2": [peVolTop['highest']['strike'], peVolTop['second']['strike'], round(peVolTop['pct'], 2)],
            "pe_oi_top2": [peOiTop['highest']['strike'], peOiTop['second']['strike'], round(peOiTop['pct'], 2)]}

        def detect_live_state_advanced(top_obj, is_put_side):
            if not top_obj or top_obj['highest']['val'] == 0 or top_obj['pct'] < 75.0: return "STRONG"
            if is_put_side:
                return "wtb" if top_obj['second']['strike'] < top_obj['highest']['strike'] else "wtt"
            else:
                return "wtt" if top_obj['second']['strike'] > top_obj['highest']['strike'] else "wtb"

        ceVolRawState = detect_live_state_advanced(ceVolTop, False)
        ceOiRawState = detect_live_state_advanced(ceOiTop, False)
        peVolRawState = detect_live_state_advanced(peVolTop, True)
        peOiRawState = detect_live_state_advanced(peOiTop, True)

        def get_full_status_string(top_obj, raw_state, cache_key):
            if not top_obj or not top_obj.get('highest') or top_obj['highest']['strike'] == 0: return "stable strong"
            strike = top_obj['highest']['strike']
            prev_strike_str = get_symbol_memory(symbol_name, "PREV_STRK_" + cache_key)
            prev_strike = float(prev_strike_str) if prev_strike_str else strike
            current_weak_hist = get_symbol_memory(symbol_name, "WEAK_HIST_" + cache_key, "")

            if raw_state == "STRONG":
                if current_weak_hist == "wtt": return "wtt to strong"
                if current_weak_hist == "wtb": return "wtb to strong"
                if strike != prev_strike: return "strong"
                return "stable strong"
            return raw_state

        ceVolStatusText = get_full_status_string(ceVolTop, ceVolRawState, "CE_VOL")
        ceOiStatusText = get_full_status_string(ceOiTop, ceOiRawState, "CE_OI")
        peVolStatusText = get_full_status_string(peVolTop, peVolRawState, "PE_VOL")
        peOiStatusText = get_full_status_string(peOiTop, peOiRawState, "PE_OI")

        ceOiIsStable100 = ceOiRawState == "STRONG" or "to strong" in ceOiStatusText
        ceVolIsStable100 = ceVolRawState == "STRONG" or "to strong" in ceVolStatusText
        peOiIsStable100 = peOiRawState == "STRONG" or "to strong" in peOiStatusText
        peVolIsStable100 = peVolRawState == "STRONG" or "to strong" in peVolStatusText

        # CALL (CE) SIDE WINNER ENGINE
        ceWinner = "VOL"
        if ceVolTop['highest']['strike'] == ceOiTop['highest']['strike'] and ceOiRawState == "wtb" and ceVolRawState == "wtb":
            ceWinner = "BOTH"
        elif ceVolTop['highest']['strike'] == ceOiTop['highest']['strike'] and not ceOiIsStable100 and not ceVolIsStable100 and (ceOiTop['second']['strike'] > ceOiTop['highest']['strike']) and (ceVolTop['second']['strike'] > ceVolTop['highest']['strike']):
            ceWinner = "BOTH"
        elif ceVolTop['highest']['strike'] == ceOiTop['highest']['strike'] and ceOiIsStable100 and ceVolIsStable100:
            ceWinner = "BOTH"
        elif ceVolTop['highest']['strike'] == ceOiTop['highest']['strike'] and ceOiIsStable100 and not ceVolIsStable100 and (ceVolTop['second'] and ceVolTop['second']['strike'] > ceVolTop['highest']['strike']):
            ceWinner = "OI"
        elif ceVolTop['highest']['strike'] == ceOiTop['highest']['strike'] and ceVolIsStable100 and not ceOiIsStable100 and (ceOiTop['second'] and ceOiTop['second']['strike'] > ceOiTop['highest']['strike']):
            ceWinner = "VOL"
        elif ceOiIsStable100 and not ceVolIsStable100 and (ceVolTop['second'] and ceVolTop['second']['strike'] > ceVolTop['highest']['strike']) and (ceOiTop['highest']['strike'] < ceVolTop['highest']['strike']):
            ceWinner = "OI"   # FIX: Apps Script jaisa (ceVolTop.highest)
        elif ceVolIsStable100 and not ceOiIsStable100 and (ceOiTop['second'] and ceOiTop['second']['strike'] > ceVolTop['highest']['strike']) and (ceVolTop['highest']['strike'] < ceOiTop['highest']['strike']):
            ceWinner = "VOL"
        else:
            ceOiActiveRow = ceOiTop['highest']['row']
            if ceOiTop['second'] and ceOiTop['second']['row'] > 0 and ceOiTop['pct'] >= 75.0:
                ceOiActiveRow = min(ceOiActiveRow, ceOiTop['second']['row'])
            ceVolActiveRow = ceVolTop['highest']['row']
            if ceVolTop['second'] and ceVolTop['second']['row'] > 0 and ceVolTop['pct'] >= 75.0:
                ceVolActiveRow = min(ceVolActiveRow, ceVolTop['second']['row'])
            if ceVolActiveRow != ceOiActiveRow:
                ceWinner = "VOL" if ceVolActiveRow < ceOiActiveRow else "OI"
            else:
                ceWinner = "VOL" if ceVolTop['highest']['row'] <= ceOiTop['highest']['row'] else "OI"

        # PUT (PE) SIDE WINNER ENGINE
        peWinner = "VOL"
        if peVolTop['highest']['strike'] == peOiTop['highest']['strike'] and peOiRawState == "wtt" and peVolRawState == "wtt":
            peWinner = "BOTH"
        elif peVolTop['highest']['strike'] == peOiTop['highest']['strike'] and not peOiIsStable100 and not peVolIsStable100 and (peOiTop['second']['strike'] < peOiTop['highest']['strike']) and (peVolTop['second']['strike'] < peVolTop['highest']['strike']):
            peWinner = "BOTH"
        elif peVolTop['highest']['strike'] == peOiTop['highest']['strike'] and peOiIsStable100 and peVolIsStable100:
            peWinner = "BOTH"
        elif peVolTop['highest']['strike'] == peOiTop['highest']['strike'] and peOiIsStable100 and not peVolIsStable100 and (peVolTop['second'] and peVolTop['second']['strike'] < peOiTop['highest']['strike']):
            peWinner = "OI"
        elif peVolTop['highest']['strike'] == peOiTop['highest']['strike'] and peVolIsStable100 and not peOiIsStable100 and (peOiTop['second'] and peOiTop['second']['strike'] < peOiTop['highest']['strike']):
            peWinner = "VOL"
        elif peOiIsStable100 and not peVolIsStable100 and (peVolTop['second'] and peVolTop['second']['strike'] < peVolTop['highest']['strike']) and (peOiTop['highest']['strike'] > peVolTop['highest']['strike']):
            peWinner = "OI"
        elif peVolIsStable100 and not peOiIsStable100 and (peOiTop['second'] and peOiTop['second']['strike'] < peVolTop['highest']['strike']) and (peVolTop['highest']['strike'] > peOiTop['highest']['strike']):
            peWinner = "VOL"
        else:
            peOiActiveRow = peOiTop['highest']['row']
            if peOiTop['second'] and peOiTop['second']['row'] > 0 and peOiTop['pct'] >= 75.0:
                peOiActiveRow = max(peOiActiveRow, peOiTop['second']['row'])
            peVolActiveRow = peVolTop['highest']['row']
            if peVolTop['second'] and peVolTop['second']['row'] > 0 and peVolTop['pct'] >= 75.0:
                peVolActiveRow = max(peVolActiveRow, peVolTop['second']['row'])
            if peVolActiveRow != peOiActiveRow:
                peWinner = "VOL" if peVolActiveRow > peOiActiveRow else "OI"
            else:
                peWinner = "VOL" if peVolTop['highest']['strike'] >= peOiTop['highest']['strike'] else "OI"

        if ceOiRawState in ["wtt", "wtb"]: set_symbol_memory(symbol_name, "WEAK_HIST_CE_OI", ceOiRawState)
        if ceVolRawState in ["wtt", "wtb"]: set_symbol_memory(symbol_name, "WEAK_HIST_CE_VOL", ceVolRawState)
        if peOiRawState in ["wtt", "wtb"]: set_symbol_memory(symbol_name, "WEAK_HIST_PE_OI", peOiRawState)
        if peVolRawState in ["wtt", "wtb"]: set_symbol_memory(symbol_name, "WEAK_HIST_PE_VOL", peVolRawState)

        def calculateIndependentCell(topObj, cacheKey, sideLabel, isWinner, rawState, finalStatusText):
            if not topObj or not topObj.get('highest') or topObj['highest']['strike'] == 0:
                return {'text': '<div class="winner-card-top">-----</div>', 'status': "stable strong", 'scenarioStatus': "stable strong", 'rawState': "STRONG"}

            strike = topObj['highest']['strike']
            secondStrike = strike
            if topObj.get('second') and topObj['second']['val'] > 0: secondStrike = topObj['second']['strike']

            strike_str = str(int(strike)) if strike == int(strike) else str(strike)
            second_strike_str = str(int(secondStrike)) if secondStrike == int(secondStrike) else str(secondStrike)

            # pichhle refresh ka leader strike (pehli baar = abhi wala)
            prevStrikeStr = get_symbol_memory(symbol_name, "PREV_STRK_" + cacheKey)
            prevStrike = float(prevStrikeStr) if prevStrikeStr else strike

            displayStatus = finalStatusText
            scenarioStatus = finalStatusText
            isShifting = False
            shiftType = get_symbol_memory(symbol_name, "SHIFT_TYPE_" + cacheKey, "")
            shiftFrom = num(get_symbol_memory(symbol_name, "SHIFT_FROM_" + cacheKey, 0))
            shiftStatus = get_symbol_memory(symbol_name, "SHIFT_STATUS_" + cacheKey, "") or finalStatusText   # shift bante waqt ka wtt/wtb
            shifted = get_symbol_memory(symbol_name, "IS_SHIFTED_" + cacheKey) == "true"
            shiftStatus = get_symbol_memory(symbol_name, "SHIFT_STATUS_" + cacheKey, "")
            isStrongNow = rawState == "STRONG" or finalStatusText == "stable strong" or "to strong" in finalStatusText
            reverted = False

            # 1) SHIFT TUTNE KE RULES (second strike badalne se shift NAHI tutta, par wtt/wtb ki direction palti to tutta hai)
            if shifted:
                if isStrongNow:
                    shifted = False
                    log_event(symbol_name, cacheKey, f"SHIFT CLEARED (strong): strike={strike_str} status={finalStatusText}")
                elif strike == shiftFrom:
                    shifted = False
                    reverted = True
                    log_event(symbol_name, cacheKey, f"SHIFT CLEARED (leader wapas purane strike {int(shiftFrom)} par)")
                elif finalStatusText in ("wtb", "wtt") and finalStatusText != shiftStatus:
                    shifted = False
                    log_event(symbol_name, cacheKey, f"SHIFT CLEARED (direction palti {shiftStatus} -> {finalStatusText}): strike={strike_str} second={second_strike_str}")
                elif finalStatusText in ("wtb", "wtt") and shiftStatus and finalStatusText != shiftStatus:
                    shifted = False
                    log_event(symbol_name, cacheKey, f"SHIFT CLEARED (weak direction badli: {shiftStatus} -> {finalStatusText}) strike={strike_str} second={second_strike_str}")

            # 2) NAYA SHIFT: leader strike badla aur status weak (wtt/wtb)
            if (not reverted) and strike != prevStrike and prevStrike > 0 and finalStatusText in ("wtb", "wtt"):
                shiftType = "top to bottom" if strike < prevStrike else "bottom to top"
                shiftFrom = prevStrike
                shiftStatus = finalStatusText
                shifted = True
                set_symbol_memory(symbol_name, "SHIFT_STATUS_" + cacheKey, finalStatusText)
                log_event(symbol_name, cacheKey, f"SHIFT SET {shiftType}: {int(prevStrike)} -> {strike_str} second={second_strike_str} status={finalStatusText}")

            if shifted:
                isShifting = True
                scenarioStatus = "wtb" if shiftType == "top to bottom" else "wtt"
                set_symbol_memory(symbol_name, "IS_SHIFTED_" + cacheKey, "true")
                set_symbol_memory(symbol_name, "SHIFT_TYPE_" + cacheKey, shiftType)
                set_symbol_memory(symbol_name, "SHIFT_FROM_" + cacheKey, str(shiftFrom))
                set_symbol_memory(symbol_name, "SHIFT_STATUS_" + cacheKey, shiftStatus)
            else:
                set_symbol_memory(symbol_name, "IS_SHIFTED_" + cacheKey, "false")
                scenarioStatus = finalStatusText
            set_symbol_memory(symbol_name, "PREV_STRK_" + cacheKey, str(strike))

            if not isWinner and rawState != "STRONG":
                displayStatus = "stable strong"
                scenarioStatus = "stable strong"

            # 3) TIMESTAMP: jo card par dikhta hai wo badle (strike / second strike / status / shift) to naya time lock
            if isShifting:
                sig = f"S|{shiftType}|{strike_str}|{second_strike_str}|{displayStatus}"
            elif displayStatus in ("wtb", "wtt"):
                sig = f"W|{displayStatus}|{strike_str}|{second_strike_str}"
            else:
                sig = f"N|{displayStatus}|{strike_str}"
            displayTime = get_symbol_memory(symbol_name, "TIME_" + cacheKey)
            if not displayTime or get_symbol_memory(symbol_name, "LAST_SIG_" + cacheKey, "") != sig:
                displayTime = currentTimeStr
                set_symbol_memory(symbol_name, "TIME_" + cacheKey, displayTime)
                set_symbol_memory(symbol_name, "LAST_SIG_" + cacheKey, sig)
                log_event(symbol_name, cacheKey, f"TIME LOCK {displayTime} | {sig} | pct={round(topObj['pct'], 1)} raw={rawState} winner={isWinner}")

            if isShifting:
                line1 = f"<div class='card-header-line'>{displayTime} - {sideLabel}-{strike_str}-{displayStatus}-{second_strike_str}</div>"
                line2 = f"<div><span class='yellow-pill-badge'>shifted {shiftType}</span></div>"
                outputText = f"<div class='winner-card-top'>{line1}{line2}</div>"
            else:
                if displayStatus == "stable strong":
                    line1 = f"<div class='card-header-line'>{displayTime} - {sideLabel} - {strike_str}</div>"
                    line2 = "<div><span class='yellow-pill-badge'>stable strong</span></div>"
                    outputText = f"<div class='winner-card-top'>{line1}{line2}</div>"
                elif "to strong" in displayStatus:
                    line1 = f"<div class='card-header-line'>{displayTime} - {sideLabel} - {strike_str}</div>"
                    line2 = f"<div><span class='yellow-pill-badge'>{displayStatus}</span></div>"
                    outputText = f"<div class='winner-card-top'>{line1}{line2}</div>"
                else:
                    weakDir = "weak towards top" if displayStatus == "wtt" else "weak towards bottom"
                    line1 = f"<div class='card-header-line'>{displayTime} - {sideLabel}-{strike_str}-{displayStatus}-{second_strike_str}</div>"
                    line2 = f"<div><span class='yellow-pill-badge'>{weakDir}</span></div>"
                    outputText = f"<div class='winner-card-top'>{line1}{line2}</div>"

            return {'text': outputText, 'status': displayStatus, 'scenarioStatus': scenarioStatus, 'rawState': rawState}

        ceOiResult = calculateIndependentCell(ceOiTop, "CE_OI", "Eor OI", ceWinner == "OI" or ceWinner == "BOTH", ceOiRawState, ceOiStatusText)
        ceVolResult = calculateIndependentCell(ceVolTop, "CE_VOL", "Eor VOL", ceWinner == "VOL" or ceWinner == "BOTH", ceVolRawState, ceVolStatusText)
        peVolResult = calculateIndependentCell(peVolTop, "PE_VOL", "Eos VOL", peWinner == "VOL" or peWinner == "BOTH", peVolRawState, peVolStatusText)
        peOiResult = calculateIndependentCell(peOiTop, "PE_OI", "Eos OI", peWinner == "OI" or peWinner == "BOTH", peOiRawState, peOiStatusText)

        finalCeState = ceOiResult['scenarioStatus'] if ceWinner == "OI" else ceVolResult['scenarioStatus']
        finalPeState = peOiResult['scenarioStatus'] if peWinner == "OI" else peVolResult['scenarioStatus']

        if ceWinner == "BOTH": finalCeState = ceVolResult['scenarioStatus'] if "to strong" in ceVolResult['scenarioStatus'] else ceOiResult['scenarioStatus']
        if peWinner == "BOTH": finalPeState = peVolResult['scenarioStatus'] if "to strong" in peVolResult['scenarioStatus'] else peOiResult['scenarioStatus']

        checkCe = finalCeState
        checkPe = finalPeState
        SS = "stable strong"

        # K3 SCENARIOS: bilkul Apps Script jaisa (default Neutral, "strong" alag se nahi)
        k3Text = f"{symbol_name} is Neutral"
        if checkCe == "wtt" and checkPe == "wtb": k3Text = f"{symbol_name} is 8th scenario both side Risky"
        elif checkCe == "wtb" and checkPe == "wtt": k3Text = f"{symbol_name} is 9th scenario both side Risky"
        elif checkCe == SS and checkPe == SS: k3Text = f"{symbol_name} is Neutral"
        elif checkCe == "wtb" and checkPe == SS: k3Text = f"{symbol_name} is slightly Bearish"
        elif checkCe == SS and checkPe == "wtb": k3Text = f"{symbol_name} is slightly Bearish"
        elif checkCe == "wtt" and checkPe == SS: k3Text = f"{symbol_name} is slightly bullish"
        elif checkCe == SS and checkPe == "wtt": k3Text = f"{symbol_name} is slightly bullish"
        elif checkCe == "wtt" and checkPe == "wtt": k3Text = f"{symbol_name} is Bullrun"
        elif checkCe == "wtb" and checkPe == "wtb": k3Text = f"{symbol_name} is bloodbath"
        elif checkCe == "wtb" and checkPe == "wtt to strong": k3Text = f"{symbol_name} is confirm Bloodbath"
        elif checkCe == "wtt to strong" and checkPe == "wtb": k3Text = f"{symbol_name} is confirm Bloodbath"
        elif checkCe == "wtt" and checkPe == "wtt to strong": k3Text = f"{symbol_name} is slightly bullish (Both side trade)"
        elif checkCe == "wtt to strong" and checkPe == "wtt": k3Text = f"{symbol_name} is slightly bullish (Both side trade)"
        elif checkCe == "wtt" and checkPe == "wtb to strong": k3Text = f"{symbol_name} is confirm Bull Run"
        elif checkCe == "wtb to strong" and checkPe == "wtt": k3Text = f"{symbol_name} is confirm Bull Run"
        elif checkCe == "wtb to strong" and checkPe == "wtb": k3Text = f"{symbol_name} is slightly Bearish (Both side trade)"
        elif checkCe == "wtb" and checkPe == "wtb to strong": k3Text = f"{symbol_name} is slightly Bearish (both side trade)"
        elif checkCe == "wtb to strong" and checkPe == SS: k3Text = f"{symbol_name} is slightly Bullish"
        elif checkCe == SS and checkPe == "wtb to strong": k3Text = f"{symbol_name} is slightly Bullish"
        elif checkCe == "wtt to strong" and checkPe == SS: k3Text = f"{symbol_name} is slightly Bearish"
        elif checkCe == SS and checkPe == "wtt to strong": k3Text = f"{symbol_name} is slightly Bearish"
        elif checkCe == "wtt to strong" and checkPe == "wtb to strong": k3Text = f"{symbol_name} is 8th scenario both side Risky trade"
        elif checkCe == "wtb to strong" and checkPe == "wtt to strong": k3Text = f"{symbol_name} is 9th scenario both side Risky trade"
        elif checkCe == "wtb to strong" and checkPe == "wtb to strong": k3Text = f"{symbol_name} is confirm Bull Run"
        elif checkCe == "wtt to strong" and checkPe == "wtt to strong": k3Text = f"{symbol_name} is confirm bloodbath"

        dash = '<div class="winner-card-top">-----</div>'
        finalD5, finalH5, finalQ5, finalU5 = "", "", "", ""

        if ceWinner == "BOTH":
            finalD5, finalH5 = ceOiResult['text'], ceVolResult['text']
        elif ceWinner == "VOL":
            finalD5, finalH5 = dash, ceVolResult['text']
        elif ceWinner == "OI":
            finalD5, finalH5 = ceOiResult['text'], dash

        if peWinner == "BOTH":
            finalU5, finalQ5 = peOiResult['text'], peVolResult['text']
        elif peWinner == "VOL":
            finalQ5, finalU5 = peVolResult['text'], dash
        elif peWinner == "OI":
            finalQ5, finalU5 = dash, peOiResult['text']

        if symbol_name not in multi_symbol_runtime_states: multi_symbol_runtime_states[symbol_name] = {}
        vault = multi_symbol_runtime_states[symbol_name]

        c_oi_1, c_oi_2 = ceOiTop['highest']['strike'], ceOiTop['second']['strike']
        c_vol_1, c_vol_2 = ceVolTop['highest']['strike'], ceVolTop['second']['strike']
        p_vol_1, p_vol_2 = peVolTop['highest']['strike'], peVolTop['second']['strike']
        p_oi_1, p_oi_2 = peOiTop['highest']['strike'], peOiTop['second']['strike']

        max_ce_oi = ceOiTop['highest']['val'] or 1
        max_ce_vol = ceVolTop['highest']['val'] or 1
        max_pe_vol = peVolTop['highest']['val'] or 1
        max_pe_oi = peOiTop['highest']['val'] or 1

        # imaginary line se 2 strike se door wale strike: CE me line ke upar (kam strike), PE me line ke neeche (zyada strike)
        ce_out_set = {i for i, stv in enumerate(strikes_list) if stv <= spot_price and i not in ce_idx_set}
        pe_out_set = {i for i, stv in enumerate(strikes_list) if stv > spot_price and i not in pe_idx_set}

        grid_rows_built = []
        spot_inserted = False
        spot_insert_idx = spot_idx
        if strikes_list and spot_price > strikes_list[spot_insert_idx] and spot_insert_idx < len(strikes_list) - 1:
            spot_insert_idx += 1

        def build_badge_payload(top_obj, raw_state):
            if not top_obj or top_obj['highest']['strike'] == 0: return {}
            s_type = raw_state.upper() if raw_state in ["wtt", "wtb"] else "STRONG"
            return {"state": s_type, "pct": round(top_obj['pct'], 2), "show_badge": True}

        def spot_row():
            return {
                'is_spot_row': True, 'spot_price': round(spot_price, 2),
                'ce_oi_badge': build_badge_payload(ceOiTop, ceOiRawState),
                'ce_vol_badge': build_badge_payload(ceVolTop, ceVolRawState),
                'pe_vol_badge': build_badge_payload(peVolTop, peVolRawState),
                'pe_oi_badge': build_badge_payload(peOiTop, peOiRawState)
            }

        for idx, r_dict in enumerate(raw_rows):
            if not spot_inserted and idx == spot_insert_idx:
                grid_rows_built.append(spot_row())
                spot_inserted = True

            st = r_dict['strike']
            r_copy = r_dict.copy()
            r_copy['strike_display'] = int(st) if st == int(st) else st
            r_copy['ce_oi_pct'] = round((r_dict.get('ce_oi', 0) / max_ce_oi) * 100, 2)
            r_copy['ce_vol_pct'] = round((r_dict.get('ce_vol', 0) / max_ce_vol) * 100, 2)
            r_copy['pe_vol_pct'] = round((r_dict.get('pe_vol', 0) / max_pe_vol) * 100, 2)
            r_copy['pe_oi_pct'] = round((r_dict.get('pe_oi', 0) / max_pe_oi) * 100, 2)

            r_copy['ce_oi_color'] = 'bg-100' if st == c_oi_1 else ('bg-75' if st == c_oi_2 and ceOiTop['pct'] >= 75.0 else '')
            r_copy['ce_vol_color'] = 'bg-100' if st == c_vol_1 else ('bg-75' if st == c_vol_2 and ceVolTop['pct'] >= 75.0 else '')
            r_copy['pe_vol_color'] = 'bg-100' if st == p_vol_1 else ('bg-75' if st == p_vol_2 and peVolTop['pct'] >= 75.0 else '')
            r_copy['pe_oi_color'] = 'bg-100' if st == p_oi_1 else ('bg-75' if st == p_oi_2 and peOiTop['pct'] >= 75.0 else '')
            # window se bahar gaye strike par 75% ya zyada ho to dark-grey highlight (100% / 75% highlight ki tarah)
            for col in ('ce_oi', 'ce_vol'):
                if not r_copy[col + '_color'] and idx in ce_out_set and r_copy[col + '_pct'] >= 75.0:
                    r_copy[col + '_color'] = 'bg-out'
            for col in ('pe_vol', 'pe_oi'):
                if not r_copy[col + '_color'] and idx in pe_out_set and r_copy[col + '_pct'] >= 75.0:
                    r_copy[col + '_color'] = 'bg-out'
            grid_rows_built.append(r_copy)

        if not spot_inserted:
            grid_rows_built.append(spot_row())

        vault["D5_TEXT"] = finalD5
        vault["H5_TEXT"] = finalH5
        vault["U5_TEXT"] = finalU5
        vault["Q5_TEXT"] = finalQ5
        vault["K3_TEXT"] = k3Text
        vault["lot_size"] = lot_size
        vault["spot_price"] = round(spot_price, 2)
        vault["active_expiry"] = active_expiry
        vault["grid_rows"] = grid_rows_built
    except Exception as e:
        print(f"❌ Scenario Tracker Error ({symbol_name}): {e}")


# --- MARKET DATA ENGINE ---
api_stats = {"req": 0, "fail": 0, "lat": 0.0, "pause_until": 0.0, "backoff": 5.0}


def rate_limited_market_data(smart_api, req, priority=False, retries=2):
    """Saari getMarketData calls yahin se: 1.1s gap, retry, 'access rate' error par backoff."""
    for attempt in range(retries):
        res = None
        while time.time() < api_stats["pause_until"]:      # Angel ne rok diya to sab ruke
            time.sleep(0.2)
        if not priority:                      # background calls screen wale symbol ko aage jaane dete hain
            while active_pending[0] > 0:
                time.sleep(0.05)
        with api_lock:
            wait = MIN_API_GAP - (time.time() - _last_call[0])
            if wait > 0:
                time.sleep(wait)
            _last_call[0] = time.time()
            try:
                t0 = time.time()
                res = smart_api.getMarketData("FULL", req)
                dt = time.time() - t0
                api_stats["req"] += 1
                api_stats["lat"] += dt
                if dt > 3:
                    print(f"⏱ slow getMarketData: {dt:.1f}s")
            except Exception as e:
                api_stats["req"] += 1
                api_stats["fail"] += 1
                print(f"⚠ getMarketData exception: {e}")
                if "access denied" in str(e).lower() or "exceeding access rate" in str(e).lower():
                    api_stats["pause_until"] = time.time() + api_stats["backoff"]
                    print(f"⛔ Angel rate limit: {api_stats['backoff']:.0f}s ruk raha hu")
                    api_stats["backoff"] = min(api_stats["backoff"] * 2, 60.0)
        if res and res.get('status') and res.get('data') is not None:
            fail_streak[0] = 0
            api_stats["backoff"] = 5.0
            d = res['data']
            if d.get('unfetched'):
                print(f"⚠ unfetched tokens: {len(d['unfetched'])}")
            return d.get('fetched') or []
        fail_streak[0] += 1
        msg = str(res)[:200]
        print(f"⚠ getMarketData failed (try {attempt + 1}): {msg}")
        low = msg.lower()
        if any(k in low for k in ("invalid token", "token expired", "session", "unauthor")) or fail_streak[0] >= 12:
            if time.time() - last_login_ts[0] > 300:
                need_relogin[0] = True
            fail_streak[0] = 0
        time.sleep(1.0 * (attempt + 1))
    return []


def fetch_quotes(smart_api, pairs, priority=False):
    """pairs = [(exch, token)]. Max 50 tokens per request. Returns {(exch, token): item}"""
    out = {}
    for i in range(0, len(pairs), 50):
        chunk = pairs[i:i + 50]
        req = {}
        for e, t in chunk:
            req.setdefault(e, []).append(t)
        default_exch = chunk[0][0] if len(req) == 1 else ""
        for item in rate_limited_market_data(smart_api, req, priority):
            e = str(item.get('exchange') or default_exch).strip()
            t = str(item.get('symbolToken', item.get('token', ''))).strip()
            out[(e, t)] = item
    return out


def get_item(quotes, e, t):
    return quotes.get((e, t)) or quotes.get(("", t))


def spot_from(quotes, src):
    it = get_item(quotes, src[0], src[1])
    return num(it.get('ltp')) if it else 0.0


def nearest_idx(strikes, spot):
    return min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))


def build_chain(sym, expiry_str):
    key = (sym, expiry_str)
    if key in chain_cache:
        return chain_cache[key]
    opts = options_cache.get(sym)
    if opts is None:
        return None
    exp_dt = pd.Timestamp(datetime.strptime(expiry_str, '%d%b%Y'))
    sub = opts[opts['parsed_expiry'] == exp_dt]
    if sub.empty:
        return None
    typ = sub['symbol'].astype(str).str.upper().str[-2:]
    tokmap = {}
    for st, tp, tok, ex in zip(sub['strike'], typ, sub['token'], sub['exch_seg']):
        if tp in ('CE', 'PE'):
            tokmap.setdefault((float(st), tp), (str(ex).strip(), str(tok).strip()))
    strikes = sorted({k[0] for k in tokmap})
    if not strikes:
        return None
    lot = 1
    if 'lotsize' in sub.columns:
        lot = int(num(sub['lotsize'].iloc[0], 1)) or 1
    chain = {'expiry_dt': exp_dt, 'tokmap': tokmap, 'strikes': strikes, 'lot': lot}
    chain_cache[key] = chain
    return chain


def resolve_spot_source(sym, expiry_dt):
    """(exchange, token) of underlying: index / MCX near future / NSE equity."""
    key = (sym, str(expiry_dt))
    if key in spot_source_cache:
        return spot_source_cache[key]
    src = None
    try:
        if sym in INDEX_MAP:
            src = INDEX_MAP[sym]
        else:
            df = full_scrip_df
            exch = symbol_exch_cache.get(sym, "")
            if exch == 'MCX':
                m = df[(df['name_up'] == sym) & (df['exch_seg'] == 'MCX') & (df['strike'] <= 0)]
                if 'instrumenttype' in m.columns:
                    m = m[m['instrumenttype'].astype(str).str.upper().str.startswith('FUT')]
                m = m.copy()
                m['pe'] = pd.to_datetime(m['expiry'], format='mixed', errors='coerce')
                m = m[m['pe'].notna()].sort_values('pe')
                cand = m[m['pe'] >= expiry_dt]
                if cand.empty:
                    cand = m[m['pe'] >= pd.Timestamp(datetime.now().date())]
                if not cand.empty:
                    src = ('MCX', str(cand.iloc[0]['token']).strip())
            elif exch == 'NFO':
                eq = df[(df['name_up'] == sym) & (df['exch_seg'] == 'NSE')]
                if not eq.empty:
                    pref = eq[eq['symbol'].astype(str).str.upper().str.endswith('-EQ')]
                    eq = pref if not pref.empty else eq
                    src = ('NSE', str(eq['token'].iloc[0]).strip())
    except Exception as e:
        print(f"⚠ Spot source error ({sym}): {e}")
    if src:
        spot_source_cache[key] = src
    return src


def option_quote(tokmap, st, tp, after_open):
    zero = {'ltp': 0, 'volume': 0, 'oi': 0, 'oi_chg': 0, 'net_change': 0}
    pair = tokmap.get((st, tp))
    if not pair:
        return zero
    key = f"{pair[0]}:{pair[1]}"
    q = persistent_quotes_cache.get(key)
    if not q:
        return zero
    if after_open and key not in morning_oi_dict and q['oi'] > 0:
        morning_oi_dict[key] = q['oi']
    base_oi = morning_oi_dict.get(key, q['oi'])
    calc_oi_chg = q['oi'] - base_oi if q['oi'] > 0 else q.get('oi_chg', 0)
    return {'ltp': q['ltp'], 'volume': q['volume'], 'oi': q['oi'], 'oi_chg': calc_oi_chg, 'net_change': q['net_change']}


def fetch_symbol_data(smart_api, sym, mode="light", priority=False):
    """mode: light = tracker tokens, bina greeks (background symbols)
             core  = tracker tokens + greeks (screen wala symbol, har ~3s)
             extra = baaki strikes ke tokens (screen wala symbol, har 3rd cycle)
             full  = poori chain (screen par symbol pehli baar khulne par)"""
    light = (mode == "light")
    locked = False
    try:
        now_dt = datetime.now()
        with state_lock:
            maybe_daily_reset(now_dt)

        expiry_list = symbol_expiries_cache.get(sym) or []
        if not expiry_list or sym not in options_cache:
            return
        chosen = user_selected_expiries.get(sym, expiry_list[0])
        current_expiry = chosen if chosen in expiry_list else expiry_list[0]
        chain = build_chain(sym, current_expiry)
        if not chain:
            return
        spot_src = resolve_spot_source(sym, chain['expiry_dt'])
        if not spot_src:
            return

        tokmap, all_strikes = chain['tokmap'], chain['strikes']
        spot_ref = num(multi_symbol_runtime_states.get(sym, {}).get('spot_price'))
        if spot_ref <= 0:   # pehli baar: spot alag se (sirf ek baar)
            spot_ref = spot_from(fetch_quotes(smart_api, [spot_src], priority), spot_src)
            if spot_ref <= 0:
                return

        closest = nearest_idx(all_strikes, spot_ref)
        lo, hi = max(0, closest - 18), min(len(all_strikes), closest + 19)
        pairs = [spot_src]
        for pos in range(lo, hi):
            st = all_strikes[pos]
            core_ce = (closest - 5) <= pos <= (closest + 14)     # tracker window + padding
            core_pe = (closest - 14) <= pos <= (closest + 5)
            if mode == "full":
                need_ce = need_pe = True
            elif mode == "extra":
                need_ce, need_pe = (not core_ce), (not core_pe)
            else:
                need_ce, need_pe = core_ce, core_pe
            if need_ce and (st, 'CE') in tokmap: pairs.append(tokmap[(st, 'CE')])
            if need_pe and (st, 'PE') in tokmap: pairs.append(tokmap[(st, 'PE')])

        quotes = fetch_quotes(smart_api, pairs, priority)
        spot_price = spot_from(quotes, spot_src)
        if spot_price <= 0:
            return
        if mode != "extra":
            nc = nearest_idx(all_strikes, spot_price)
            win = all_strikes[max(0, nc - 18):min(len(all_strikes), nc + 19)]
            ce_set, pe_set = window_sets(win, spot_price)
            have = set(pairs)
            topup = []
            for i in ce_set:
                pr = tokmap.get((win[i], 'CE'))
                if pr and pr not in have: topup.append(pr)
            for i in pe_set:
                pr = tokmap.get((win[i], 'PE'))
                if pr and pr not in have: topup.append(pr)
            if topup:     # spot kaafi hil gaya tha: window ke naye strikes ka taaza data
                quotes.update(fetch_quotes(smart_api, topup, priority))
                pairs.extend(topup)
        state_lock.acquire()
        locked = True

        missing = 0
        for e, t in pairs[1:]:
            item = get_item(quotes, e, t)
            if not item:
                missing += 1
                continue
            key = f"{e}:{t}"
            new_ltp = num(item.get('ltp'))
            new_vol = num(item.get('tradeVolume', item.get('volume', 0)))
            new_oi = num(item.get('opnInterest', item.get('oi', 0)))
            new_net = num(item.get('netChange', item.get('change', 0)))
            new_oi_chg = num(item.get('opnInterestChange', item.get('oiChange', 0)))
            prev = persistent_quotes_cache.get(key, {})
            persistent_quotes_cache[key] = {
                'ltp': new_ltp if new_ltp > 0 else prev.get('ltp', 0.0),
                'volume': new_vol if new_vol > 0 else prev.get('volume', 0.0),
                'oi': new_oi if new_oi > 0 else prev.get('oi', 0.0),
                'net_change': new_net if new_net != 0 else prev.get('net_change', 0.0),
                'oi_chg': new_oi_chg if new_oi_chg != 0 else prev.get('oi_chg', 0.0),
            }
        if missing and not light:
            print(f"⚠ {sym}: {missing}/{len(pairs) - 1} tokens ka quote nahi aaya (purani value dikh rahi hai)")

        closest = nearest_idx(all_strikes, spot_price)
        strikes = all_strikes[max(0, closest - 18):min(len(all_strikes), closest + 19)]
        after_open = (now_dt.hour, now_dt.minute) >= (9, 15)

        expiry_dt_val = chain['expiry_dt']
        t = max((expiry_dt_val.replace(hour=15, minute=30) - now_dt).total_seconds() / (365.0 * 24 * 3600), 0.0001)

        raw_grid_payload = []
        for st in strikes:
            ce_q = option_quote(tokmap, st, 'CE', after_open)
            pe_q = option_quote(tokmap, st, 'PE', after_open)
            ce_g = calculate_iv_and_greeks('CE', spot_price, st, t, 0.07, ce_q['ltp'])
            pe_g = calculate_iv_and_greeks('PE', spot_price, st, t, 0.07, pe_q['ltp'])

            raw_grid_payload.append({
                'strike': st, 'ce_oi': ce_q['oi'], 'ce_vol': ce_q['volume'], 'pe_vol': pe_q['volume'], 'pe_oi': pe_q['oi'],
                'ce_vega': ce_g['vega'], 'ce_theta': ce_g['theta'], 'ce_gamma': ce_g['gamma'], 'ce_delta': ce_g['delta'], 'ce_iv': ce_g['iv'] if ce_g['iv'] > 1 else 0,
                'ce_oi_chg': ce_q['oi_chg'], 'ce_change': ce_q['net_change'], 'ce_ltp': ce_q['ltp'],
                'pe_ltp': pe_q['ltp'], 'pe_change': pe_q['net_change'], 'pe_oi_chg': pe_q['oi_chg'],
                'pe_iv': pe_g['iv'] if pe_g['iv'] > 1 else 0, 'pe_delta': pe_g['delta'], 'pe_gamma': pe_g['gamma'], 'pe_theta': pe_g['theta'], 'pe_vega': pe_g['vega'],
                'is_itm_ce': bool(st < spot_price), 'is_itm_pe': bool(st > spot_price)
            })

        execute_python_scenario_tracker(raw_rows=raw_grid_payload, spot_price=spot_price, lot_size=chain['lot'], active_expiry=current_expiry, symbol_name=sym)
        v = multi_symbol_runtime_states.get(sym)
        if v is not None:
            if mode in ("full", "extra"):
                v['extra_ts'] = time.time()
                v['extra_exp'] = current_expiry
            if mode != "extra":
                v['fetched_ts'] = time.time()
                v['updated_at'] = datetime.now().strftime("%H:%M:%S")
    except Exception as e:
        print(f"❌ fetch_symbol_data Exception ({sym}): {e}")
    finally:
        if locked:
            state_lock.release()


# --- SCRIP MASTER ---
def initialize_scrip_master():
    global full_scrip_df, options_cache, symbol_expiries_cache, symbol_exch_cache, symbol_kind_cache, scrip_master_date
    try:
        url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
        resp = requests.get(url, timeout=120)
        if resp.status_code != 200:
            print(f"⚠ Scrip master HTTP {resp.status_code}")
            return False
        df = pd.DataFrame(resp.json())
        if 'symboltoken' in df.columns and 'token' not in df.columns: df['token'] = df['symboltoken']
        if 'tradingsymbol' in df.columns and 'symbol' not in df.columns: df['symbol'] = df['tradingsymbol']
        keep = [c for c in ['token', 'symbol', 'name', 'expiry', 'strike', 'lotsize', 'instrumenttype', 'exch_seg'] if c in df.columns]
        df = df[keep].copy()
        df['strike'] = pd.to_numeric(df['strike'], errors='coerce').fillna(0) / 100.0
        df['name_up'] = df['name'].astype(str).str.upper().str.strip()

        fno = df[(df['strike'] > 0) & (df['exch_seg'].isin(['NFO', 'MCX', 'BFO', 'BSE']))].copy()
        fno['parsed_expiry'] = pd.to_datetime(fno['expiry'], format='mixed', errors='coerce')

        today_dt = pd.Timestamp(datetime.now().date())
        new_opts, new_exp, new_exch, new_kind = {}, {}, {}, {}
        for name, group in fno.groupby('name_up'):
            clean_name = name
            if clean_name in ['BSX', 'BSEX']: clean_name = 'SENSEX'
            new_opts[clean_name] = group
            new_exch[clean_name] = str(group['exch_seg'].iloc[0])
            itype = str(group['instrumenttype'].iloc[0]).upper() if 'instrumenttype' in group.columns else ""
            new_kind[clean_name] = "MCX" if new_exch[clean_name] == "MCX" else ("INDEX" if (itype == "OPTIDX" or clean_name in INDEX_NAMES) else "STOCK")
            valid = group[group['parsed_expiry'] >= today_dt].sort_values(by='parsed_expiry')
            uniq = []
            for exp_dt in valid['parsed_expiry'].unique():
                s = pd.Timestamp(exp_dt).strftime('%d%b%Y').upper()
                if s not in uniq: uniq.append(s)
            new_exp[clean_name] = uniq

        if not new_opts:
            print("⚠ Scrip master khali aaya")
            return False
        full_scrip_df = df
        options_cache = new_opts
        symbol_expiries_cache = new_exp
        symbol_exch_cache = new_exch
        symbol_kind_cache = new_kind
        chain_cache.clear()
        spot_source_cache.clear()
        scrip_master_date = datetime.now().strftime("%Y-%m-%d")
        print(f"📚 Scrip master loaded: {len(new_opts)} symbols")
        return True
    except Exception as e:
        print(f"⚠ Scrip master error: {e}")
        return False


# --- HTML TEMPLATE ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Arush LTP Calculator</title>
    <style>
        * { box-sizing: border-box; }
        body { font-family: 'Segoe UI', Arial, sans-serif; background: #ffffff; color: #000000; margin: 0; padding: 8px; }
        .header { display: flex; justify-content: space-between; align-items: center; background: #f1f3f5; padding: 8px 12px; border-radius: 6px; border: 1px solid #ced4da; }
        .controls-group { display: flex; gap: 10px; align-items: center; }
        .lot-size-badge { background: #1c7ed6; color: #ffffff; padding: 5px 10px; border-radius: 4px; font-weight: bold; font-size: 13px; }
        .upd-badge { background: #2b8a3e; }
        .upd-stale { background: #c92a2a; }
        select { padding: 6px; font-size: 14px; background: #ffffff; border: 1px solid #ced4da; border-radius: 4px; }

        .k3-banner { background: #e6fcf5; padding: 8px; margin-top: 6px; text-align: center; font-size: 14px; font-weight: bold; border-radius: 5px; color: #0ca678; border: 1px solid #20c997; }

        .table-container { margin-top: 6px; overflow-anchor: none; max-height: 780px; overflow-y: auto; overflow-x: auto; border: 1px solid #dee2e6; border-radius: 6px; position: relative; -webkit-overflow-scrolling: touch; }

        table { width: max-content; min-width: 100%; border-collapse: collapse; border-spacing: 0; background: #ffffff; font-size: 11px; text-align: center; }

        th { padding: 4px 5px; white-space: nowrap; color: #212529; font-weight: bold; }

        .th-ce { background: #c8e6c9 !important; color: #1b5e20 !important; }
        .th-pe { background: #ffcdd2 !important; color: #b71c1c !important; }

        .bg-call-side { background: #81c784 !important; border: none !important; box-shadow: inset -2px 0 0 0 #81c784, inset 2px 0 0 0 #81c784, inset 0 -2px 0 0 #81c784, inset 0 2px 0 0 #81c784; transform: translateZ(0); }
        .bg-put-side { background: #e57373 !important; border: none !important; box-shadow: inset -2px 0 0 0 #e57373, inset 2px 0 0 0 #e57373, inset 0 -2px 0 0 #e57373, inset 0 2px 0 0 #e57373; transform: translateZ(0); }

        td { border: none; border-bottom: 1px solid #f1f3f5; padding: 4px 5px; white-space: nowrap; position: relative; }

        #row-winners th { position: sticky; top: 0; z-index: 25; padding: 4px; height: 50px; vertical-align: middle; background-clip: padding-box; }

        #row-columns th {
            position: sticky;
            top: 50px;
            z-index: 24;
            border-bottom: 3px solid #000000 !important;
            box-shadow: inset 0 -3px 0 0 #000000, 0 3px 0 0 #000000 !important;
            background-clip: padding-box;
        }

        .strike-col { background: #ff922b !important; color: #ffffff !important; font-weight: bold; font-size: 11px; border: 1px solid #dee2e6 !important; min-width: 90px !important; width: 90px !important; }
        #row-winners th.strike-col { top: 0; z-index: 27; background: #ff922b !important; color: #ffffff !important; border: 1px solid #dee2e6 !important; box-shadow: none !important; min-width: 90px !important; width: 90px !important; }

        #row-columns th.strike-col {
            top: 50px;
            z-index: 26;
            background: #ff922b !important;
            color: #ffffff !important;
            border-bottom: 3px solid #000000 !important;
            box-shadow: inset 0 -3px 0 0 #000000, 0 3px 0 0 #000000 !important;
            min-width: 90px !important;
            width: 90px !important;
        }

        .ce-oi-col, .ce-vol-col, .pe-vol-col, .pe-oi-col { min-width: 80px !important; width: 80px !important; }

        .winner-card-top { background: #ffffff !important; border: 1px solid #ced4da; border-radius: 4px; padding: 4px 6px; text-align: center; font-size: 10px; font-weight: bold; color: #000000; box-shadow: 0 1px 2px rgba(0,0,0,0.05); width: 100%; height: 42px; display: flex; flex-direction: column; justify-content: center; align-items: center; margin: 0 auto; }

        .yellow-pill-badge {
            background-color: #ffd43b !important;
            color: #000000 !important;
            padding: 2px 10px !important;
            border-radius: 12px !important;
            font-size: 10px !important;
            font-weight: bold !important;
            display: inline-block !important;
            margin-top: 2px !important;
            box-shadow: 0 1px 2px rgba(0,0,0,0.1) !important;
        }

        .spot-row td { background: #ffffff !important; padding: 0px !important; height: 2px; border: none !important; }
        .spot-row td::after { content: ""; position: absolute; left: 0; right: 0; top: 50%; height: 3px; background-color: #f03e3e; z-index: 2; transform: translateY(-50%); }
        .spot-row td.spot-price-cell::after { display: none !important; }
        .spot-price-cell { background: #ffffff !important; text-align: center; z-index: 4; padding: 2px 2px !important; border-left: 2px solid #dee2e6 !important; border-right: 2px solid #dee2e6 !important; border-top: 1px solid #dee2e6 !important; border-bottom: 1px solid #dee2e6 !important; }
        .spot-tag-red { background: #e53935; color: #ffffff; font-size: 8px; font-weight: 800; padding: 1px 5px; border-radius: 8px; display: inline-block; margin-bottom: 1px; }
        .spot-value-green { color: #2e7d32; font-weight: 800; font-size: 12px; display: block; }
        .spot-badges-wrapper { position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%); z-index: 8; display: flex; align-items: center; justify-content: center; gap: 2px; pointer-events: none; width: max-content; }
        .state-badge-pill { background: #ffffff; border: 1.5px solid #333333; border-radius: 10px; padding: 1px 6px; display: inline-flex; align-items: center; gap: 3px; font-size: 10px; font-weight: 700; white-space: nowrap; }
        .state-wtt { color: #2e7d32; border-color: #2e7d32; }
        .state-wtb { color: #c62828; border-color: #c62828; }
        .state-strong { color: #2e7d32; border-color: #333333; }
        .itm-ce { background: rgba(240, 62, 62, 0.06); }
        .itm-pe { background: rgba(43, 138, 62, 0.06); }
        .ce-oi-col.bg-100 { background: #ff8787 !important; color: #000000 !important; font-weight: bold; }
        .ce-vol-col.bg-100 { background: #a5d8ff !important; color: #000000 !important; font-weight: bold; }
        .pe-vol-col.bg-100 { background: #a5d8ff !important; color: #000000 !important; font-weight: bold; }
        .pe-oi-col.bg-100 { background: #63e6be !important; color: #000000 !important; font-weight: bold; }
        .bg-75 { background: #ffff00 !important; color: #000000 !important; font-weight: bold; }
        .bg-out { background: #5b636b !important; color: #ffffff !important; font-weight: bold; }
        .bg-out .pct-sub { color: #ffffff; }
        .pct-sub { font-size: 9px; font-weight: bold; display: block; color: #000000; margin-top: 1px; }

        .chg-pos { color: #1c7ed6 !important; font-weight: bold; }
        .chg-neg { color: #c92a2a !important; font-weight: bold; }
    </style>
</head>
<body>
    <div class="header">
        <h2>⚡ Arush LTP Calculator</h2>
        <div class="controls-group">
            <button class="lot-size-badge" id="centerBtn" onclick="centerNow()" style="border:0;cursor:pointer;">⌖ Center</button>
            <div class="lot-size-badge upd-badge" id="updDisplay">Updated: -</div>
            <div class="lot-size-badge" id="lotSizeDisplay">Lot Size: -</div>
            <select id="symbolSelect" onchange="onSymbolChange()"></select>
            <select id="expirySelect" onchange="updateSelectedSymbolSheet()"></select>
        </div>
    </div>
    <div class="k3-banner" id="k3Display">Loading Framework...</div>

    <div class="table-container" id="tableContainer">
        <table id="optionChainTable">
            <thead>
                <tr id="row-winners">
                    <th colspan="5" class="bg-call-side" id="cell-d5"><div class="winner-card-top">-----</div></th>
                    <th class="bg-call-side"></th>
                    <th colspan="3" class="bg-call-side" id="cell-h5"><div class="winner-card-top">-----</div></th>
                    <th class="bg-call-side"></th>
                    <th class="strike-col">STRIKE</th>
                    <th class="bg-put-side"></th>
                    <th colspan="3" class="bg-put-side" id="cell-q5"><div class="winner-card-top">-----</div></th>
                    <th class="bg-put-side"></th>
                    <th colspan="5" class="bg-put-side" id="cell-u5"><div class="winner-card-top">-----</div></th>
                </tr>
                <tr id="row-columns">
                    <th class="th-ce">Vega</th><th class="th-ce">Theta</th><th class="th-ce">Gamma</th><th class="th-ce">Delta</th><th class="th-ce">IV</th>
                    <th class="th-ce">OI Chg</th><th class="ce-oi-col th-ce">OI</th><th class="ce-vol-col th-ce">Vol</th><th class="th-ce">Change</th><th class="th-ce">LTP</th>
                    <th class="strike-col">STRIKE</th>
                    <th class="th-pe">LTP</th><th class="th-pe">Change</th><th class="pe-vol-col th-pe">Vol</th><th class="pe-oi-col th-pe">OI</th><th class="th-pe">OI Chg</th>
                    <th class="th-pe">IV</th><th class="th-pe">Delta</th><th class="th-pe">Gamma</th><th class="th-pe">Theta</th><th class="th-pe">Vega</th>
                </tr>
            </thead>
            <tbody id="gridBody">
                <tr><td colspan="21" style="padding: 20px; font-size: 13px; color: #666;">Syncing option chain matrix...</td></tr>
            </tbody>
        </table>
    </div>
    <script>
        let allExpiriesData = {};
        let isFirstLoad = true;
        let busy = false;
        let fastPoll = false;
        let userInteracted = false;

        function sizeContainer() {
            const c = document.getElementById('tableContainer');
            const top = c.getBoundingClientRect().top + window.scrollY;
            c.style.maxHeight = Math.max(300, window.innerHeight - top - 8) + 'px';
        }
        function centerSpot() {
            const c = document.getElementById('tableContainer');
            const row = c.querySelector('.spot-target-row');
            if (!row) return;
            const headH = c.querySelector('thead').offsetHeight;
            const cr = c.getBoundingClientRect(), rr = row.getBoundingClientRect();
            const rowTop = rr.top - cr.top + c.scrollTop;
            c.scrollTop = rowTop + rr.height / 2 - (headH + (c.clientHeight - headH) / 2);
            const cell = row.querySelector('.strike-col');
            if (cell) {
                const cl = cell.getBoundingClientRect();
                c.scrollLeft = (cl.left - cr.left + c.scrollLeft) + cl.width / 2 - c.clientWidth / 2;
            }
        }
        const VIEW_ROWS = 11;               // imaginary line ke upar 11 aur neeche 11 strike screen par
        function fitViewport() {
            const c = document.getElementById('tableContainer');
            const rows = document.getElementById('gridBody').children;
            let si = -1;
            for (let i = 0; i < rows.length; i++) if (rows[i].classList.contains('spot-target-row')) { si = i; break; }
            if (si < 0) return;
            const first = rows[Math.max(0, si - VIEW_ROWS)], last = rows[Math.min(rows.length - 1, si + VIEW_ROWS)];
            const headH = c.querySelector('thead').offsetHeight;
            const bodyH = last.getBoundingClientRect().bottom - first.getBoundingClientRect().top;
            const extra = c.offsetHeight - c.clientHeight;          // border + horizontal scrollbar
            c.style.maxHeight = 'none';
            c.style.height = Math.ceil(headH + bodyH + extra) + 'px';
        }
        function centerNow() { userInteracted = false; sizeContainer(); fitViewport(); centerSpot(); }
        window.addEventListener('resize', () => { sizeContainer(); fitViewport(); if (!userInteracted) centerSpot(); });
        ['touchstart', 'wheel', 'mousedown'].forEach(ev => document.getElementById('tableContainer').addEventListener(ev, () => { userInteracted = true; }, {passive: true}));
        sizeContainer();
        const LOADING_ROW = '<tr><td colspan="21" style="padding: 20px; font-size: 13px; color: #666;">Loading data...</td></tr>';

        function initApp() {
            fetch('/api/init-meta').then(res => res.json()).then(meta => {
                allExpiriesData = meta.expiries;
                let symSelect = document.getElementById("symbolSelect");
                symSelect.innerHTML = "";
                // 1st Index list, 2nd MCX list, 3rd F&O stocks
                const groupLabels = {"INDEX": "INDEX", "MCX": "MCX", "STOCK": "F&O STOCKS"};
                ["INDEX", "MCX", "STOCK"].forEach(k => {
                    let list = (meta.groups && meta.groups[k]) || [];
                    if (!list.length) return;
                    let og = document.createElement("optgroup"); og.label = groupLabels[k];
                    list.forEach(sym => {
                        let opt = document.createElement("option"); opt.value = sym; opt.innerText = sym; og.appendChild(opt);
                    });
                    symSelect.appendChild(og);
                });
                if (meta.symbols.length > 0) {
                    symSelect.value = meta.active_symbol || meta.symbols[0];
                    onSymbolChange();
                }
                if (!meta.ready) setTimeout(initApp, 3000);
            }).catch(() => setTimeout(initApp, 3000));
        }
        function onSymbolChange() {
            let sym = document.getElementById("symbolSelect").value;
            let expSelect = document.getElementById("expirySelect");
            expSelect.innerHTML = "";
            let list = allExpiriesData[sym] || [];
            list.forEach(exp => {
                let opt = document.createElement("option"); opt.value = exp; opt.innerText = exp; expSelect.appendChild(opt);
            });
            isFirstLoad = true;
            updateSelectedSymbolSheet();
        }
        function updateSelectedSymbolSheet() {
            let sym = document.getElementById("symbolSelect").value;
            let exp = document.getElementById("expirySelect").value;
            if (!sym) return;
            document.getElementById("gridBody").innerHTML = LOADING_ROW;
            fastPoll = true;
            userInteracted = false;
            isFirstLoad = true;
            fetch('/api/set-symbol/' + encodeURIComponent(sym) + (exp ? '/' + encodeURIComponent(exp) : ''), {method: 'POST'}).then(() => fetchData());
        }
        function createSinglePillBadgeHtml(badgeObj) {
            if (!badgeObj || !badgeObj.show_badge) return "";
            let cls = badgeObj.state === 'WTT' ? 'state-wtt' : (badgeObj.state === 'WTB' ? 'state-wtb' : 'state-strong');
            let pctColor = badgeObj.state === 'WTB' ? '#c62828' : '#2e7d32';
            return `<div class="state-badge-pill ${cls}"><b>${badgeObj.state}</b> <span style="font-weight:800; color:${pctColor};">${badgeObj.pct}%</span></div>`;
        }
        function fetchData() {
            let sym = document.getElementById("symbolSelect").value;
            let exp = document.getElementById("expirySelect").value;
            if (!sym || busy) return;
            busy = true;
            fetch('/api/data/' + encodeURIComponent(sym) + '?t=' + new Date().getTime()).then(res => res.json()).then(data => {
                busy = false;
                if (document.getElementById("symbolSelect").value !== sym) return;
                let tbody = document.getElementById("gridBody");
                if (data.partial || (exp && data.expiry && data.expiry !== exp)) {
                    if (!tbody.innerHTML.includes("Loading data")) tbody.innerHTML = LOADING_ROW;
                    fastPoll = true;
                    return;
                }
                fastPoll = false;
                document.getElementById("lotSizeDisplay").innerText = "Lot Size: " + data.lot_size;
                let upd = document.getElementById("updDisplay");
                if (data.updated_at) {
                    upd.innerText = "Updated: " + data.updated_at + " (" + data.age + "s ago)";
                    upd.className = "lot-size-badge " + (data.age > 20 ? "upd-stale" : "upd-badge");
                }

                if (data.D5) document.getElementById("cell-d5").innerHTML = data.D5;
                if (data.H5) document.getElementById("cell-h5").innerHTML = data.H5;
                if (data.Q5) document.getElementById("cell-q5").innerHTML = data.Q5;
                if (data.U5) document.getElementById("cell-u5").innerHTML = data.U5;

                let k3Div = document.getElementById("k3Display");
                k3Div.innerText = data.K3;
                let k3TextLower = data.K3.toLowerCase();
                if (k3TextLower.includes("bull")) k3Div.style.color = "#0000FF";
                else if (k3TextLower.includes("bear") || k3TextLower.includes("bloodbath")) k3Div.style.color = "#FF0000";
                else k3Div.style.color = "#000000";

                if (data.grid_rows && data.grid_rows.length > 0) {
                    const cont = document.getElementById('tableContainer');
                    const keepTop = cont.scrollTop, keepLeft = cont.scrollLeft;
                    tbody.innerHTML = "";
                    data.grid_rows.forEach(row => {
                        let tr = document.createElement("tr");
                        if (row.is_spot_row) {
                            tr.classList.add("spot-row", "spot-target-row");
                            let renderSpotValue = row.spot_price || data.spot_price || 0;
                            let ceBadges = [];
                            let ceOiHtml = createSinglePillBadgeHtml(row.ce_oi_badge);
                            let ceVolHtml = createSinglePillBadgeHtml(row.ce_vol_badge);
                            if (ceOiHtml) ceBadges.push(ceOiHtml);
                            if (ceVolHtml) ceBadges.push(ceVolHtml);
                            let peBadges = [];
                            let peVolHtml = createSinglePillBadgeHtml(row.pe_vol_badge);
                            let peOiHtml = createSinglePillBadgeHtml(row.pe_oi_badge);
                            if (peVolHtml) peBadges.push(peVolHtml);
                            if (peOiHtml) peBadges.push(peOiHtml);
                            let ceOverlay = ceBadges.length > 0 ? `<div class="spot-badges-wrapper">${ceBadges.join('')}</div>` : '';
                            let peOverlay = peBadges.length > 0 ? `<div class="spot-badges-wrapper">${peBadges.join('')}</div>` : '';
                            tr.innerHTML = `<td></td><td></td><td></td><td></td><td></td><td></td><td colspan="2" style="position:relative;">${ceOverlay}</td><td></td><td></td><td class="spot-price-cell strike-col"><span class="spot-tag-red">SPOT</span><span class="spot-value-green">${renderSpotValue}</span></td><td></td><td></td><td colspan="2" style="position:relative;">${peOverlay}</td><td></td><td></td><td></td><td></td><td></td><td></td>`;
                        } else {
                            let ceOiChgCls = row.ce_oi_chg >= 0 ? 'chg-pos' : 'chg-neg';
                            let ceChgCls   = row.ce_change >= 0 ? 'chg-pos' : 'chg-neg';
                            let peChgCls   = row.pe_change >= 0 ? 'chg-pos' : 'chg-neg';
                            let peOiChgCls = row.pe_oi_chg >= 0 ? 'chg-pos' : 'chg-neg';

                            tr.innerHTML = `<td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_vega}</td>` +
                                `<td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_theta}</td>` +
                                `<td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_gamma}</td>` +
                                `<td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_delta}</td>` +
                                `<td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_iv}</td>` +
                                `<td class="${ceOiChgCls} ${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_oi_chg}</td>` +
                                `<td class="ce-oi-col ${row.ce_oi_color}">${row.ce_oi}<span class="pct-sub">${row.ce_oi_pct}%</span></td>` +
                                `<td class="ce-vol-col ${row.ce_vol_color}">${row.ce_vol}<span class="pct-sub">${row.ce_vol_pct}%</span></td>` +
                                `<td class="${ceChgCls} ${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_change}</td>` +
                                `<td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_ltp}</td>` +
                                `<td class="strike-col">${row.strike}</td>` +
                                `<td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_ltp}</td>` +
                                `<td class="${peChgCls} ${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_change}</td>` +
                                `<td class="pe-vol-col ${row.pe_vol_color}">${row.pe_vol}<span class="pct-sub">${row.pe_vol_pct}%</span></td>` +
                                `<td class="pe-oi-col ${row.pe_oi_color}">${row.pe_oi}<span class="pct-sub">${row.pe_oi_pct}%</span></td>` +
                                `<td class="${peOiChgCls} ${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_oi_chg}</td>` +
                                `<td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_iv}</td>` +
                                `<td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_delta}</td>` +
                                `<td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_gamma}</td>` +
                                `<td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_theta}</td>` +
                                `<td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_vega}</td>`;
                        }
                        tbody.appendChild(tr);
                    });

                    if (isFirstLoad || !userInteracted) {
                        sizeContainer();
                        fitViewport();
                        centerSpot();
                        requestAnimationFrame(() => { fitViewport(); centerSpot(); });
                        if (isFirstLoad) { setTimeout(() => { fitViewport(); centerSpot(); }, 300); setTimeout(() => { fitViewport(); centerSpot(); }, 900); }
                        isFirstLoad = false;
                    } else {
                        cont.scrollTop = keepTop;
                        cont.scrollLeft = keepLeft;
                    }
                }
            }).catch(() => { busy = false; });
        }
        function pollLoop() { fetchData(); setTimeout(pollLoop, fastPoll ? 500 : 1500); }
        window.onload = function() { initApp(); pollLoop(); };
    </script>
</body>
</html>
"""


# --- FLASK ROUTES ---
@app.before_request
def basic_auth_guard():
    if not DASH_USER or request.path == "/health":
        return None
    a = request.authorization
    if a and hmac.compare_digest(a.username or "", DASH_USER) and hmac.compare_digest(a.password or "", DASH_PASS or ""):
        return None
    return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Dashboard"'})


@app.route('/health')
def health():
    return "ok"


@app.route('/')
def home():
    return render_template_string(HTML_TEMPLATE)


@app.route('/api/init-meta')
def get_initial_metadata():
    last_ui_poll[0] = time.time()
    wake_active.set()
    groups = {"INDEX": [], "MCX": [], "STOCK": []}
    for sname in options_cache:
        groups[symbol_kind_cache.get(sname, "INDEX" if sname in INDEX_NAMES else "STOCK")].append(sname)
    rank = {n: i for i, n in enumerate(INDEX_NAMES)}
    groups["INDEX"].sort(key=lambda n: (rank.get(n, 99), n))
    groups["MCX"].sort()
    groups["STOCK"].sort()
    if not options_cache:
        groups["INDEX"] = list(INDICES)
    return jsonify({
        "symbols": groups["INDEX"] + groups["MCX"] + groups["STOCK"],
        "groups": groups,
        "expiries": symbol_expiries_cache,
        "active_symbol": current_active_symbol_ui,
        "ready": bool(options_cache)
    })


@app.route('/api/set-symbol/<symbol>', defaults={'expiry': None}, methods=['POST'])
@app.route('/api/set-symbol/<symbol>/<expiry>', methods=['POST'])
def set_symbol_api(symbol, expiry):
    global current_active_symbol_ui
    sym = symbol.upper()
    current_active_symbol_ui = sym
    if expiry:
        user_selected_expiries[sym] = expiry.upper()
    last_ui_poll[0] = time.time()
    wake_active.set()
    return jsonify({"status": "success"})


@app.route('/api/data/<symbol>')
def get_symbol_runtime_data(symbol):
    if time.time() - last_ui_poll[0] > 10:
        wake_active.set()
    last_ui_poll[0] = time.time()
    sym = symbol.upper()
    vault = multi_symbol_runtime_states.get(sym, {})
    ts = vault.get("fetched_ts")
    return jsonify({
        "lot_size": vault.get("lot_size", 1),
        "D5": vault.get("D5_TEXT", '<div class="winner-card-top">-----</div>'),
        "H5": vault.get("H5_TEXT", '<div class="winner-card-top">-----</div>'),
        "Q5": vault.get("Q5_TEXT", '<div class="winner-card-top">-----</div>'),
        "U5": vault.get("U5_TEXT", '<div class="winner-card-top">-----</div>'),
        "K3": vault.get("K3_TEXT", f"{sym} is Neutral"),
        "grid_rows": vault.get("grid_rows", []),
        "spot_price": vault.get("spot_price", 0),
        "expiry": vault.get("active_expiry", ""),
        "partial": (not vault.get("grid_rows")) or (
            market_open(symbol_exch_cache.get(sym, ""), datetime.now()) and not (
                vault.get("extra_exp") == vault.get("active_expiry") and (time.time() - vault.get("extra_ts", 0)) < 30)),
        "updated_at": vault.get("updated_at", ""),
        "age": int(time.time() - ts) if ts else 0
    })


@app.route('/api/debug/<symbol>')
def get_debug(symbol):
    return jsonify(debug_info.get(symbol.upper(), {}))


@app.route('/api/log/<symbol>')
def get_event_log(symbol):
    lines = list(event_log.get(symbol.upper(), []))
    return Response("\n".join(lines) or "no events yet", mimetype="text/plain")


# --- BACKGROUND ENGINE ---
def market_open(exch, now_dt):
    if now_dt.weekday() >= 5:
        return False
    hm = now_dt.strftime("%H:%M")
    if exch == "MCX":
        return "09:00" <= hm <= "23:55"
    return "09:00" <= hm <= "15:35"


def do_login(smart_api):
    last_login_ts[0] = time.time()
    if not (CLIENT_ID and PIN and TOTP_SECRET):
        print("❌ ANGEL_CLIENT_ID / ANGEL_PIN / ANGEL_TOTP_SECRET env set nahi hain")
        return False
    try:
        totp = pyotp.TOTP(TOTP_SECRET.replace(" ", "")).now()
        with api_lock:
            res = smart_api.generateSession(CLIENT_ID, PIN, totp)
        if res and res.get('status'):
            print("🔐 Session generated")
            return True
        print(f"❌ Login failed: {str(res)[:150]}")
    except Exception as e:
        print(f"❌ Login exception: {e}")
    return False


def system_ready():
    today = datetime.now().strftime("%Y-%m-%d")
    return bool(options_cache) and last_session_date == today and scrip_master_date == today and not need_relogin[0]


def active_loop():
    """Screen wala symbol: alag thread, API par priority, ~3s cadence."""
    n = 0
    while True:
        t0 = time.time()
        try:
            if system_ready() and (time.time() - last_ui_poll[0]) < 10:
                active = current_active_symbol_ui
                if active in options_cache:
                    lk = get_sym_lock(active)
                    if lk.acquire(timeout=5):
                        try:
                            now = time.time()
                            v = multi_symbol_runtime_states.get(active, {})
                            exp_list = symbol_expiries_cache.get(active) or []
                            chosen = user_selected_expiries.get(active, exp_list[0] if exp_list else "")
                            if exp_list and chosen not in exp_list:
                                chosen = exp_list[0]
                            is_open = market_open(symbol_exch_cache.get(active, ""), datetime.now())
                            chain_ok = bool(v.get("grid_rows")) and v.get("active_expiry") == chosen
                            extra_ok = v.get("extra_exp") == chosen and (now - v.get("extra_ts", 0)) < 20
                            core_age = now - v.get("fetched_ts", 0)
                            mode = None
                            if not is_open:
                                if (not chain_ok) or v.get("extra_exp") != chosen or core_age > 60:
                                    mode = "full"
                            elif (not chain_ok) or (not extra_ok and core_age > 8):
                                mode = "full"; n = 0
                            elif not extra_ok:
                                mode = "extra"
                            else:
                                mode = "core"; n += 1
                            if mode:
                                active_pending[0] += 1
                                try:
                                    fetch_symbol_data(smart_api_global, active, mode, True)
                                    if mode == "core" and n % 3 == 0:
                                        fetch_symbol_data(smart_api_global, active, "extra", True)
                                finally:
                                    active_pending[0] -= 1
                        finally:
                            lk.release()
        except Exception as e:
            print(f"⚠ Active loop error: {e}")
        wake_active.wait(timeout=max(0.2, 3.0 - (time.time() - t0)))
        wake_active.clear()


def run_bot_lock_loop():
    """Login, scrip master, state save aur background (light) scanning."""
    global last_session_date, smart_api_global
    print("🚀 Starting Background Bot Loop...")
    time.sleep(3)

    try:
        smart_api = SmartConnect(api_key=API_KEY)
        smart_api_global = smart_api
    except Exception as e:
        print(f"❌ Init Error: {e}")
        return

    load_state()
    threading.Thread(target=active_loop, daemon=True).start()
    login_fail = 0
    last_save = time.time()
    last_sm_try = 0.0
    last_stat = time.time()
    rot_i = 0

    while True:
        try:
            now_dt = datetime.now()
            today = now_dt.strftime("%Y-%m-%d")

            # LOGIN (backoff ke saath: galat PIN par account lock na ho)
            if last_session_date != today or need_relogin[0]:
                if do_login(smart_api):
                    last_session_date = today
                    need_relogin[0] = False
                    login_fail = 0
                else:
                    login_fail += 1
                    time.sleep(600 if login_fail >= 5 else 20 * login_fail)
                    continue

            # SCRIP MASTER (roz ek baar, fail ho to har 60s retry)
            if scrip_master_date != today:
                if time.time() - last_sm_try > 60:
                    last_sm_try = time.time()
                    initialize_scrip_master()
                if not options_cache:
                    time.sleep(5)
                    continue

            ui_open = (time.time() - last_ui_poll[0]) < 10
            active = current_active_symbol_ui

            # Background: ek symbol per loop (indices har ~10 symbols baad)
            open_syms = [s for s in options_cache if market_open(symbol_exch_cache.get(s, ""), now_dt)]
            others = sorted(s for s in open_syms if s not in INDICES)
            idx_open = [s for s in INDICES if s in open_syms]
            rotation = []
            for k, s in enumerate(others):
                rotation.append(s)
                if k % 10 == 9:
                    rotation.extend(idx_open)
            if not others:
                rotation = idx_open
            # Hot symbol (jo last screen par tha): UI band ho tab bhi taaza rakho, taaki kholte hi data tayyar mile
            if (not ui_open) and active in options_cache and market_open(symbol_exch_cache.get(active, ""), now_dt):
                hv = multi_symbol_runtime_states.get(active, {})
                hot_mode = None
                if time.time() - hv.get("extra_ts", 0) > 25:
                    hot_mode = "extra"
                elif time.time() - hv.get("fetched_ts", 0) > 10:
                    hot_mode = "core"
                if hot_mode and hv.get("grid_rows"):
                    hl = get_sym_lock(active)
                    if hl.acquire(blocking=False):
                        try:
                            fetch_symbol_data(smart_api, active, hot_mode, False)
                        finally:
                            hl.release()

            if rotation:
                sym = rotation[rot_i % len(rotation)]
                rot_i += 1
                if not (ui_open and sym == active):
                    lk = get_sym_lock(sym)
                    if lk.acquire(blocking=False):
                        try:
                            fetch_symbol_data(smart_api, sym, "light", False)
                        finally:
                            lk.release()
            else:
                time.sleep(5)

            if time.time() - last_stat > 60:
                last_stat = time.time()
                r = max(api_stats["req"], 1)
                print(f"📊 API: {api_stats['req']} req/min, fail={api_stats['fail']}, avg latency={api_stats['lat'] / r:.2f}s")
                api_stats["req"] = api_stats["fail"] = 0
                api_stats["lat"] = 0.0

            if time.time() - last_save > 30:
                with state_lock:
                    save_state()
                last_save = time.time()
        except Exception as e:
            print(f"⚠ Loop Error: {e}")
            time.sleep(3)
        time.sleep(0.2)


if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    run_bot_lock_loop()
