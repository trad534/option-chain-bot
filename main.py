import time
import os
import json
import requests
import pandas as pd
import numpy as np
import pyotp
import gspread
import gspread.utils
from scipy.stats import norm
from scipy.optimize import brentq
from datetime import datetime
from SmartApi import SmartConnect
from flask import Flask, jsonify, render_template_string
import threading
import builtins

RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://onrender.com")

def self_ping():
    while True:
        try:
            if RENDER_URL:
                requests.get(RENDER_URL)
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

GOOGLE_SHEET_NAME = "6 LTP CALCULATOR"
MAIN_SHEET_NAME = "upstox get data"
SYMBOL_SHEET_NAME = "Option chain index new"

try:
    gc = gspread.service_account(filename="Credentials.json") if os.path.exists("Credentials.json") else gspread.service_account_from_dict(json.loads(os.getenv("GOOGLE_CREDENTIALS", "{}")))
    spreadsheet = gc.open(GOOGLE_SHEET_NAME)
    data_sheet = spreadsheet.worksheet(MAIN_SHEET_NAME)
    dashboard_sheet = spreadsheet.worksheet(SYMBOL_SHEET_NAME)
    print("✅ SUCCESS: Google Sheet Connected Successfully!")
except Exception as e:
    print(f"❌ Google Sheet Connection Error: {e}")

script_memory = {}
global_run_date = ""
last_written_values = {}
persistent_quotes_cache = {}
multi_symbol_runtime_states = {}
symbol_expiries_cache = {}
options_cache = {}
eq_cache = {}
full_scrip_df = pd.DataFrame()
morning_oi_dict = {}
baseline_date = None
current_active_symbol_for_memory = "NIFTY"
last_session_date = None

def get_memory(key, default=""):
    global current_active_symbol_for_memory
    return script_memory.get(f"{current_active_symbol_for_memory}_{key}", default)

def set_memory(key, value):
    global current_active_symbol_for_memory
    script_memory[f"{current_active_symbol_for_memory}_{key}"] = str(value)

def safe_update_cell(sheet, cell_name, new_value):
    global last_written_values
    if not sheet: return
    cache_key = f"{sheet.title}_{cell_name}"
    str_val = str(new_value)
    if last_written_values.get(cache_key) != str_val:
        try:
            sheet.update_acell(cell_name, str_val)
            last_written_values[cache_key] = str_val
            time.sleep(0.1)
        except Exception as e:
            print(f"⚠ Cell Update Error: {e}")

def update_k3_with_color(spreadsheet_obj, sheet, cell_name, text_value):
    if not sheet or not spreadsheet_obj: return
    try:
        sheet.update_acell(cell_name, text_value)
        text_lower = text_value.lower()
        if "bull" in text_lower: color_dict = {"red": 0.0, "green": 0.0, "blue": 1.0}
        elif "bear" in text_lower or "bloodbath" in text_lower: color_dict = {"red": 1.0, "green": 0.0, "blue": 0.0}
        else: color_dict = {"red": 0.0, "green": 0.0, "blue": 0.0}

        row, col = gspread.utils.a1_to_rowcol(cell_name)
        body = {"requests": [{"repeatCell": {"range": {"sheetId": sheet.id, "startRowIndex": row - 1, "endRowIndex": row, "startColumnIndex": col - 1, "endColumnIndex": col}, "cell": {"userEnteredFormat": {"textFormat": {"foregroundColor": color_dict}}}, "fields": "userEnteredFormat.textFormat.foregroundColor"}}]}
        spreadsheet_obj.batch_update(body)
    except Exception as e:
        print(f"⚠ K3 Color Error: {e}")

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

def execute_python_scenario_tracker(raw_rows=None, spot_price=0, lot_size=1, active_expiry=""):
    global global_run_date, multi_symbol_runtime_states, current_active_symbol_for_memory
    try:
        now_dt = datetime.now()
        today_str = now_dt.strftime("%Y-%m-%d")
        currentTimeStr = now_dt.strftime("%I:%M %p").lower()

        if global_run_date != today_str:
            script_memory.clear()
            global_run_date = today_str

        active_symbol_name = "NIFTY"
        try:
            if dashboard_sheet:
                j4_val = str(dashboard_sheet.cell(4, 10).value).strip()
                if j4_val and j4_val not in ["NONE", "NAN", "0", ""]:
                    active_symbol_name = j4_val.replace("-EQ", "").replace(".NS", "").strip().upper()
        except Exception: pass

        current_active_symbol_for_memory = active_symbol_name
        if not raw_rows: return

        ceVolArray, ceOiArray, peVolArray, peOiArray = [], [], [], []
        for idx, r_dict in enumerate(raw_rows):
            row_idx = 8 + idx
            strike = float(r_dict['strike'])
            ceOiArray.append({'strike': strike, 'val': float(r_dict['ce_oi']), 'row': row_idx})
            ceVolArray.append({'strike': strike, 'val': float(r_dict['ce_vol']), 'row': row_idx})
            peVolArray.append({'strike': strike, 'val': float(r_dict['pe_vol']), 'row': row_idx})
            peOiArray.append({'strike': strike, 'val': float(r_dict['pe_oi']), 'row': row_idx})

        def extract_top_two(data_array):
            if not data_array: return {'highest': {'strike': 0, 'val': 0, 'row': 0}, 'second': {'strike': 0, 'val': 0, 'row': 0}, 'pct': 0}
            filtered = [item for item in data_array if item['val'] > 0]
            if not filtered: return {'highest': {'strike': 0, 'val': 0, 'row': 0}, 'second': {'strike': 0, 'val': 0, 'row': 0}, 'pct': 0}
            
            sorted_items = sorted(filtered, key=lambda x: x['val'], reverse=True)
            highest = sorted_items[0]
            
            second = {'strike': highest['strike'], 'val': 0, 'row': highest['row']}
            for item in sorted_items:
                if item['strike'] != highest['strike']:
                    second = item
                    break
            if second['val'] == 0 and len(sorted_items) > 1:
                second = sorted_items[1]

            pct = (second['val'] / highest['val']) * 100 if highest['val'] > 0 else 0
            return {'highest': highest, 'second': second, 'pct': pct}

        ceVolTop = extract_top_two(ceVolArray)
        ceOiTop = extract_top_two(ceOiArray)
        peVolTop = extract_top_two(peVolArray)
        peOiTop = extract_top_two(peOiArray)

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
        if ceVolTop['highest']['strike'] == ceOiTop['highest']['strike'] and ceOiRawState == "wtb" and ceVolRawState == "wtb": ceWinner = "BOTH"
        elif ceVolTop['highest']['strike'] == ceOiTop['highest']['strike'] and ceOiIsStable100 and ceVolIsStable100: ceWinner = "BOTH"
        elif ceVolTop['highest']['strike'] == ceOiTop['highest']['strike'] and ceOiIsStable100 and not ceVolIsStable100 and ceVolTop['second']['strike'] > ceVolTop['highest']['strike']: ceWinner = "OI"
        elif ceVolTop['highest']['strike'] == ceOiTop['highest']['strike'] and ceVolIsStable100 and not ceOiIsStable100 and ceOiTop['second']['strike'] > ceVolTop['highest']['strike']: ceWinner = "VOL"
        else:
            ceOiActiveRow = ceOiTop['highest']['row']
            if ceOiTop['second']['row'] > 0 and ceOiTop['pct'] >= 75.0: ceOiActiveRow = min(ceOiActiveRow, ceOiTop['second']['row'])
            ceVolActiveRow = ceVolTop['highest']['row']
            if ceVolTop['second']['row'] > 0 and ceVolTop['pct'] >= 75.0: ceVolActiveRow = min(ceVolActiveRow, ceVolTop['second']['row'])
            if ceVolActiveRow != ceOiActiveRow: ceWinner = "VOL" if ceVolActiveRow < ceOiActiveRow else "OI"
            else: ceWinner = "VOL" if ceVolTop['highest']['row'] <= ceOiTop['highest']['row'] else "OI"

        peWinner = "VOL"
        if peVolTop['highest']['strike'] == peOiTop['highest']['strike'] and peOiRawState == "wtt" and peVolRawState == "wtt": peWinner = "BOTH"
        elif peVolTop['highest']['strike'] == peOiTop['highest']['strike'] and peOiIsStable100 and peVolIsStable100: peWinner = "BOTH"
        else:
            peOiActiveRow = peOiTop['highest']['row']
            if peOiTop['second']['row'] > 0 and peOiTop['pct'] >= 75.0: peOiActiveRow = max(peOiActiveRow, peOiTop['second']['row'])
            peVolActiveRow = peVolTop['highest']['row']
            if peVolTop['second']['row'] > 0 and peVolTop['pct'] >= 75.0: peVolActiveRow = max(peVolActiveRow, peVolTop['second']['row'])
            if peVolActiveRow != peOiActiveRow: ceWinner = "VOL" if peVolActiveRow > peOiActiveRow else "OI"
            else: ceWinner = "VOL" if peVolTop['highest']['row'] >= peOiTop['highest']['row'] else "OI"

        if ceOiRawState in ["wtt", "wtb"]: set_memory("WEAK_HIST_CE_OI", ceOiRawState)
        if ceVolRawState in ["wtt", "wtb"]: set_memory("WEAK_HIST_CE_VOL", ceVolRawState)
        if peOiRawState in ["wtt", "wtb"]: set_memory("WEAK_HIST_PE_OI", peOiRawState)
        if peVolRawState in ["wtt", "wtb"]: set_memory("WEAK_HIST_PE_VOL", peVolRawState)

        def calculateIndependentCell(topObj, cacheKey, sideLabel, isWinner, rawState, finalStatusText):
            if not topObj or topObj['highest']['strike'] == 0:
                return {'text': "---", 'status': "stable strong", 'scenarioStatus': "stable strong"}
            strike = topObj['highest']['strike']
            secondStrike = topObj['second']['strike'] if topObj['second']['val'] > 0 else strike
            prevStrikeStr = get_memory("PREV_STRK_" + cacheKey)
            prevStrike = float(prevStrikeStr) if prevStrikeStr else strike
            if not prevStrikeStr: set_memory("PREV_STRK_" + cacheKey, str(strike)); prevStrike = strike

            displayStatus = finalStatusText; scenarioStatus = finalStatusText
            isShifting = False

            if get_memory("IS_SHIFTED_" + cacheKey) == "true":
                shiftFrom = float(get_memory("SHIFT_FROM_" + cacheKey, 0))
                lastTarget = float(get_memory("SHIFT_TARGET_" + cacheKey, 0))
                if strike == shiftFrom or (lastTarget > 0 and secondStrike != lastTarget):
                    set_memory("IS_SHIFTED_" + cacheKey, "false")
                    set_memory("PREV_STRK_" + cacheKey, str(strike))
                    script_memory.pop(f"{active_symbol_name}_SHIFT_TARGET_" + cacheKey, None)

            if strike != prevStrike and prevStrike > 0 and finalStatusText in ["wtb", "wtt"]:
                shiftType = "top to bottom" if strike < prevStrike else "bottom to top"
                set_memory("IS_SHIFTED_" + cacheKey, "true")
                set_memory("SHIFT_TYPE_" + cacheKey, shiftType)
                set_memory("SHIFT_FROM_" + cacheKey, str(prevStrike))
                set_memory("SHIFT_TARGET_" + cacheKey, str(secondStrike))

            if get_memory("IS_SHIFTED_" + cacheKey) == "true":
                if rawState == "STRONG" or finalStatusText == "stable strong" or "to strong" in finalStatusText:
                    set_memory("IS_SHIFTED_" + cacheKey, "false")
                    set_memory("PREV_STRK_" + cacheKey, str(strike))
                    scenarioStatus = finalStatusText
                else:
                    isShifting = True
                    shiftType = get_memory("SHIFT_TYPE_" + cacheKey)
                    scenarioStatus = "wtb" if shiftType == "top to bottom" else "wtt"
            else:
                set_memory("PREV_STRK_" + cacheKey, str(strike))
                scenarioStatus = finalStatusText

            if not isWinner and rawState != "STRONG":
                displayStatus = "stable strong"; scenarioStatus = "stable strong"

            displayTime = get_memory("TIME_" + cacheKey)
            lastStatus = get_memory("LAST_STATUS_" + cacheKey, "")
            isTrigger = displayStatus in ["wtb", "wtt", "wtb to strong", "wtt to strong"] or isShifting

            if isTrigger and displayStatus != lastStatus:
                displayTime = currentTimeStr
                set_memory("TIME_" + cacheKey, displayTime)
                set_memory("LAST_STATUS_" + cacheKey, displayStatus)
            elif not displayTime:
                displayTime = currentTimeStr
                set_memory("TIME_" + cacheKey, displayTime)
                set_memory("LAST_STATUS_" + cacheKey, displayStatus)

            if isShifting:
                line1 = f"{displayTime}-{sideLabel} - {strike}"
                line2 = f"shifted {get_memory('SHIFT_TYPE_' + cacheKey)}"
                outputText = f"<div>{line1}</div><div class='yellow-pill-badge'>{line2}</div>"
            else:
                if displayStatus == "stable strong":
                    line1 = f"{displayTime}-{sideLabel} - {strike}"
                    line2 = f"stable strong"
                    outputText = f"<div>{line1}</div><div class='yellow-pill-badge'>{line2}</div>"
                elif "to strong" in displayStatus:
                    line1 = f"{displayTime}-{sideLabel} - {strike}"
                    line2 = f"{displayStatus}"
                    outputText = f"<div>{line1}</div><div class='yellow-pill-badge'>{line2}</div>"
                else:
                    weakDir = "weak towards top" if displayStatus == "wtt" else "weak towards bottom"
                    line1 = f"{displayTime}-{sideLabel}-{strike}-{displayStatus}-{secondStrike}"
                    line2 = f"{weakDir}"
                    outputText = f"<div>{line1}</div><div class='yellow-pill-badge'>{line2}</div>"

            return {'text': outputText, 'status': displayStatus, 'scenarioStatus': scenarioStatus}

        ceOiResult  = calculateIndependentCell(ceOiTop, "CE_OI", "Eor OI", ceWinner in ["OI", "BOTH"], ceOiRawState, ceOiStatusText)
        ceVolResult = calculateIndependentCell(ceVolTop, "CE_VOL", "Eor vol", ceWinner in ["VOL", "BOTH"], ceVolRawState, ceVolStatusText)
        peVolResult = calculateIndependentCell(peVolTop, "PE_VOL", "Eos vol", peWinner in ["VOL", "BOTH"], peVolRawState, peVolStatusText)
        peOiResult  = calculateIndependentCell(peOiTop, "PE_OI", "Eos OI", peWinner in ["OI", "BOTH"], peOiRawState, peOiStatusText)

        finalCeState = ceOiResult['scenarioStatus'] if ceWinner == "OI" else ceVolResult['scenarioStatus']
        finalPeState = peOiResult['scenarioStatus'] if ceWinner == "OI" else peVolResult['scenarioStatus']
        if ceWinner == "BOTH": finalCeState = ceVolResult['scenarioStatus'] if "to strong" in ceVolResult['scenarioStatus'] else ceOiResult['scenarioStatus']
        if peWinner == "BOTH": finalPeState = peVolResult['scenarioStatus'] if "to strong" in peVolResult['scenarioStatus'] else peOiResult['scenarioStatus']

        checkCe = finalCeState.lower().strip()
        checkPe = finalPeState.lower().strip()

        k3Text = f"{active_symbol_name} is Neutral"
        if checkCe == "wtt" and checkPe == "wtb": k3Text = f"{active_symbol_name} is 8th scenario both side Risky"
        elif checkCe == "wtb" and checkPe == "wtt": k3Text = f"{active_symbol_name} is 9th scenario both side Risky"
        elif "stable strong" in checkCe and "stable strong" in checkPe: k3Text = f"{active_symbol_name} is Neutral"
        elif "wtb" in checkCe and "stable strong" in checkPe: k3Text = f"{active_symbol_name} is slightly Bearish"
        elif "stable strong" in checkCe and "wtb" in checkPe: k3Text = f"{active_symbol_name} is slightly Bearish"
        elif "wtt" in checkCe and "stable strong" in checkPe: k3Text = f"{active_symbol_name} is slightly bullish"
        elif "stable strong" in checkCe and "wtt" in checkPe: k3Text = f"{active_symbol_name} is slightly bullish"
        elif checkCe == "wtt" and checkPe == "wtt": k3Text = f"{active_symbol_name} is Bullrun"
        elif checkCe == "wtb" and checkPe == "wtb": k3Text = f"{active_symbol_name} is bloodbath"
        elif checkCe == "wtb" and "wtt to strong" in checkPe: k3Text = f"{active_symbol_name} is confirm Bloodbath"
        elif "wtt to strong" in checkCe and checkPe == "wtb": k3Text = f"{active_symbol_name} is confirm Bloodbath"
        elif checkCe == "wtt" and "wtt to strong" in checkPe: k3Text = f"{active_symbol_name} is slightly bullish (Both side trade)"
        elif "wtt to strong" in checkCe and checkPe == "wtt": k3Text = f"{active_symbol_name} is slightly bullish (Both side trade)"
        elif checkCe == "wtt" and "wtb to strong" in checkPe: k3Text = f"{active_symbol_name} is confirm Bull Run"
        elif "wtb to strong" in checkCe and checkPe == "wtt": k3Text = f"{active_symbol_name} is confirm Bull Run"
        elif "wtb to strong" in checkCe and checkPe == "wtb": k3Text = f"{active_symbol_name} is slightly Bearish (Both side trade)"
        elif "wtb" in checkCe and "wtb to strong" in checkPe: k3Text = f"{active_symbol_name} is slightly Bearish (both side trade)"
        elif "wtb to strong" in checkCe and "stable strong" in checkPe: k3Text = f"{active_symbol_name} is slightly Bullish"
        elif "stable strong" in checkCe and "wtb to strong" in checkPe: k3Text = f"{active_symbol_name} is slightly Bullish"
        elif "wtt to strong" in checkCe and "stable strong" in checkPe: k3Text = f"{active_symbol_name} is slightly Bearish"
        elif "stable strong" in checkCe and "wtt to strong" in checkPe: k3Text = f"{active_symbol_name} is slightly Bearish"
        elif "wtt to strong" in checkCe and "wtb to strong" in checkPe: k3Text = f"{active_symbol_name} is 8th scenario both side Risky trade"
        elif "wtb to strong" in checkCe and "wtt to strong" in checkPe: k3Text = f"{active_symbol_name} is 9th scenario both side Risky trade"
        elif "wtb to strong" in checkCe and "wtb to strong" in checkPe: k3Text = f"{active_symbol_name} is confirm Bull Run"
        elif "wtt to strong" in checkCe and "wtt to strong" in checkPe: k3Text = f"{active_symbol_name} is confirm bloodbath"
        elif "wtt to strong" in checkCe and checkPe == "wtt": k3Text = f"{active_symbol_name} is Bullrun High Momentum"
        elif "wtb to strong" in checkCe and checkPe == "wtb": k3Text = f"{active_symbol_name} is Bloodbath High Momentum"

        finalD5 = "---"; finalH5 = "---"; finalU5 = "---"; finalQ5 = "---"
        if ceWinner == "BOTH": finalD5 = ceOiResult['text']; finalH5 = ceVolResult['text']
        elif ceWinner == "VOL": finalH5 = ceVolResult['text']
        elif ceWinner == "OI": finalD5 = ceOiResult['text']

        if peWinner == "BOTH": finalU5 = peOiResult['text']; finalQ5 = peVolResult['text']
        elif peWinner == "VOL": finalQ5 = peVolResult['text']
        elif peWinner == "OI": finalU5 = peOiResult['text']

        if active_symbol_name not in multi_symbol_runtime_states: multi_symbol_runtime_states[active_symbol_name] = {}
        vault = multi_symbol_runtime_states[active_symbol_name]
        vault["D5_TEXT"] = finalD5; vault["H5_TEXT"] = finalH5; vault["U5_TEXT"] = finalU5; vault["Q5_TEXT"] = finalQ5
        vault["K3_TEXT"] = k3Text; vault["lot_size"] = lot_size; vault["spot_price"] = spot_price

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
        
        # Spot price ke sabse qareeb wali strike ka index find karke exact wahan spot insert karenge
        strikes_list = [r['strike'] for r in raw_rows]
        spot_insert_idx = 0
        if strikes_list:
            spot_insert_idx = min(range(len(strikes_list)), key=lambda i: abs(strikes_list[i] - spot_price))
            if spot_price > strikes_list[spot_insert_idx] and spot_insert_idx < len(strikes_list) - 1:
                spot_insert_idx += 1

        for idx, r_dict in enumerate(raw_rows):
            if not spot_inserted and idx == spot_insert_idx:
                grid_rows_built.append({
                    'is_spot_row': True, 
                    'spot_price': round(spot_price, 2)
                })
                spot_inserted = True

            st = r_dict['strike']
            r_copy = r_dict.copy()
            r_copy['ce_oi_pct'] = round((r_dict['ce_oi'] / max_ce_oi) * 100, 2)
            r_copy['ce_vol_pct'] = round((r_dict['ce_vol'] / max_ce_vol) * 100, 2)
            r_copy['pe_vol_pct'] = round((r_dict['pe_vol'] / max_pe_vol) * 100, 2)
            r_copy['pe_oi_pct'] = round((r_dict['pe_oi'] / max_pe_oi) * 100, 2)

            r_copy['ce_oi_color'] = 'bg-100' if st == c_oi_1 else ('bg-75' if st == c_oi_2 and ceOiTop['pct'] >= 75.0 else '')
            r_copy['ce_vol_color'] = 'bg-100' if st == c_vol_1 else ('bg-75' if st == c_vol_2 and ceVolTop['pct'] >= 75.0 else '')
            r_copy['pe_vol_color'] = 'bg-100' if st == p_vol_1 else ('bg-75' if st == p_vol_2 and peVolTop['pct'] >= 75.0 else '')
            r_copy['pe_oi_color'] = 'bg-100' if st == p_oi_1 else ('bg-75' if st == p_oi_2 and peOiTop['pct'] >= 75.0 else '')

            grid_rows_built.append(r_copy)

        if not spot_inserted:
            grid_rows_built.append({
                'is_spot_row': True, 
                'spot_price': round(spot_price, 2)
            })

        vault["grid_rows"] = grid_rows_built
        vault["spot_price"] = round(spot_price, 2)
        safe_update_cell(dashboard_sheet, "D5", finalD5)
        safe_update_cell(dashboard_sheet, "H5", finalH5)
        safe_update_cell(dashboard_sheet, "U5", finalU5)
        safe_update_cell(dashboard_sheet, "Q5", finalQ5)
        update_k3_with_color(spreadsheet, dashboard_sheet, "K3", k3Text)
    except Exception as err:
        print(f"❌ Scenario Tracker Error: {err}")

def fetch_and_update_option_chain(smart_api):
    global morning_oi_dict, baseline_date, options_cache, persistent_quotes_cache, symbol_expiries_cache
    try:
        now_dt = datetime.now()
        current_date_str = now_dt.strftime('%Y-%m-%d')
        if baseline_date != current_date_str: morning_oi_dict = {}; baseline_date = current_date_str
        if not options_cache: initialize_scrip_master(smart_api); return

        raw_selected_symbol = "NIFTY"
        try:
            if dashboard_sheet:
                val = str(dashboard_sheet.cell(4, 10).value).strip().upper()
                if val and val not in ["NONE", "NAN", "0"]: raw_selected_symbol = val
        except Exception: pass
        clean_active_symbol = raw_selected_symbol.replace("-EQ", "").replace(".NS", "").strip()

        if clean_active_symbol in options_cache:
            sym_options = options_cache[clean_active_symbol]
            valid_opts = sym_options[sym_options['parsed_expiry'] >= pd.Timestamp(now_dt.date())].copy()
            if not valid_opts.empty:
                unique_expiries_dt = sorted(valid_opts['parsed_expiry'].unique())
                expiry_strings_sorted = [pd.Timestamp(dt).strftime('%d%b%Y').upper() for dt in unique_expiries_dt]
                active_expiries = expiry_strings_sorted[:4]
                while len(active_expiries) < 4: active_expiries.append(active_expiries[-1] if active_expiries else "N/A")

                current_expiry = active_expiries[0]
                current_expiry_dt = unique_expiries_dt[0]
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
                    except Exception as e:
                        print(f"⚠ Equity Spot Fetch Error: {e}")

                if spot_price <= 0:
                    try:
                        fut_sub = full_scrip_df[(full_scrip_df['name'].str.upper() == clean_active_symbol) & (full_scrip_df['instrumenttype'].isin(['FUTSTK', 'FUTIDX']))]
                        if not fut_sub.empty:
                            fut_tok = str(fut_sub['token'].values[0])
                            fut_quote = smart_api.getMarketData("FULL", {"NFO": [fut_tok]})
                            if fut_quote and fut_quote.get('status') and fut_quote.get('data') and fut_quote['data'].get('fetched'):
                                s_val = float(fut_quote['data']['fetched'][0].get('ltp', 0))
                                if s_val > 0: spot_price = s_val
                    except Exception as e:
                        print(f"⚠ Future Spot Fetch Error: {e}")

                if spot_price <= 0:
                    all_strikes_list = sorted(current_expiry_options['strike'].unique())
                    spot_price = float(all_strikes_list[len(all_strikes_list) // 2]) if all_strikes_list else 100.0

                safe_update_cell(data_sheet, "K9", spot_price)
                safe_update_cell(data_sheet, "L9", str(current_expiry))
                safe_update_cell(data_sheet, "A7", lot_size)

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
                    ltp = float(item.get('ltp', 0)); volume = float(item.get('tradeVolume', 0)); oi = float(item.get('opnInterest', 0)); change = float(item.get('netChange', 0))
                    if ltp > 0 or oi > 0: persistent_quotes_cache[tok] = {'ltp': ltp, 'volume': volume, 'oi': oi, 'net_change': change}

                for tok, q_data in persistent_quotes_cache.items():
                    if now_dt.time() >= datetime.strptime("09:15:00", "%H:%M:%S").time() and tok not in morning_oi_dict and q_data['oi'] > 0: morning_oi_dict[tok] = q_data['oi']
                    base_oi = morning_oi_dict.get(tok, q_data['oi'])
                    dash_quotes_dict[tok] = {'ltp': q_data['ltp'], 'volume': q_data['volume'], 'oi': q_data['oi'], 'oi_chg': q_data['oi'] - base_oi, 'net_change': q_data['net_change']}

                t = max((pd.Timestamp(current_expiry_options['parsed_expiry'].iloc[0]).replace(hour=15, minute=30) - datetime.now()).total_seconds() / (365.0 * 24 * 3600), 0.0001)

                output_rows = [["CE Vega", "CE Theta", "CE Gamma", "CE Delta", "CE IV", "CE OI Chg", "CE OI", "CE Vol", "CE Change", "CE LTP", "Strike Price", "PE LTP", "PE Change", "PE Vol", "PE OI", "PE OI Chg", "PE IV", "PE Delta", "PE Gamma", "PE Theta", "PE Vega"]]
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

                    output_rows.append([ce_gks['vega'], ce_gks['theta'], ce_gks['gamma'], ce_gks['delta'], ce_gks['iv'] if ce_gks['iv'] > 1 else 0, ce_q['oi_chg'], ce_q['oi'], ce_q['volume'], ce_q['net_change'], ce_q['ltp'], st, pe_q['ltp'], pe_q['net_change'], pe_q['volume'], pe_q['oi'], pe_q['oi_chg'], pe_gks['iv'] if pe_gks['iv'] > 1 else 0, pe_gks['delta'], pe_gks['gamma'], pe_gks['theta'], pe_gks['vega']])
                    
                    raw_grid_payload.append({
                        'strike': st,
                        'ce_oi': ce_q['oi'], 'ce_vol': ce_q['volume'], 'pe_vol': pe_q['volume'], 'pe_oi': pe_q['oi'],
                        'ce_vega': ce_gks['vega'], 'ce_theta': ce_gks['theta'], 'ce_gamma': ce_gks['gamma'], 'ce_delta': ce_gks['delta'], 'ce_iv': ce_gks['iv'] if ce_gks['iv'] > 1 else 0,
                        'ce_oi_chg': ce_q['oi_chg'], 'ce_change': ce_q['net_change'], 'ce_ltp': ce_q['ltp'],
                        'pe_ltp': pe_q['ltp'], 'pe_change': pe_q['net_change'], 'pe_oi_chg': pe_q['oi_chg'],
                        'pe_iv': pe_gks['iv'] if pe_gks['iv'] > 1 else 0, 'pe_delta': pe_gks['delta'], 'pe_gamma': pe_gks['gamma'], 'pe_theta': pe_gks['theta'], 'pe_vega': pe_gks['vega'],
                        'is_itm_ce': bool(st < spot_price), 'is_itm_pe': bool(st > spot_price)
                    })

                if len(output_rows) > 1 and data_sheet:
                    data_sheet.update(range_name=f"A10:U{10 + len(output_rows) - 1}", values=output_rows)
                    try: data_sheet.batch_clear([f"A{10 + len(output_rows)}:U100"])
                    except Exception: pass

                execute_python_scenario_tracker(raw_rows=raw_grid_payload, spot_price=spot_price, lot_size=lot_size, active_expiry=current_expiry)
    except Exception as ex: print(f"❌ Fetch Error: {str(ex)}")

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
                valid_exp = sorted(group[group['parsed_expiry'] >= today_dt]['expiry'].unique())
                symbol_expiries_cache[name.strip()] = [str(exp).upper() for exp in valid_exp]
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
        
        .yellow-pill-badge { background: #ffd43b; color: #000000; font-size: 9px; font-weight: 700; padding: 2px 8px; border-radius: 10px; border: 1px solid #fab005; box-shadow: 0 1px 2px rgba(0,0,0,0.1); display: inline-block; margin-top: 3px; }

        .k3-banner { background: #e6fcf5; padding: 10px; margin-top: 8px; text-align: center; font-size: 16px; font-weight: bold; border-radius: 6px; color: #0ca678; border: 1px solid #20c997; }
        .table-container { margin-top: 10px; overflow-x: auto; max-height: 700px; border: 1px solid #dee2e6; border-radius: 6px; }
        table { width: 100%; border-collapse: collapse; background: #ffffff; font-size: 11px; text-align: center; }
        th, td { border: 1px solid #dee2e6; padding: 5px 6px; white-space: nowrap; }
        th { background: #f8f9fa; color: #212529; position: sticky; top: 0; z-index: 10; }
        .strike-col { background: #ff922b !important; color: #ffffff !important; font-weight: bold; font-size: 12px; }
        .spot-row td { background: #ffffff !important; position: relative; padding: 4px; }
        .spot-row td::after { content: ""; position: absolute; left: 0; right: 0; top: 50%; height: 3px; background-color: #f03e3e; z-index: 2; }
        .spot-price-cell { background: #7950f2 !important; color: #ffffff !important; font-weight: bold; font-size: 13px; text-align: center; z-index: 4; border: 2px solid #f03e3e !important; }
        .spot-tag-small { background: #ffffff; color: #7950f2; font-size: 8px; font-weight: bold; padding: 1px 4px; border-radius: 3px; display: block; margin-bottom: 2px; }
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
                    <th>OI Chg</th><th>OI</th><th>Vol</th><th>Change</th><th>LTP</th>
                    <th class="strike-col">Price</th>
                    <th>LTP</th><th>Change</th><th>Vol</th><th>OI</th><th>OI Chg</th>
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
                meta.symbols.forEach(sym => {
                    let opt = document.createElement("option"); opt.value = sym; opt.innerText = sym; symSelect.appendChild(opt);
                });
                if(meta.symbols.length > 0) { symSelect.value = meta.active_symbol || meta.symbols[0]; onSymbolChange(); }
            });
        }
        function onSymbolChange() {
            let sym = document.getElementById("symbolSelect").value;
            let expSelect = document.getElementById("expirySelect");
            expSelect.innerHTML = "";
            (allExpiriesData[sym] || []).forEach(exp => {
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
        function fetchData() {
            let sym = document.getElementById("symbolSelect").value;
            if(!sym) return;
            fetch('/api/data/' + sym + '?t=' + new Date().getTime()).then(res => res.json()).then(data => {
                document.getElementById("lotSizeDisplay").innerText = "Lot Size: " + data.lot_size;
                document.getElementById("d5Val").innerHTML = data.D5;
                document.getElementById("h5Val").innerHTML = data.H5;
                document.getElementById("q5Val").innerHTML = data.Q5;
                document.getElementById("u5Val").innerHTML = data.U5;
                document.getElementById("k3Display").innerText = data.K3;
                let tbody = document.getElementById("gridBody"); tbody.innerHTML = "";
                if (data.grid_rows && data.grid_rows.length > 0) {
                    data.grid_rows.forEach(row => {
                        let tr = document.createElement("tr");
                        if (row.is_spot_row) {
                            tr.classList.add("spot-row");
                            let renderSpotValue = row.spot_price || data.spot_price || 0;
                            tr.innerHTML = `<td></td><td></td><td></td><td></td><td></td><td></td><td></td><td></td><td></td><td></td><td class="strike-col spot-price-cell"><span class="spot-tag-small">SPOT</span>${renderSpotValue}</td><td></td><td></td><td></td><td></td><td></td><td></td><td></td><td></td><td></td>`;
                        } else {
                            tr.innerHTML = `<td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_vega}</td><td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_theta}</td><td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_gamma}</td><td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_delta}</td><td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_iv}</td><td class="${row.ce_oi_chg >= 0 ? 'chg-pos' : 'chg-neg'} ${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_oi_chg}</td><td class="ce-oi-col ${row.ce_oi_color}">${row.ce_oi}<span class="pct-sub">(${row.ce_oi_pct}%)</span></td><td class="ce-vol-col ${row.ce_vol_color}">${row.ce_vol}<span class="pct-sub">(${row.ce_vol_pct}%)</span></td><td class="${row.ce_change >= 0 ? 'chg-pos' : 'chg-neg'} ${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_change}</td><td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_ltp}</td><td class="strike-col">${row.strike}</td><td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_ltp}</td><td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_change}</td><td class="pe-vol-col ${row.pe_vol_color}">${row.pe_vol}<span class="pct-sub">(${row.pe_vol_pct}%)</span></td><td class="pe-oi-col ${row.pe_oi_color}">${row.pe_oi}<span class="pct-sub">(${row.pe_oi_pct}%)</span></td><td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_oi_chg}</td><td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_iv}</td><td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_delta}</td><td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_gamma}</td><td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_theta}</td><td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_vega}</td>`;
                        }
                        tbody.appendChild(tr);
                    });
                }
            });
        }
        setInterval(fetchData, 1000); window.onload = initApp;
    </script>
</body>
</html>
"""

@app.route('/api/init-meta')
def get_initial_metadata():
    active_sym = "NIFTY"
    try:
        if dashboard_sheet:
            val = str(dashboard_sheet.cell(4, 10).value).strip().upper()
            if val and val not in ["NONE", "NAN", "0"]: active_sym = val.replace("-EQ", "").replace(".NS", "").strip()
    except Exception: pass
    return jsonify({"symbols": sorted(list(options_cache.keys())) if options_cache else ["NIFTY", "BANKNIFTY"], "expiries": symbol_expiries_cache, "active_symbol": active_sym})

@app.route('/api/set-symbol/<symbol>', defaults={'expiry': None}, methods=['POST'])
@app.route('/api/set-symbol/<symbol>/<expiry>', methods=['POST'])
def set_symbol_api(symbol, expiry):
    try:
        if dashboard_sheet: dashboard_sheet.update_acell("J4", symbol.upper())
    except Exception: pass
    return jsonify({"status": "success"})

@app.route('/api/data/<symbol>')
def get_symbol_runtime_data(symbol):
    sym = symbol.upper()
    vault = multi_symbol_runtime_states.get(sym, {})
    return jsonify({
        "lot_size": vault.get("lot_size", 1),
        "D5": vault.get("D5_TEXT", "---"),
        "H5": vault.get("H5_TEXT", "---"),
        "Q5": vault.get("Q5_TEXT", "---"),
        "U5": vault.get("U5_TEXT", "---"),
        "K3": vault.get("K3_TEXT", f"{sym} is Neutral"),
        "grid_rows": vault.get("grid_rows", []),
        "spot_price": vault.get("spot_price", 0)
    })

def run_bot_lock_loop():
    global last_session_date
    print("🚀 Starting Engine...")
    try: smart_api = SmartConnect(api_key=API_KEY)
    except Exception as e: print(f"❌ Init Error: {e}"); return
    
    while True:
        try:
            now_dt = datetime.now()
            today_date_str = now_dt.strftime("%Y-%m-%d")
            
            if last_session_date != today_date_str and now_dt.hour >= 9:
                print(f"🔄 Daily Session Refresh for {today_date_str}...")
                totp = pyotp.TOTP(TOTP_SECRET.replace(" ", "")).now() if TOTP_SECRET else "123456"
                session_res = smart_api.generateSession(CLIENT_ID, PIN, totp)
                if session_res and session_res.get('status'):
                    last_session_date = today_date_str
                    initialize_scrip_master(smart_api)
                    print("✅ Daily Session Renewed Successfully!")
            
            if not options_cache:
                totp = pyotp.TOTP(TOTP_SECRET.replace(" ", "")).now() if TOTP_SECRET else "123456"
                if smart_api.generateSession(CLIENT_ID, PIN, totp).get('status'):
                    last_session_date = today_date_str
                    initialize_scrip_master(smart_api)
            
            fetch_and_update_option_chain(smart_api)
        except Exception as err:
            print(f"⚠ Loop Error: {err}")
        time.sleep(1)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    run_bot_lock_loop()
