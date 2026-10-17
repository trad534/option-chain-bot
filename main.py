import time
import os
import json
import requests
import pandas as pd
import numpy as np
import pyotp
from scipy.stats import norm
from scipy.optimize import brentq
from datetime import datetime
from SmartApi import SmartConnect
from flask import Flask, jsonify, render_template_string
import threading
import builtins

RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "")

def self_ping():
    while True:
        try:
            if RENDER_URL:
                requests.get(RENDER_URL, timeout=5)
        except Exception:
            pass
        time.sleep(180)

threading.Thread(target=self_ping, daemon=True).start()

def print(*args, **kwargs):
    kwargs['flush'] = True
    builtins.print(*args, **kwargs)

app = Flask(__name__)

@app.route('/')
def home():
    return render_template_string(HTML_TEMPLATE)

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

CLIENT_ID = os.getenv("ANGEL_CLIENT_ID")
PIN = os.getenv("ANGEL_PIN")
API_KEY = os.getenv("ANGEL_API_KEY")
TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET")

script_memory = {}
global_run_date = ""
persistent_quotes_cache = {}
multi_symbol_runtime_states = {}
symbol_expiries_cache = {}
options_cache = {}
full_scrip_df = pd.DataFrame()
morning_oi_dict = {}
baseline_date = None
current_active_symbol_for_memory = "NIFTY"
current_active_symbol_ui = "NIFTY"
last_session_date = None
user_selected_expiries = {}
smart_api_global = None

def get_memory(key, default=""):
    global current_active_symbol_for_memory
    return script_memory.get(f"{current_active_symbol_for_memory}_{key}", default)

def set_memory(key, value):
    global current_active_symbol_for_memory
    script_memory[f"{current_active_symbol_for_memory}_{key}"] = str(value)

def calculate_iv_and_greeks(flag, S, K, t, r, ltp):
    if t <= 0 or S <= 0 or K <= 0 or ltp <= 0.05:
        delta = 1.0 if (flag == 'CE' and S > K) else (0.0 if flag == 'CE' else (-1.0 if S < K else 0.0))
        return {'iv': 0, 'delta': round(delta, 3), 'gamma': 0, 'theta': 0, 'vega': 0}
    intrinsic = max(0, S - K * np.exp(-r * t)) if flag == 'CE' else max(0, K * np.exp(-r * t) - S)
    if ltp < intrinsic: ltp = intrinsic + 0.05
    def objective_function(sigma):
        if sigma <= 0: sigma = 1e-4
        d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * t) / (sigma * np.sqrt(t))
        d2 = d1 - sigma * np.sqrt(t)
        return (S * norm.cdf(d1) - K * np.exp(-r * t) * norm.cdf(d2) if flag == 'CE' else K * np.exp(-r * t) * norm.cdf(-d2) - S * norm.cdf(-d1)) - ltp
    try: sigma = brentq(objective_function, 1e-4, 5.0, xtol=1e-6, maxiter=100)
    except Exception: sigma = 0.20
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * t) / (sigma * np.sqrt(t))
    gamma = norm.pdf(d1) / (S * sigma * np.sqrt(t))
    vega_val = S * norm.pdf(d1) * np.sqrt(t) / 100
    if flag == 'CE':
        delta = norm.cdf(d1)
        theta = (- (S * norm.pdf(d1) * sigma) / (2 * np.sqrt(t)) - r * K * np.exp(-r * t) * norm.cdf(d1 - sigma * np.sqrt(t))) / 365
    else:
        delta = norm.cdf(d1) - 1
        theta = (- (S * norm.pdf(d1) * sigma) / (2 * np.sqrt(t)) + r * K * np.exp(-r * t) * norm.cdf(-(d1 - sigma * np.sqrt(t)))) / 365
    return {'iv': round(sigma * 100, 2), 'delta': round(delta, 3), 'gamma': round(gamma, 4), 'theta': round(theta, 2), 'vega': round(vega_val, 2)}

def execute_python_scenario_tracker(raw_rows=None, spot_price=0, lot_size=1, active_expiry="", symbol_name="NIFTY"):
    global global_run_date, multi_symbol_runtime_states, current_active_symbol_for_memory
    try:
        now_dt = datetime.now()
        today_str = now_dt.strftime("%Y-%m-%d")
        currentTimeStr = now_dt.strftime("%I:%M %p").lower()

        if global_run_date != today_str:
            script_memory.clear()
            global_run_date = today_str

        current_active_symbol_for_memory = symbol_name
        if not raw_rows: return

        strikes_list = [float(r['strike']) for r in raw_rows]
        spot_idx = 0
        if strikes_list:
            spot_idx = min(range(len(strikes_list)), key=lambda i: abs(strikes_list[i] - spot_price))

        ceVolArray, ceOiArray, peVolArray, peOiArray = [], [], [], []

        for idx, r_dict in enumerate(raw_rows):
            row_idx = 8 + idx
            strike = float(r_dict['strike'])
            if not strike: continue

            if idx >= (spot_idx - 3) and idx < len(raw_rows):
                ceOiArray.append({'strike': strike, 'val': float(r_dict.get('ce_oi', 0)), 'row': row_idx})
                ceVolArray.append({'strike': strike, 'val': float(r_dict.get('ce_vol', 0)), 'row': row_idx})

            if idx <= (spot_idx + 3):
                peVolArray.append({'strike': strike, 'val': float(r_dict.get('pe_vol', 0)), 'row': row_idx})
                peOiArray.append({'strike': strike, 'val': float(r_dict.get('pe_oi', 0)), 'row': row_idx})

        def extract_top_two(data_array):
            if not data_array: return {'highest': {'strike': 0, 'val': 0, 'row': 0}, 'second': {'strike': 0, 'val': 0, 'row': 0}, 'pct': 0}
            filtered = [item for item in data_array if item['val'] > 0]
            if not filtered: return {'highest': {'strike': 0, 'val': 0, 'row': 0}, 'second': {'strike': 0, 'val': 0, 'row': 0}, 'pct': 0}
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
            prev_strike_str = get_memory("PREV_STRK_" + cache_key)
            prev_strike = float(prev_strike_str) if prev_strike_str else strike
            current_weak_hist = get_memory("WEAK_HIST_" + cache_key, "")

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

        ceWinner = "VOL"
        if ceVolTop['highest']['strike'] == ceOiTop['highest']['strike'] and ceOiRawState == "wtb" and ceVolRawState == "wtb":
            ceWinner = "BOTH"
        elif ceVolTop['highest']['strike'] == ceOiTop['highest']['strike'] and not ceOiIsStable100 and not ceVolIsStable100 and (ceOiTop['second']['strike'] > ceOiTop['highest']['strike']) and (ceVolTop['second']['strike'] > ceOiTop['highest']['strike']):
            ceWinner = "BOTH"
        elif ceVolTop['highest']['strike'] == ceOiTop['highest']['strike'] and ceOiIsStable100 and ceVolIsStable100:
            ceWinner = "BOTH"
        elif ceVolTop['highest']['strike'] == ceOiTop['highest']['strike'] and ceOiIsStable100 and not ceVolIsStable100 and (ceVolTop['second'] and ceVolTop['second']['strike'] > ceOiTop['highest']['strike']):
            ceWinner = "OI"
        elif ceVolTop['highest']['strike'] == ceOiTop['highest']['strike'] and ceVolIsStable100 and not ceOiIsStable100 and (ceOiTop['second'] and ceOiTop['second']['strike'] > ceOiTop['highest']['strike']):
            ceWinner = "VOL"
        elif ceOiIsStable100 and not ceVolIsStable100 and (ceVolTop['second'] and ceVolTop['second']['strike'] > ceOiTop['highest']['strike']) and (ceOiTop['highest']['strike'] < ceVolTop['highest']['strike']):
            ceWinner = "OI"
        elif ceVolIsStable100 and not ceOiIsStable100 and (ceVolTop['second'] and ceVolTop['second']['strike'] > ceOiTop['highest']['strike']) and (ceVolTop['highest']['strike'] < ceOiTop['highest']['strike']):
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

        peWinner = "VOL"
        if peVolTop['highest']['strike'] == peOiTop['highest']['strike'] and peOiRawState == "wtt" and peVolRawState == "wtt":
            peWinner = "BOTH"
        elif peVolTop['highest']['strike'] == peOiTop['highest']['strike'] and not peOiIsStable100 and not peVolIsStable100 and (peOiTop['second']['strike'] < peOiTop['highest']['strike']) and (peVolTop['second']['strike'] < peOiTop['highest']['strike']):
            peWinner = "BOTH"
        elif peVolTop['highest']['strike'] == peOiTop['highest']['strike'] and peOiIsStable100 and peVolIsStable100:
            peWinner = "BOTH"
        elif peVolTop['highest']['strike'] == peOiTop['highest']['strike'] and peOiIsStable100 and not peVolIsStable100 and (peVolTop['second'] and peVolTop['second']['strike'] < peOiTop['highest']['strike']):
            peWinner = "OI"
        elif peVolTop['highest']['strike'] == peOiTop['highest']['strike'] and peOiIsStable100 and not peOiIsStable100 and (peVolTop['second'] and peVolTop['second']['strike'] < peOiTop['highest']['strike']):
            peWinner = "VOL"
        elif peOiIsStable100 and not peVolIsStable100 and (peVolTop['second'] and peVolTop['second']['strike'] < peOiTop['highest']['strike']) and (peOiTop['highest']['strike'] > peVolTop['highest']['strike']):
            peWinner = "OI"
        elif peVolIsStable100 and not peOiIsStable100 and (peVolTop['second'] and peVolTop['second']['strike'] < peOiTop['highest']['strike']) and (peVolTop['highest']['strike'] > peVolTop['highest']['strike']):
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
                peWinner = "VOL" if peVolTop['highest']['row'] >= peOiTop['highest']['row'] else "OI"

        if ceOiRawState in ["wtt", "wtb"]: set_memory("WEAK_HIST_CE_OI", ceOiRawState)
        if ceVolRawState in ["wtt", "wtb"]: set_memory("WEAK_HIST_CE_VOL", ceVolRawState)
        if peOiRawState in ["wtt", "wtb"]: set_memory("WEAK_HIST_PE_OI", peOiRawState)
        if peVolRawState in ["wtt", "wtb"]: set_memory("WEAK_HIST_PE_VOL", peVolRawState)

        def calculateIndependentCell(topObj, cacheKey, sideLabel, isWinner, rawState, finalStatusText):
            if not topObj or not topObj.get('highest') or topObj['highest']['strike'] == 0:
                return {'text': "<div>-----</div>", 'status': "stable strong", 'scenarioStatus': "stable strong", 'rawState': "STRONG"}

            strike = topObj['highest']['strike']
            secondStrike = strike
            if topObj.get('second') and topObj['second']['val'] > 0: secondStrike = topObj['second']['strike']

            prevStrikeStr = get_memory("PREV_STRK_" + cacheKey)
            prevStrike = float(prevStrikeStr) if prevStrikeStr else strike
            if not prevStrikeStr:
                set_memory("PREV_STRK_" + cacheKey, str(strike))
                prevStrike = strike

            displayStatus = finalStatusText
            scenarioStatus = finalStatusText
            isShifting = False
            shiftType = get_memory("SHIFT_TYPE_" + cacheKey, "")
            shiftFromStrike = float(get_memory("SHIFT_FROM_" + cacheKey, 0))
            lastTargetStrike = float(get_memory("SHIFT_TARGET_" + cacheKey, 0))

            if get_memory("IS_SHIFTED_" + cacheKey) == "true":
                if strike == shiftFromStrike or (lastTargetStrike > 0 and secondStrike != lastTargetStrike):
                    set_memory("IS_SHIFTED_" + cacheKey, "false")
                    set_memory("PREV_STRK_" + cacheKey, str(strike))
                    script_memory.pop(f"{symbol_name}_SHIFT_TARGET_" + cacheKey, None)

            if strike != prevStrike and prevStrike > 0 and (finalStatusText == "wtb" or finalStatusText == "wtt"):
                if strike < prevStrike:
                    shiftType = "top to bottom"
                    scenarioStatus = "wtb"
                else:
                    shiftType = "bottom to top"
                    scenarioStatus = "wtt"
                set_memory("IS_SHIFTED_" + cacheKey, "true")
                set_memory("SHIFT_TYPE_" + cacheKey, shiftType)
                set_memory("SHIFT_FROM_" + cacheKey, str(prevStrike))
                set_memory("SHIFT_TARGET_" + cacheKey, str(secondStrike))

            if get_memory("IS_SHIFTED_" + cacheKey) == "true":
                if rawState == "STRONG" or finalStatusText == "stable strong" or "to strong" in finalStatusText:
                    set_memory("IS_SHIFTED_" + cacheKey, "false")
                    set_memory("PREV_STRK_" + cacheKey, str(strike))
                    isShifting = False
                    scenarioStatus = finalStatusText
                else:
                    isShifting = True
                    shiftType = get_memory("SHIFT_TYPE_" + cacheKey)
                    scenarioStatus = "wtb" if shiftType == "top to bottom" else "wtt"
            else:
                set_memory("PREV_STRK_" + cacheKey, str(strike))
                scenarioStatus = finalStatusText

            if not isWinner and rawState != "STRONG":
                displayStatus = "stable strong"
                scenarioStatus = "stable strong"

            # 🔒 9:15 AM EXACT EVENT TIMESTAMP FREEZE LOCK
            displayTime = get_memory("TIME_" + cacheKey)
            lastRecordedStatus = get_memory("LAST_STATUS_" + cacheKey, "")
            isTriggerStatus = displayStatus in ["wtb", "wtt", "wtb to strong", "wtt to strong"] or isShifting

            if isTriggerStatus and displayStatus != lastRecordedStatus:
                displayTime = currentTimeStr
                set_memory("TIME_" + cacheKey, displayTime)
                set_memory("LAST_STATUS_" + cacheKey, displayStatus)
            elif not displayTime:
                displayTime = currentTimeStr
                set_memory("TIME_" + cacheKey, displayTime)
                set_memory("LAST_STATUS_" + cacheKey, displayStatus)

            if isShifting:
                line1 = f"{displayTime}-{sideLabel}-{strike}-{displayStatus}-{secondStrike}"
                line2 = f"shifted {shiftType}"
                outputText = f"<div>{line1}</div><div class='yellow-pill-badge'>{line2}</div>"
            else:
                if displayStatus == "stable strong":
                    line1 = f"{displayTime}-{sideLabel}-{strike}"
                    line2 = "stable strong"
                    outputText = f"<div>{line1}</div><div class='yellow-pill-badge'>{line2}</div>"
                elif "to strong" in displayStatus:
                    line1 = f"{displayTime}-{sideLabel}-{strike}"
                    line2 = displayStatus
                    outputText = f"<div>{line1}</div><div class='yellow-pill-badge'>{line2}</div>"
                else:
                    weakDir = "weak towards top" if displayStatus == "wtt" else "weak towards bottom"
                    line1 = f"{displayTime}-{sideLabel}-{strike}-{displayStatus}-{secondStrike}"
                    line2 = weakDir
                    outputText = f"<div>{line1}</div><div class='yellow-pill-badge'>{line2}</div>"

            return {'text': outputText, 'status': displayStatus, 'scenarioStatus': scenarioStatus, 'rawState': rawState}

        ceOiResult  = calculateIndependentCell(ceOiTop, "CE_OI", "Eor OI", ceWinner == "OI" or ceWinner == "BOTH", ceOiRawState, ceOiStatusText)
        ceVolResult = calculateIndependentCell(ceVolTop, "CE_VOL", "Eor vol", ceWinner == "VOL" or ceWinner == "BOTH", ceVolRawState, ceVolStatusText)
        peVolResult = calculateIndependentCell(peVolTop, "PE_VOL", "Eos vol", peWinner == "VOL" or peWinner == "BOTH", peVolRawState, peVolStatusText)
        peOiResult  = calculateIndependentCell(peOiTop, "PE_OI", "Eos OI", peWinner == "OI" or peWinner == "BOTH", peOiRawState, peOiStatusText)

        finalCeState = ceOiResult['scenarioStatus'] if ceWinner == "OI" else ceVolResult['scenarioStatus']
        finalPeState = peOiResult['scenarioStatus'] if peWinner == "OI" else peVolResult['scenarioStatus']

        if ceWinner == "BOTH": finalCeState = ceVolResult['scenarioStatus'] if "to strong" in ceVolResult['scenarioStatus'] else ceOiResult['scenarioStatus']
        if peWinner == "BOTH": finalPeState = peVolResult['scenarioStatus'] if "to strong" in peVolResult['scenarioStatus'] else peOiResult['scenarioStatus']

        checkCe = finalCeState
        checkPe = finalPeState

        k3Text = f"{symbol_name} is Neutral"
        if checkCe == "wtt" and checkPe == "wtb": k3Text = f"{symbol_name} is 8th scenario both side Risky"
        elif checkCe == "wtb" and checkPe == "wtt": k3Text = f"{symbol_name} is 9th scenario both side Risky"
        elif checkCe == "stable strong" and checkPe == "stable strong": k3Text = f"{symbol_name} is Neutral"
        elif checkCe == "wtb" and checkPe == "stable strong": k3Text = f"{symbol_name} is slightly Bearish"
        elif checkCe == "stable strong" and checkPe == "wtb": k3Text = f"{symbol_name} is slightly Bearish"
        elif checkCe == "wtt" and checkPe == "stable strong": k3Text = f"{symbol_name} is slightly bullish"
        elif checkCe == "stable strong" and checkPe == "wtt": k3Text = f"{symbol_name} is slightly bullish"
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
        elif checkCe == "wtb to strong" and checkPe == "stable strong": k3Text = f"{symbol_name} is slightly Bullish"
        elif checkCe == "stable strong" and checkPe == "wtb to strong": k3Text = f"{symbol_name} is slightly Bullish"
        elif checkCe == "wtt to strong" and checkPe == "stable strong": k3Text = f"{symbol_name} is slightly Bearish"
        elif checkCe == "stable strong" and checkPe == "wtt to strong": k3Text = f"{symbol_name} is slightly Bearish"
        elif checkCe == "wtt to strong" and checkPe == "wtb to strong": k3Text = f"{symbol_name} is 8th scenario both side Risky trade"
        elif checkCe == "wtb to strong" and checkPe == "wtt to strong": k3Text = f"{symbol_name} is 9th scenario both side Risky trade"
        elif checkCe == "wtb to strong" and checkPe == "wtb to strong": k3Text = f"{symbol_name} is confirm Bull Run"
        elif checkCe == "wtt to strong" and checkPe == "wtt to strong": k3Text = f"{symbol_name} is confirm bloodbath"

        finalD5 = "<div>-----</div>"
        finalH5 = "<div>-----</div>"
        finalQ5 = "<div>-----</div>"
        finalU5 = "<div>-----</div>"

        if ceWinner == "BOTH": 
            finalD5 = ceOiResult['text']
            finalH5 = ceVolResult['text']
        elif ceWinner == "VOL": 
            finalD5 = "<div>-----</div>"
            finalH5 = ceVolResult['text']
        elif ceWinner == "OI": 
            finalD5 = ceOiResult['text']
            finalH5 = "<div>-----</div>"

        if peWinner == "BOTH": 
            finalU5 = peOiResult['text']
            finalQ5 = peVolResult['text']
        elif peWinner == "VOL": 
            finalU5 = "<div>-----</div>"
            finalH5 = ceVolResult['text']
        elif peWinner == "OI": 
            finalU5 = peOiResult['text']
            finalH5 = "<div>-----</div>"

        if symbol_name not in multi_symbol_runtime_states: multi_symbol_runtime_states[symbol_name] = {}
        vault = multi_symbol_runtime_states[symbol_name]
        vault["D5_TEXT"] = finalD5
        vault["H5_TEXT"] = finalH5
        vault["U5_TEXT"] = finalU5
        vault["Q5_TEXT"] = finalQ5
        vault["K3_TEXT"] = k3Text
        vault["lot_size"] = lot_size
        vault["spot_price"] = spot_price
        vault["active_expiry"] = active_expiry

        c_oi_1 = ceOiTop['highest']['strike']
        c_oi_2 = ceOiTop['second']['strike']
        c_vol_1 = ceVolTop['highest']['strike']
        c_vol_2 = ceVolTop['second']['strike']
        p_vol_1 = peVolTop['highest']['strike']
        p_vol_2 = peVolTop['second']['strike']
        p_oi_1 = peOiTop['highest']['strike']
        p_oi_2 = peOiTop['second']['strike']

        max_ce_oi = ceOiTop['highest']['val'] or 1
        max_ce_vol = ceVolTop['highest']['val'] or 1
        max_pe_vol = peVolTop['highest']['val'] or 1
        max_pe_oi = peOiTop['highest']['val'] or 1

        grid_rows_built = []
        spot_inserted = False
        spot_insert_idx = spot_idx
        if strikes_list and spot_price > strikes_list[spot_insert_idx] and spot_insert_idx < len(strikes_list) - 1:
            spot_insert_idx += 1

        def build_badge_payload(top_obj, raw_state):
            if not top_obj or top_obj['highest']['strike'] == 0: return {}
            s_type = raw_state.upper() if raw_state in ["wtt", "wtb"] else "STRONG"
            return {"state": s_type, "pct": round(top_obj['pct'], 2), "show_badge": True}

        for idx, r_dict in enumerate(raw_rows):
            if not spot_inserted and idx == spot_insert_idx:
                grid_rows_built.append({
                    'is_spot_row': True, 
                    'spot_price': round(spot_price, 2),
                    'ce_oi_badge': build_badge_payload(ceOiTop, ceOiRawState),
                    'ce_vol_badge': build_badge_payload(ceVolTop, ceVolRawState),
                    'pe_vol_badge': build_badge_payload(peVolTop, peVolRawState),
                    'pe_oi_badge': build_badge_payload(peOiTop, peOiRawState)
                })
                spot_inserted = True

            st = r_dict['strike']
            r_copy = r_dict.copy()
            r_copy['ce_oi_pct'] = round((r_dict.get('ce_oi', 0) / max_ce_oi) * 100, 2)
            r_copy['ce_vol_pct'] = round((r_dict.get('ce_vol', 0) / max_ce_vol) * 100, 2)
            r_copy['pe_vol_pct'] = round((r_dict.get('pe_vol', 0) / max_pe_vol) * 100, 2)
            r_copy['pe_oi_pct'] = round((r_dict.get('pe_oi', 0) / max_pe_oi) * 100, 2)

            r_copy['ce_oi_color'] = 'bg-100' if st == c_oi_1 else ('bg-75' if st == c_oi_2 and ceOiTop['pct'] >= 75.0 else '')
            r_copy['ce_vol_color'] = 'bg-100' if st == c_vol_1 else ('bg-75' if st == c_vol_2 and ceVolTop['pct'] >= 75.0 else '')
            r_copy['pe_vol_color'] = 'bg-100' if st == p_vol_1 else ('bg-75' if st == p_vol_2 and peVolTop['pct'] >= 75.0 else '')
            r_copy['pe_oi_color'] = 'bg-100' if st == p_oi_1 else ('bg-75' if st == p_oi_2 and peOiTop['pct'] >= 75.0 else '')

            grid_rows_built.append(r_copy)

        if not spot_inserted:
            grid_rows_built.append({
                'is_spot_row': True, 
                'spot_price': round(spot_price, 2),
                'ce_oi_badge': build_badge_payload(ceOiTop, ceOiRawState),
                'ce_vol_badge': build_badge_payload(ceVolTop, ceVolRawState),
                'pe_vol_badge': build_badge_payload(peVolTop, peVolRawState),
                'pe_oi_badge': build_badge_payload(peOiTop, peOiRawState)
            })

        vault["grid_rows"] = grid_rows_built
        vault["spot_price"] = round(spot_price, 2)
    except Exception:
        pass

def fetch_symbol_data(smart_api, clean_active_symbol):
    global morning_oi_dict, baseline_date, options_cache, persistent_quotes_cache, symbol_expiries_cache, user_selected_expiries
    try:
        now_dt = datetime.now()
        current_date_str = now_dt.strftime('%Y-%m-%d')
        if baseline_date != current_date_str: morning_oi_dict = {}; baseline_date = current_date_str
        if not options_cache: return

        if clean_active_symbol in options_cache:
            sym_options = options_cache[clean_active_symbol]
            valid_opts = sym_options[sym_options['parsed_expiry'] >= pd.Timestamp(now_dt.date())].copy()
            if not valid_opts.empty:
                unique_expiries_dt = sorted(valid_opts['parsed_expiry'].unique())
                expiry_strings_sorted = [pd.Timestamp(dt).strftime('%d%b%Y').upper() for dt in unique_expiries_dt]
                
                target_expiry_str = user_selected_expiries.get(clean_active_symbol, expiry_strings_sorted[0])
                
                matched_exp = [dt for dt in unique_expiries_dt if pd.Timestamp(dt).strftime('%d%b%Y').upper() == target_expiry_str]
                if matched_exp:
                    current_expiry_dt = matched_exp[0]
                    current_expiry = target_expiry_str
                else:
                    current_expiry_dt = unique_expiries_dt[0]
                    current_expiry = expiry_strings_sorted[0]

                current_expiry_options = valid_opts[valid_opts['parsed_expiry'] == current_expiry_dt].copy()
                lot_size = int(float(current_expiry_options['lotsize'].iloc[0])) if 'lotsize' in current_expiry_options.columns and not current_expiry_options['lotsize'].empty else 1

                spot_price = 0.0
                index_tokens = {"NIFTY": "99992000", "BANKNIFTY": "99992009", "FINNIFTY": "99992018"}
                target_tok = index_tokens.get(clean_active_symbol, "")
                
                if target_tok:
                    try:
                        q_res = smart_api.getMarketData("FULL", {"NSE": [target_tok]})
                        if q_res and q_res.get('status') and q_res.get('data') and q_res['data'].get('fetched'):
                            s_val = float(q_res['data']['fetched'][0].get('ltp', 0))
                            if s_val > 0: spot_price = s_val
                    except Exception: pass
                
                if spot_price <= 0:
                    try:
                        eq_sub = full_scrip_df[(full_scrip_df['name'].str.upper() == clean_active_symbol) & (full_scrip_df['exch_seg'] == 'NSE')].copy()
                        if not eq_sub.empty:
                            exact_match = eq_sub[eq_sub['symbol'].str.upper().isin([clean_active_symbol, clean_active_symbol + "-EQ"])]
                            target_row = exact_match.iloc[0] if not exact_match.empty else eq_sub.iloc[0]
                            eq_tok = str(target_row['token'])
                            eq_exch = str(target_row['exch_seg'])
                            eq_quote = smart_api.getMarketData("FULL", {eq_exch: [eq_tok]})
                            if eq_quote and eq_quote.get('status') and eq_quote.get('data') and eq_quote['data'].get('fetched'):
                                s_val = float(eq_quote['data']['fetched'][0].get('ltp', 0))
                                if s_val > 0: spot_price = s_val
                    except Exception: pass

                if spot_price <= 0:
                    try:
                        fut_sub = full_scrip_df[(full_scrip_df['name'].str.upper() == clean_active_symbol) & (full_scrip_df['instrumenttype'].isin(['FUTSTK', 'FUTIDX']))]
                        if not fut_sub.empty:
                            fut_tok = str(fut_sub['token'].values[0])
                            fut_quote = smart_api.getMarketData("FULL", {"NFO": [fut_tok]})
                            if fut_quote and fut_quote.get('status') and fut_quote.get('data') and fut_quote['data'].get('fetched'):
                                s_val = float(fut_quote['data']['fetched'][0].get('ltp', 0))
                                if s_val > 0: spot_price = s_val
                    except Exception: pass

                if spot_price <= 0:
                    all_strikes_list = sorted(current_expiry_options['strike'].unique())
                    spot_price = float(all_strikes_list[len(all_strikes_list) // 2]) if all_strikes_list else 100.0

                all_master_strikes = sorted(current_expiry_options['strike'].unique())
                closest_idx = min(range(len(all_master_strikes)), key=lambda i: abs(all_master_strikes[i] - spot_price))
                strikes = all_master_strikes[max(0, closest_idx - 12):min(len(all_master_strikes), closest_idx + 13)]

                filtered_opts = current_expiry_options[current_expiry_options['strike'].isin(strikes)].copy()
                exch_map = {}
                for _, row in filtered_opts.iterrows():
                    exch, tok = str(row['exch_seg']).strip(), str(row['token']).strip()
                    if exch not in exch_map: exch_map[exch] = []
                    exch_map[exch].append(tok)

                all_dash_quotes = []
                for exch, t_list in exch_map.items():
                    for i in range(0, len(t_list), 40):
                        try:
                            res = smart_api.getMarketData("FULL", {exch: t_list[i:i+40]})
                            if res and res.get('status') and res.get('data'): all_dash_quotes.extend(res['data']['fetched'])
                        except Exception: pass

                dash_quotes_dict = {}
                for item in all_dash_quotes:
                    tok = str(item.get('symbolToken', item.get('token', ''))).strip()
                    ltp = float(item.get('ltp', 0))
                    volume = float(item.get('tradeVolume', item.get('volume', 0)))
                    oi = float(item.get('opnInterest', item.get('oi', 0)))
                    change = float(item.get('netChange', item.get('change', 0)))
                    oi_chg = float(item.get('opnInterestChange', item.get('oiChange', 0)))
                    if ltp > 0 or oi > 0: 
                        persistent_quotes_cache[tok] = {'ltp': ltp, 'volume': volume, 'oi': oi, 'net_change': change, 'oi_chg': oi_chg}

                for tok, q_data in persistent_quotes_cache.items():
                    if now_dt.time() >= datetime.strptime("09:15:00", "%H:%M:%S").time() and tok not in morning_oi_dict and q_data['oi'] > 0: morning_oi_dict[tok] = q_data['oi']
                    base_oi = morning_oi_dict.get(tok, q_data['oi'])
                    calc_oi_chg = q_data['oi'] - base_oi if q_data['oi'] > 0 else q_data.get('oi_chg', 0)
                    dash_quotes_dict[tok] = {'ltp': q_data['ltp'], 'volume': q_data['volume'], 'oi': q_data['oi'], 'oi_chg': calc_oi_chg, 'net_change': q_data['net_change']}

                t = max((pd.Timestamp(current_expiry_options['parsed_expiry'].iloc[0]).replace(hour=15, minute=30) - datetime.now()).total_seconds() / (365.0 * 24 * 3600), 0.0001)

                raw_grid_payload = []

                for st in strikes:
                    ce_sub = current_expiry_options[(current_expiry_options['strike'] == st) & (current_expiry_options['symbol'].str.upper().str.endswith('CE'))]
                    pe_sub = current_expiry_options[(current_expiry_options['strike'] == st) & (current_expiry_options['symbol'].str.upper().str.endswith('PE'))]
                    ce_token = str(ce_sub['token'].values[0]).strip() if not ce_sub.empty else ""
                    pe_token = str(pe_sub['token'].values[0]).strip() if not pe_sub.empty else ""
                    
                    ce_q = dash_quotes_dict.get(ce_token, {'ltp': 0, 'volume': 0, 'oi': 0, 'oi_chg': 0, 'net_change': 0})
                    pe_q = dash_quotes_dict.get(pe_token, {'ltp': 0, 'volume': 0, 'oi': 0, 'oi_chg': 0, 'net_change': 0})

                    ce_gks = calculate_iv_and_greeks('CE', spot_price, st, t, 0.07, ce_q['ltp'])
                    pe_gks = calculate_iv_and_greeks('PE', spot_price, st, t, 0.07, pe_q['ltp'])
                    
                    raw_grid_payload.append({
                        'strike': st,
                        'ce_oi': ce_q['oi'], 'ce_vol': ce_q['volume'], 'pe_vol': pe_q['volume'], 'pe_oi': pe_q['oi'],
                        'ce_vega': ce_gks['vega'], 'ce_theta': ce_gks['theta'], 'ce_gamma': ce_gks['gamma'], 'ce_delta': ce_gks['delta'], 'ce_iv': ce_gks['iv'] if ce_gks['iv'] > 1 else 0,
                        'ce_oi_chg': ce_q['oi_chg'], 'ce_change': ce_q['net_change'], 'ce_ltp': ce_q['ltp'],
                        'pe_ltp': pe_q['ltp'], 'pe_change': pe_q['net_change'], 'pe_oi_chg': pe_q['oi_chg'],
                        'pe_iv': pe_gks['iv'] if pe_gks['iv'] > 1 else 0, 'pe_delta': pe_gks['delta'], 'pe_gamma': pe_gks['gamma'], 'pe_theta': pe_gks['theta'], 'pe_vega': pe_gks['vega'],
                        'is_itm_ce': bool(st < spot_price), 'is_itm_pe': bool(st > spot_price)
                    })

                execute_python_scenario_tracker(raw_rows=raw_grid_payload, spot_price=spot_price, lot_size=lot_size, active_expiry=current_expiry, symbol_name=clean_active_symbol)
    except Exception: pass

def initialize_scrip_master(smart_api):
    global full_scrip_df, options_cache, symbol_expiries_cache
    try:
        url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
        resp = requests.get(url, timeout=30)
        if resp.status_code == 200:
            scrip_data = resp.json(); full_scrip_df = pd.DataFrame(scrip_data)
            if 'symboltoken' in full_scrip_df.columns and 'token' not in full_scrip_df.columns: full_scrip_df['token'] = full_scrip_df['symboltoken']
            if 'tradingsymbol' in full_scrip_df.columns and 'symbol' not in full_scrip_df.columns: full_scrip_df['symbol'] = full_scrip_df['tradingsymbol']
            if 'strike' in full_scrip_df.columns: full_scrip_df['strike'] = pd.to_numeric(full_scrip_df['strike'], errors='coerce').fillna(0) / 100.0
            
            fno_opts = full_scrip_df[(full_scrip_df['strike'] > 0) & ((full_scrip_df['exch_seg'] == 'NFO') | (full_scrip_df['exch_seg'] == 'MCX'))].copy()
            fno_opts['strike'] = pd.to_numeric(fno_opts['strike'], errors='coerce')
            fno_opts['parsed_expiry'] = pd.to_datetime(fno_opts['expiry'], format='%d%b%Y', errors='coerce').fillna(pd.to_datetime(fno_opts['expiry'], errors='coerce'))
            
            today_dt = pd.Timestamp(datetime.now().date())
            for name, group in fno_opts.groupby(fno_opts['name'].str.upper()):
                options_cache[name.strip()] = group
                valid_group = group[group['parsed_expiry'] >= today_dt].copy()
                valid_group_sorted = valid_group.sort_values(by='parsed_expiry')
                unique_expiries = []
                for exp_dt in valid_group_sorted['parsed_expiry'].unique():
                    formatted_exp = pd.Timestamp(exp_dt).strftime('%d%b%Y').upper()
                    if formatted_exp not in unique_expiries:
                        unique_expiries.append(formatted_exp)
                symbol_expiries_cache[name.strip()] = unique_expiries
            print(f"✅ Scrip Master Loaded for {len(options_cache)} Symbols!")
    except Exception as e: print(f"⚠ Scrip Master Error: {e}")

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Arush LTP Calculator</title>
    <style>
        body { font-family: 'Segoe UI', Arial, sans-serif; background: #ffffff; color: #000000; margin: 0; padding: 10px; }
        .header { display: flex; justify-content: space-between; align-items: center; background: #f1f3f5; padding: 10px 15px; border-radius: 8px; border: 1px solid #ced4da; }
        .controls-group { display: flex; gap: 15px; align-items: center; }
        .lot-size-badge { background: #1c7ed6; color: #ffffff; padding: 6px 12px; border-radius: 5px; font-weight: bold; font-size: 14px; }
        select { padding: 8px; font-size: 16px; background: #ffffff; border: 1px solid #ced4da; border-radius: 5px; }
        
        .status-box { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-top: 8px; }
        .card { padding: 8px 10px; border-radius: 6px; border: 1px solid #ced4da; text-align: center; font-size: 11px; font-weight: bold; color: #000000; display: flex; flex-direction: column; justify-content: center; align-items: center; min-height: 42px; box-shadow: 0 1px 2px rgba(0,0,0,0.03); }
        
        .card-ce { background: #e8f5e9; border-color: #a5d6a7; }
        .card-pe { background: #ffebee; border-color: #ef9a9a; }
        
        .yellow-pill-badge { 
            background: #ffd43b; 
            color: #000000; 
            font-size: 11px; 
            font-weight: 700; 
            padding: 2px 10px; 
            border-radius: 12px; 
            border: 1px solid #fab005; 
            box-shadow: 0 1px 2px rgba(0,0,0,0.08); 
            display: inline-block; 
            margin-top: 4px; 
        }

        .k3-banner { background: #e6fcf5; padding: 10px; margin-top: 8px; text-align: center; font-size: 16px; font-weight: bold; border-radius: 6px; color: #0ca678; border: 1px solid #20c997; }
        .table-container { margin-top: 10px; overflow-x: auto; max-height: 700px; border: 1px solid #dee2e6; border-radius: 6px; position: relative; }
        table { width: 100%; border-collapse: collapse; background: #ffffff; font-size: 11px; text-align: center; }
        th, td { border: 1px solid #dee2e6; padding: 5px 6px; white-space: nowrap; position: relative; }
        th { background: #f8f9fa; color: #212529; position: sticky; top: 0; z-index: 10; }
        .strike-col { background: #ff922b !important; color: #ffffff !important; font-weight: bold; font-size: 12px; }
        
        .ce-oi-col, .ce-vol-col, .pe-vol-col, .pe-oi-col {
            min-width: 87px !important;
            width: 87px !important;
        }

        .spot-row td { background: #ffffff !important; padding: 0px !important; height: 2px; }
        .spot-row td::after { 
            content: ""; 
            position: absolute; 
            left: 0; 
            right: 0; 
            top: 50%; 
            height: 3px; 
            background-color: #f03e3e; 
            z-index: 2; 
            transform: translateY(-50%);
        }
        
        .spot-row td.spot-price-cell::after { display: none !important; }
        .spot-price-cell { 
            background: #ffffff !important; 
            text-align: center; 
            z-index: 4; 
            padding: 2px 2px !important;
            border-left: 2px solid #dee2e6 !important;
            border-right: 2px solid #dee2e6 !important;
        }
        
        .spot-tag-red { 
            background: #e53935; 
            color: #ffffff; 
            font-size: 9px; 
            font-weight: 800; 
            padding: 1px 6px; 
            border-radius: 10px; 
            display: inline-block; 
            margin-bottom: 1px;
            letter-spacing: 0.5px;
        }
        .spot-value-green {
            color: #2e7d32;
            font-weight: 800;
            font-size: 13px;
            display: block;
        }

        .spot-badges-wrapper {
            position: absolute;
            left: 50%;
            top: 50%;
            transform: translate(-50%, -50%);
            z-index: 8;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 2px;
            pointer-events: none;
            width: max-content;
        }

        .state-badge-pill {
            background: #ffffff;
            border: 1.8px solid #333333;
            border-radius: 12px;
            padding: 2px 7px;
            display: inline-flex;
            align-items: center;
            gap: 4px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.15);
            font-size: 11px;
            font-weight: 700;
            white-space: nowrap;
        }
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
        .pct-sub { font-size: 10px; font-weight: bold; display: block; color: #000000; margin-top: 2px; }
        .chg-pos { color: #2b8a3e; font-weight: bold; }
        .chg-neg { color: #c92a2a; font-weight: bold; }
    </style>
</head>
<body>
    <div class="header">
        <h2>⚡ Arush LTP Calculator</h2>
        <div class="controls-group">
            <div class="lot-size-badge" id="lotSizeDisplay">Lot Size: -</div>
            <select id="symbolSelect" onchange="onSymbolChange()"></select>
            <select id="expirySelect" onchange="updateSelectedSymbolSheet()"></select>
        </div>
    </div>
    <div class="k3-banner" id="k3Display">Loading Framework...</div>
    <div class="status-box">
        <div class="card card-ce"><div id="d5Val">-</div></div>
        <div class="card card-ce"><div id="h5Val">-</div></div>
        <div class="card card-pe"><div id="q5Val">-</div></div>
        <div class="card card-pe"><div id="u5Val">-</div></div>
    </div>
    <div class="table-container">
        <table id="optionChainTable">
            <thead>
                <tr>
                    <th colspan="10" style="background: #e7f5ff; color: #1c7ed6; font-size: 13px;">CALLS (CE)</th>
                    <th class="strike-col">STRIKE</th>
                    <th colspan="10" style="background: #fff5f5; color: #f03e3e; font-size: 13px;">PUTS (PE)</th>
                </tr>
                <tr>
                    <th>Vega</th><th>Theta</th><th>Gamma</th><th>Delta</th><th>IV</th>
                    <th>OI Chg</th><th class="ce-oi-col">OI</th><th class="ce-vol-col">Vol</th><th>Change</th><th>LTP</th>
                    <th class="strike-col">Price</th>
                    <th>LTP</th><th>Change</th><th class="pe-vol-col">Vol</th><th class="pe-oi-col">OI</th><th>OI Chg</th>
                    <th>IV</th><th>Delta</th><th>Gamma</th><th>Theta</th><th>Vega</th>
                </tr>
            </thead>
            <tbody id="gridBody">
                <tr><td colspan="21" style="padding: 20px; font-size: 14px; color: #666;">Syncing option chain matrix...</td></tr>
            </tbody>
        </table>
    </div>
    <script>
        let allExpiriesData = {};
        function initApp() {
            fetch('/api/init-meta').then(res => res.json()).then(meta => {
                allExpiriesData = meta.expiries;
                let symSelect = document.getElementById("symbolSelect");
                symSelect.innerHTML = "";
                
                let priorityIndices = ["NIFTY", "BANKNIFTY", "FINNIFTY"];
                let sortedSymbols = meta.symbols.sort((a, b) => {
                    let aIdx = priorityIndices.indexOf(a);
                    let bIdx = priorityIndices.indexOf(b);
                    if (aIdx !== -1 && bIdx !== -1) return aIdx - bIdx;
                    if (aIdx !== -1) return -1;
                    if (bIdx !== -1) return 1;
                    return a.localeCompare(b);
                });

                sortedSymbols.forEach(sym => {
                    let opt = document.createElement("option"); opt.value = sym; opt.innerText = sym; symSelect.appendChild(opt);
                });
                
                if(meta.symbols.length > 0) {
                    symSelect.value = meta.active_symbol || meta.symbols[0];
                    onSymbolChange();
                }
            });
        }
        function onSymbolChange() {
            let sym = document.getElementById("symbolSelect").value;
            let expSelect = document.getElementById("expirySelect");
            expSelect.innerHTML = "";
            let list = allExpiriesData[sym] || [];
            list.forEach(exp => {
                let opt = document.createElement("option"); opt.value = exp; opt.innerText = exp; expSelect.appendChild(opt);
            });
            updateSelectedSymbolSheet();
        }
        function updateSelectedSymbolSheet() {
            let sym = document.getElementById("symbolSelect").value;
            let exp = document.getElementById("expirySelect").value;
            if(!sym) return;
            fetch('/api/set-symbol/' + sym + (exp ? '/' + exp : ''), {method: 'POST'}).then(() => fetchData());
        }

        function createSinglePillBadgeHtml(badgeObj) {
            if (!badgeObj || !badgeObj.show_badge) return "";
            let cls = badgeObj.state === 'WTT' ? 'state-wtt' : (badgeObj.state === 'WTB' ? 'state-wtb' : 'state-strong');
            let pctColor = badgeObj.state === 'WTB' ? '#c62828' : '#2e7d32';
            return `<div class="state-badge-pill ${cls}"><b>${badgeObj.state}</b> <span style="font-weight:800; color:${pctColor};">${badgeObj.pct}%</span></div>`;
        }

        function fetchData() {
            let sym = document.getElementById("symbolSelect").value;
            if(!sym) return;
            fetch('/api/data/' + sym + '?t=' + new Date().getTime()).then(res => res.json()).then(data => {
                document.getElementById("lotSizeDisplay").innerText = "Lot Size: " + data.lot_size;
                
                document.getElementById("d5Val").innerHTML = data.D5;
                document.getElementById("h5Val").innerHTML = data.H5;
                document.getElementById("q5Val").innerHTML = data.Q5;
                document.getElementById("u5Val").innerHTML = data.U5;
                
                let k3Div = document.getElementById("k3Display");
                k3Div.innerText = data.K3;
                let k3TextLower = data.K3.toLowerCase();
                if(k3TextLower.includes("bull")) {
                    k3Div.style.color = "#0000FF";
                } else if(k3TextLower.includes("bear") || k3TextLower.includes("bloodbath")) {
                    k3Div.style.color = "#FF0000";
                } else {
                    k3Div.style.color = "#000000";
                }

                let tbody = document.getElementById("gridBody"); tbody.innerHTML = "";
                if (data.grid_rows && data.grid_rows.length > 0) {
                    data.grid_rows.forEach(row => {
                        let tr = document.createElement("tr");
                        if (row.is_spot_row) {
                            tr.classList.add("spot-row");
                            let renderSpotValue = row.spot_price || data.spot_price || 0;
                            
                            let ceBadges = [];
                            let ceOiHtml = createSinglePillBadgeHtml(row.ce_oi_badge);
                            let ceVolHtml = createSinglePillBadgeHtml(row.ce_vol_badge);
                            if(ceOiHtml) ceBadges.push(ceOiHtml);
                            if(ceVolHtml) ceBadges.push(ceVolHtml);

                            let peBadges = [];
                            let peVolHtml = createSinglePillBadgeHtml(row.pe_vol_badge);
                            let peOiHtml = createSinglePillBadgeHtml(row.pe_oi_badge);
                            if(peVolHtml) peBadges.push(peVolHtml);
                            if(peOiHtml) peBadges.push(peOiHtml);

                            let ceOverlay = ceBadges.length > 0 ? `<div class="spot-badges-wrapper">${ceBadges.join('')}</div>` : '';
                            let peOverlay = peBadges.length > 0 ? `<div class="spot-badges-wrapper">${peBadges.join('')}</div>` : '';

                            tr.innerHTML = `<td></td><td></td><td></td><td></td><td></td><td></td><td colspan="2" style="position:relative;">${ceOverlay}</td><td></td><td></td><td class="spot-price-cell"><span class="spot-tag-red">SPOT</span><span class="spot-value-green">${renderSpotValue}</span></td><td></td><td></td><td colspan="2" style="position:relative;">${peOverlay}</td><td></td><td></td><td></td><td></td><td></td><td></td>`;
                        } else {
                            tr.innerHTML = `<td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_vega}</td><td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_theta}</td><td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_gamma}</td><td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_delta}</td><td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_iv}</td><td class="${row.ce_oi_chg >= 0 ? 'chg-pos' : 'chg-neg'} ${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_oi_chg}</td><td class="ce-oi-col ${row.ce_oi_color}">${row.ce_oi}<span class="pct-sub">(${row.ce_oi_pct}%)</span></td><td class="ce-vol-col ${row.ce_vol_color}">${row.ce_vol}<span class="pct-sub">(${row.ce_vol_pct}%)</span></td><td class="${row.ce_change >= 0 ? 'chg-pos' : 'chg-neg'} ${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_change}</td><td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_ltp}</td><td class="strike-col">${row.strike}</td><td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_ltp}</td><td class="${row.pe_change >= 0 ? 'chg-pos' : 'chg-neg'} ${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_change}</td><td class="pe-vol-col ${row.pe_vol_color}">${row.pe_vol}<span class="pct-sub">(${row.pe_vol_pct}%)</span></td><td class="pe-oi-col ${row.pe_oi_color}">${row.pe_oi}<span class="pct-sub">(${row.pe_oi_pct}%)</span></td><td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_oi_chg}</td><td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_iv}</td><td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_delta}</td><td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_gamma}</td><td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_theta}</td><td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_vega}</td>`;
                        }
                        tbody.appendChild(tr);
                    });
                }
            });
        }
        setInterval(fetchData, 3000); window.onload = initApp;
    </script>
</body>
</html>
"""

@app.route('/api/init-meta')
def get_initial_metadata():
    global current_active_symbol_ui
    return jsonify({"symbols": sorted(list(options_cache.keys())) if options_cache else ["NIFTY", "BANKNIFTY"], "expiries": symbol_expiries_cache, "active_symbol": current_active_symbol_ui})

@app.route('/api/set-symbol/<symbol>', defaults={'expiry': None}, methods=['POST'])
@app.route('/api/set-symbol/<symbol>/<expiry>', methods=['POST'])
def set_symbol_api(symbol, expiry):
    global user_selected_expiries, current_active_symbol_ui, smart_api_global
    sym = symbol.upper()
    current_active_symbol_ui = sym
    try:
        if expiry:
            user_selected_expiries[sym] = expiry.upper()
        # ⚡ INSTANT PRIORITY FETCH ON SYMBOL CHANGE
        if smart_api_global and sym in options_cache:
            fetch_symbol_data(smart_api_global, sym)
    except Exception: pass
    return jsonify({"status": "success"})

@app.route('/api/data/<symbol>')
def get_symbol_runtime_data(symbol):
    sym = symbol.upper()
    vault = multi_symbol_runtime_states.get(sym, {})
    return jsonify({
        "lot_size": vault.get("lot_size", 1),
        "D5": vault.get("D5_TEXT", "<div>-----</div>"),
        "H5": vault.get("H5_TEXT", "<div>-----</div>"),
        "Q5": vault.get("Q5_TEXT", "<div>-----</div>"),
        "U5": vault.get("U5_TEXT", "<div>-----</div>"),
        "K3": vault.get("K3_TEXT", f"{sym} is Neutral"),
        "grid_rows": vault.get("grid_rows", []),
        "spot_price": vault.get("spot_price", 0)
    })

def run_bot_lock_loop():
    global last_session_date, current_active_symbol_ui, options_cache, smart_api_global
    print("🚀 Starting Instant Priority Engine with 9:15 AM Timestamp Lock...")
    try: smart_api = SmartConnect(api_key=API_KEY); smart_api_global = smart_api
    except Exception as e: print(f"❌ Init Error: {e}"); return
    
    symbol_index = 0
    while True:
        try:
            now_dt = datetime.now()
            today_date_str = now_dt.strftime("%Y-%m-%d")
            
            if last_session_date != today_date_str and now_dt.hour >= 9:
                totp = pyotp.TOTP(TOTP_SECRET.replace(" ", "")).now() if TOTP_SECRET else "123456"
                session_res = smart_api.generateSession(CLIENT_ID, PIN, totp)
                if session_res and session_res.get('status'):
                    last_session_date = today_date_str
                    initialize_scrip_master(smart_api)
            
            if not options_cache:
                totp = pyotp.TOTP(TOTP_SECRET.replace(" ", "")).now() if TOTP_SECRET else "123456"
                if smart_api.generateSession(CLIENT_ID, PIN, totp).get('status'):
                    last_session_date = today_date_str
                    initialize_scrip_master(smart_api)
            
            # 1. PRIORITY FETCH: Active symbol updates instantly
            active_sym = current_active_symbol_ui
            if active_sym and options_cache:
                fetch_symbol_data(smart_api, active_sym)

            # 2. BACKGROUND ROUND-ROBIN: Other symbols update smoothly one by one
            all_syms = list(options_cache.keys())
            if all_syms:
                if symbol_index >= len(all_syms):
                    symbol_index = 0
                bg_sym = all_syms[symbol_index]
                symbol_index += 1
                if bg_sym != active_sym:
                    fetch_symbol_data(smart_api, bg_sym)

        except Exception:
            pass
        time.sleep(0.5)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    run_bot_lock_loop()
