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

# --- SERVER ALIVE & FLASK CONFIGURATION ---
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

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


# --- CREDENTIALS CONFIGURATION ---
CLIENT_ID = os.getenv("ANGEL_CLIENT_ID")
PIN = os.getenv("ANGEL_PIN")
API_KEY = os.getenv("ANGEL_API_KEY")
TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET")

# --- GLOBAL CORE STORAGE PANELS ---
global_run_date = ""
persistent_quotes_cache = {}
multi_symbol_runtime_states = {}  
symbol_expiries_cache = {}
options_cache = {}
full_scrip_df = pd.DataFrame()
morning_oi_dict = {}
baseline_date = None
current_active_symbol_ui = "NIFTY"  
last_session_date = None
user_selected_expiries = {}
smart_api_global = None

symbol_states_memory = {}

def get_symbol_memory(symbol, key, default=""):
    if symbol not in symbol_states_memory:
        symbol_states_memory[symbol] = {}
    return symbol_states_memory[symbol].get(key, default)

def set_symbol_memory(symbol, key, value):
    if symbol not in symbol_states_memory:
        symbol_states_memory[symbol] = {}
    symbol_states_memory[symbol][key] = str(value)


# --- BLACK-SCHOLES MATHEMATICAL GREEKS ENGINE (FULLY DYNAMIC & MICRON PRECISION) ---
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


# --- GOOGLE SHEETS EXACT LOGIC REPLICATED IN PYTHON ---
def execute_python_scenario_tracker(raw_rows=None, spot_price=0, lot_size=1, active_expiry="", symbol_name="NIFTY"):
    global global_run_date, multi_symbol_runtime_states
    try:
        now_dt = datetime.now()
        today_str = now_dt.strftime("%Y-%m-%d")
        currentTimeStr = now_dt.strftime("%I:%M %p").lower()

        if global_run_date != today_str:
            symbol_states_memory.clear()
            global_run_date = today_str

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

            if idx >= (spot_idx - 3) and idx <= (spot_idx + 18):
                ceOiArray.append({'strike': strike, 'val': float(r_dict.get('ce_oi', 0)), 'row': row_idx})
                ceVolArray.append({'strike': strike, 'val': float(r_dict.get('ce_vol', 0)), 'row': row_idx})

            if idx >= (spot_idx - 18) and idx <= (spot_idx + 3):
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

        ceWinner = "VOL"
        if ceVolTop['highest']['strike'] == ceOiTop['highest']['strike'] and ceOiRawState == "wtb" and ceVolRawState == "wtb":
            ceWinner = "BOTH"
        elif ceVolTop['highest']['strike'] == ceOiTop['highest']['strike'] and not ceOiIsStable100 and not ceVolIsStable100 and (ceOiTop['second']['strike'] > ceOiTop['highest']['strike']) and (ceVolTop['second']['strike'] > ceOiTop['highest']['strike']):
            ceWinner = "BOTH"
        elif ceVolTop['highest']['strike'] == ceOiTop['highest']['strike'] and ceOiIsStable100 and ceVolIsStable100:
            ceWinner = "BOTH"
        elif ceVolTop['highest']['strike'] == ceOiTop['highest']['strike'] and ceOiIsStable100 and not ceVolIsStable100 and (ceVolTop['second'] and ceVolTop['second']['strike'] > ceVolTop['highest']['strike']):
            ceWinner = "OI"
        elif ceVolTop['highest']['strike'] == ceOiTop['highest']['strike'] and ceVolIsStable100 and not ceOiIsStable100 and (ceOiTop['second'] and ceOiTop['second']['strike'] > ceVolTop['highest']['strike']):
            ceWinner = "VOL"
        elif ceOiIsStable100 and not ceVolIsStable100 and (ceVolTop['second'] and ceVolTop['second']['strike'] > ceVolTop['highest']['strike']) and (ceOiTop['highest']['strike'] < ceVolTop['highest']['strike']):
            ceWinner = "OI"
        elif ceVolIsStable100 and not ceOiIsStable100 and (ceVolTop['second'] and ceVolTop['second']['strike'] > ceVolTop['highest']['strike']) and (ceVolTop['highest']['strike'] < ceOiTop['highest']['strike']):
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
        elif peVolTop['highest']['strike'] == peOiTop['highest']['strike'] and peOiIsStable100 and not peVolIsStable100 and (ceOiTop['second'] and ceOiTop['second']['strike'] < peOiTop['highest']['strike']):
            peWinner = "VOL"
        elif peOiIsStable100 and not peVolIsStable100 and (peVolTop['second'] and peVolTop['second']['strike'] < peOiTop['highest']['strike']) and (peOiTop['highest']['strike'] > peVolTop['highest']['strike']):
            peWinner = "OI"
        elif peVolIsStable100 and not peOiIsStable100 and (ceOiTop['second'] and ceOiTop['second']['strike'] < peOiTop['highest']['strike']) and (peVolTop['highest']['strike'] > peOiTop['highest']['strike']):
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

        if ceOiRawState in ["wtt", "wtb"]: set_symbol_memory(symbol_name, "WEAK_HIST_CE_OI", ceOiRawState)
        if ceVolRawState in ["wtt", "wtb"]: set_symbol_memory(symbol_name, "WEAK_HIST_CE_VOL", ceVolRawState)
        if peOiRawState in ["wtt", "wtb"]: set_symbol_memory(symbol_name, "WEAK_HIST_PE_OI", peOiRawState)
        if peVolRawState in ["wtt", "wtb"]: set_symbol_memory(symbol_name, "WEAK_HIST_PE_VOL", peVolRawState)

        def calculateIndependentCell(topObj, cacheKey, sideLabel, isWinner, rawState, finalStatusText):
            if not topObj or not topObj.get('highest') or topObj['highest']['strike'] == 0:
                return {'text': "-----", 'status': "stable strong", 'scenarioStatus': "stable strong", 'rawState': "STRONG"}

            strike = topObj['highest']['strike']
            secondStrike = strike
            if topObj.get('second') and topObj['second']['val'] > 0: secondStrike = topObj['second']['strike']

            strike_str = str(int(strike)) if strike == int(strike) else str(strike)
            second_strike_str = str(int(secondStrike)) if secondStrike == int(secondStrike) else str(secondStrike)

            prevStrikeStr = get_symbol_memory(symbol_name, "PREV_STRK_" + cacheKey)
            prevStrike = float(prevStrikeStr) if prevStrikeStr else strike
            if not prevStrikeStr:
                set_symbol_memory(symbol_name, "PREV_STRK_" + cacheKey, str(strike))
                prevStrike = strike

            displayStatus = finalStatusText
            scenarioStatus = finalStatusText
            isShifting = False
            shiftType = get_symbol_memory(symbol_name, "SHIFT_TYPE_" + cacheKey, "")
            shiftFromStrike = float(get_symbol_memory(symbol_name, "SHIFT_FROM_" + cacheKey, 0))
            lastTargetStrike = float(get_symbol_memory(symbol_name, "SHIFT_TARGET_" + cacheKey, 0))

            if get_symbol_memory(symbol_name, "IS_SHIFTED_" + cacheKey) == "true":
                if strike == shiftFromStrike or (lastTargetStrike > 0 and secondStrike != lastTargetStrike):
                    set_symbol_memory(symbol_name, "IS_SHIFTED_" + cacheKey, "false")
                    set_symbol_memory(symbol_name, "PREV_STRK_" + cacheKey, str(strike))

            if strike != prevStrike and prevStrike > 0 and (finalStatusText == "wtb" or finalStatusText == "wtt"):
                if strike < prevStrike:
                    shiftType = "top to bottom"
                    scenarioStatus = "wtb"
                else:
                    shiftType = "bottom to top"
                    scenarioStatus = "wtt"
                set_symbol_memory(symbol_name, "IS_SHIFTED_" + cacheKey, "true")
                set_symbol_memory(symbol_name, "SHIFT_TYPE_" + cacheKey, shiftType)
                set_symbol_memory(symbol_name, "SHIFT_FROM_" + cacheKey, str(prevStrike))
                set_symbol_memory(symbol_name, "SHIFT_TARGET_" + cacheKey, str(secondStrike))

            if get_symbol_memory(symbol_name, "IS_SHIFTED_" + cacheKey) == "true":
                if rawState == "STRONG" or finalStatusText == "stable strong" or "to strong" in finalStatusText:
                    set_symbol_memory(symbol_name, "IS_SHIFTED_" + cacheKey, "false")
                    set_symbol_memory(symbol_name, "PREV_STRK_" + cacheKey, str(strike))
                    isShifting = False
                    scenarioStatus = finalStatusText
                else:
                    isShifting = True
                    shiftType = get_symbol_memory(symbol_name, "SHIFT_TYPE_" + cacheKey)
                    scenarioStatus = "wtb" if shiftType == "top to bottom" else "wtt"
            else:
                set_symbol_memory(symbol_name, "PREV_STRK_" + cacheKey, str(strike))
                scenarioStatus = finalStatusText

            if not isWinner and rawState != "STRONG":
                displayStatus = "stable strong"
                scenarioStatus = "stable strong"

            displayTime = get_symbol_memory(symbol_name, "TIME_" + cacheKey)
            lastRecordedStatus = get_symbol_memory(symbol_name, "LAST_STATUS_" + cacheKey, "")
            isTriggerStatus = displayStatus in ["wtb", "wtt", "wtb to strong", "wtt to strong"] or isShifting

            if isTriggerStatus and displayStatus != lastRecordedStatus:
                displayTime = currentTimeStr
                set_symbol_memory(symbol_name, "TIME_" + cacheKey, displayTime)
                set_symbol_memory(symbol_name, "LAST_STATUS_" + cacheKey, displayStatus)
            elif not displayTime:
                displayTime = currentTimeStr
                set_symbol_memory(symbol_name, "TIME_" + cacheKey, displayTime)
                set_symbol_memory(symbol_name, "LAST_STATUS_" + cacheKey, displayStatus)

            if isShifting:
                line1 = f"<div class='card-header-line'>{displayTime} - {sideLabel}-{strike_str}-{displayStatus}-{second_strike_str}</div>"
                line2 = f"<div><span class='yellow-pill-badge'>shifted {shiftType}</span></div>"
                outputText = f"{line1}{line2}"
            else:
                if displayStatus == "stable strong":
                    line1 = f"<div class='card-header-line'>{displayTime} - {sideLabel} - {strike_str}</div>"
                    line2 = "<div><span class='yellow-pill-badge'>stable strong</span></div>"
                    outputText = f"{line1}{line2}"
                elif "to strong" in displayStatus:
                    line1 = f"<div class='card-header-line'>{displayTime} - {sideLabel} - {strike_str}</div>"
                    line2 = f"<div><span class='yellow-pill-badge'>{displayStatus}</span></div>"
                    outputText = f"{line1}{line2}"
                else:
                    weakDir = "weak towards top" if displayStatus == "wtt" else "weak towards bottom"
                    line1 = f"<div class='card-header-line'>{displayTime} - {sideLabel}-{strike_str}-{displayStatus}-{second_strike_str}</div>"
                    line2 = f"<div><span class='yellow-pill-badge'>{weakDir}</span></div>"
                    outputText = f"{line1}{line2}"

            return {'text': outputText, 'status': displayStatus, 'scenarioStatus': scenarioStatus, 'rawState': rawState}

        ceOiResult  = calculateIndependentCell(ceOiTop, "CE_OI", "Eor OI", ceWinner == "OI" or ceWinner == "BOTH", ceOiRawState, ceOiStatusText)
        ceVolResult = calculateIndependentCell(ceVolTop, "CE_VOL", "Eor VOL", ceWinner == "VOL" or ceWinner == "BOTH", ceVolRawState, ceVolStatusText)
        peVolResult = calculateIndependentCell(peVolTop, "PE_VOL", "Eos VOL", peWinner == "VOL" or peWinner == "BOTH", peVolRawState, peVolStatusText)
        peOiResult  = calculateIndependentCell(peOiTop, "PE_OI", "Eos OI", peWinner == "OI" or peWinner == "BOTH", peOiRawState, peOiStatusText)

        finalCeState = ceOiResult['scenarioStatus'] if ceWinner == "OI" else ceVolResult['scenarioStatus']
        finalPeState = peOiResult['scenarioStatus'] if ceWinner == "OI" else peVolResult['scenarioStatus']

        if ceWinner == "BOTH": finalCeState = ceVolResult['scenarioStatus'] if "to strong" in ceVolResult['scenarioStatus'] else ceOiResult['scenarioStatus']
        if peWinner == "BOTH": finalPeState = peVolResult['scenarioStatus'] if "to strong" in peVolResult['scenarioStatus'] else peOiResult['scenarioStatus']

        checkCe = finalCeState
        checkPe = finalPeState

        k3Text = f"{symbol_name} is Neutral"
        
        if checkCe == "wtt" and checkPe == "wtb": k3Text = f"{symbol_name} is 8th scenario both side Risky"
        elif checkCe == "wtb" and checkPe == "wtt": k3Text = f"{symbol_name} is 9th scenario both side Risky"
        elif checkCe in ["stable strong", "strong"] and checkPe in ["stable strong", "strong"]: k3Text = f"{symbol_name} is Neutral"
        elif checkCe == "wtb" and checkPe in ["stable strong", "strong"]: k3Text = f"{symbol_name} is slightly Bearish"
        elif checkCe in ["stable strong", "strong"] and checkPe == "wtb": k3Text = f"{symbol_name} is slightly Bearish"
        elif checkCe == "wtt" and checkPe in ["stable strong", "strong"]: k3Text = f"{symbol_name} is slightly bullish"
        elif checkCe in ["stable strong", "strong"] and checkPe == "wtt": k3Text = f"{symbol_name} is slightly bullish"
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
        elif checkCe == "wtb to strong" and checkPe in ["stable strong", "strong"]: k3Text = f"{symbol_name} is slightly Bullish"
        elif checkCe in ["stable strong", "strong"] and checkPe == "wtb to strong": k3Text = f"{symbol_name} is slightly Bullish"
        elif checkCe == "wtt to strong" and checkPe in ["stable strong", "strong"]: k3Text = f"{symbol_name} is slightly Bearish"
        elif checkCe in ["stable strong", "strong"] and checkCe == "wtt to strong": k3Text = f"{symbol_name} is slightly Bearish"
        elif checkCe == "wtt to strong" and checkPe == "wtb to strong": k3Text = f"{symbol_name} is 8th scenario both side Risky trade"
        elif checkCe == "wtb to strong" and checkPe == "wtt to strong": k3Text = f"{symbol_name} is 9th scenario both side Risky trade"
        elif checkCe == "wtb to strong" and checkPe == "wtb to strong": k3Text = f"{symbol_name} is confirm Bull Run"
        elif checkCe == "wtt to strong" and checkPe == "wtt to strong": k3Text = f"{symbol_name} is confirm bloodbath"
        elif checkCe == "wtt" and checkPe == "wtb to strong": k3Text = f"{symbol_name} is Bullish Reversal Trend"
        elif checkCe == "wtb" and checkPe == "wtt to strong": k3Text = f"{symbol_name} is Bearish Reversal Trend"
        else: k3Text = f"{symbol_name} is Active Trend"

        finalD5, finalH5, finalQ5, finalU5 = "-----", "-----", "-----", "-----"

        if ceWinner == "BOTH": finalD5, finalH5 = ceOiResult['text'], ceVolResult['text']
        elif ceWinner == "VOL": finalH5 = ceVolResult['text']
        elif ceWinner == "OI": finalD5 = ceOiResult['text']

        if peWinner == "BOTH": finalU5, finalQ5 = peOiResult['text'], peVolResult['text']
        elif peWinner == "VOL": finalQ5 = peVolResult['text']
        elif peWinner == "OI": finalU5 = peOiResult['text']

        if symbol_name not in multi_symbol_runtime_states: multi_symbol_runtime_states[symbol_name] = {}
        vault = multi_symbol_runtime_states[symbol_name]
        vault["D5_TEXT"] = finalD5
        vault["H5_TEXT"] = finalH5
        vault["U5_TEXT"] = finalU5
        vault["Q5_TEXT"] = finalQ5
        vault["K3_TEXT"] = k3Text
        vault["lot_size"] = lot_size
        vault["spot_price"] = round(spot_price, 2)
        vault["active_expiry"] = active_expiry

        c_oi_1, c_oi_2 = ceOiTop['highest']['strike'], ceOiTop['second']['strike']
        c_vol_1, c_vol_2 = ceVolTop['highest']['strike'], ceVolTop['second']['strike']
        p_vol_1, p_vol_2 = peVolTop['highest']['strike'], peVolTop['second']['strike']
        p_oi_1, p_oi_2 = peOiTop['highest']['strike'], peOiTop['second']['strike']

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
                    'is_spot_row': True, 'spot_price': round(spot_price, 2),
                    'ce_oi_badge': build_badge_payload(ceOiTop, ceOiRawState),
                    'ce_vol_badge': build_badge_payload(ceVolTop, ceVolRawState),
                    'pe_vol_badge': build_badge_payload(peVolTop, peVolRawState),
                    'pe_oi_badge': build_badge_payload(peOiTop, peOiRawState)
                })
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
            grid_rows_built.append(r_copy)

        if not spot_inserted:
            grid_rows_built.append({
                'is_spot_row': True, 'spot_price': round(spot_price, 2),
                'ce_oi_badge': build_badge_payload(ceOiTop, ceOiRawState),
                'ce_vol_badge': build_badge_payload(ceVolTop, ceVolRawState),
                'pe_vol_badge': build_badge_payload(peVolTop, peVolRawState),
                'pe_oi_badge': build_badge_payload(peOiTop, peOiRawState)
            })

        vault["grid_rows"] = grid_rows_built
    except Exception as e: 
        print(f"❌ Scenario Tracker Error: {e}")


# --- MARKET DATA ENGINE ---
def fetch_symbol_data(smart_api, clean_active_symbol):
    global morning_oi_dict, baseline_date, options_cache, persistent_quotes_cache, symbol_expiries_cache, user_selected_expiries
    try:
        now_dt = datetime.now()
        current_date_str = now_dt.strftime('%Y-%m-%d')
        if baseline_date != current_date_str: 
            morning_oi_dict = {}
            baseline_date = current_date_str
            
        if not options_cache or clean_active_symbol not in options_cache: return

        sym_options = options_cache[clean_active_symbol]
        valid_opts = sym_options[sym_options['parsed_expiry'] >= pd.Timestamp(now_dt.date())].copy()
        if valid_opts.empty: return

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
        index_map = {
            "NIFTY": {"exch": "NSE", "token": "99926000"},
            "BANKNIFTY": {"exch": "NSE", "token": "99926009"},
            "FINNIFTY": {"exch": "NSE", "token": "99926018"},
            "SENSEX": {"exch": "BSE", "token": "99919000"},
            "BSX": {"exch": "BSE", "token": "99919000"},
            "BSEX": {"exch": "BSE", "token": "99919000"}
        }

        if clean_active_symbol in index_map:
            idx_info = index_map[clean_active_symbol]
            try:
                q_res = smart_api.getMarketData("FULL", {idx_info["exch"]: [idx_info["token"]]})
                if q_res and q_res.get('status') and q_res.get('data'):
                    fetched_data = q_res['data'].get('fetched', {})
                    token_key = idx_info["token"]
                    if isinstance(fetched_data, dict) and token_key in fetched_data: 
                        spot_price = float(fetched_data[token_key].get('ltp', 0))
                    elif isinstance(fetched_data, list) and len(fetched_data) > 0: 
                        spot_price = float(fetched_data[0].get('ltp', 0))
            except Exception as e:
                print(f"⚠ Spot Fetch Error (Index): {e}")

        if spot_price <= 0 and clean_active_symbol not in index_map:
            try:
                equity_row = full_scrip_df[(full_scrip_df['name'].str.upper() == clean_active_symbol) & (full_scrip_df['exch_seg'] == 'NSE')]
                if not equity_row.empty:
                    eq_tok = str(equity_row['token'].iloc[0]).strip()
                    q_res = smart_api.getMarketData("FULL", {"NSE": [eq_tok]})
                    if q_res and q_res.get('status') and q_res.get('data'):
                        fetched_data = q_res['data'].get('fetched', {})
                        if isinstance(fetched_data, dict) and eq_tok in fetched_data: 
                            spot_price = float(fetched_data[eq_tok].get('ltp', 0))
                        elif isinstance(fetched_data, list) and len(fetched_data) > 0: 
                            spot_price = float(fetched_data[0].get('ltp', 0))
            except Exception as e:
                print(f"⚠ Spot Fetch Error (Equity): {e}")

        if spot_price <= 0: return

        all_master_strikes = sorted(current_expiry_options['strike'].unique())
        closest_idx = min(range(len(all_master_strikes)), key=lambda i: abs(all_master_strikes[i] - spot_price))
        strikes = all_master_strikes[max(0, closest_idx - 18):min(len(all_master_strikes), closest_idx + 19)]

        filtered_opts = current_expiry_options[current_expiry_options['strike'].isin(strikes)].copy()
        exch_map = {}
        for _, row in filtered_opts.iterrows():
            exch, tok = str(row['exch_seg']).strip(), str(row['token']).strip()
            if exch not in exch_map: exch_map[exch] = []
            exch_map[exch].append(tok)

        all_dash_quotes = []
        for exch, t_list in exch_map.items():
            for i in range(0, len(t_list), 20):
                try:
                    res = smart_api.getMarketData("FULL", {exch: t_list[i:i+20]})
                    if res and res.get('status') and res.get('data') and res.get('data').get('fetched'):
                        all_dash_quotes.extend(res['data']['fetched'])
                except Exception: pass
                time.sleep(0.05)

        dash_quotes_dict = {}
        for item in all_dash_quotes:
            tok = str(item.get('symbolToken', item.get('token', ''))).strip()
            persistent_quotes_cache[tok] = {
                'ltp': float(item.get('ltp', 0)), 'volume': float(item.get('tradeVolume', item.get('volume', 0))),
                'oi': float(item.get('opnInterest', item.get('oi', 0))), 'net_change': float(item.get('netChange', item.get('change', 0))),
                'oi_chg': float(item.get('opnInterestChange', item.get('oiChange', 0)))
            }

        for tok, q_data in persistent_quotes_cache.items():
            if now_dt.time() >= datetime.strptime("09:15:00", "%H:%M:%S").time() and tok not in morning_oi_dict and q_data['oi'] > 0: 
                morning_oi_dict[tok] = q_data['oi']
            base_oi = morning_oi_dict.get(tok, q_data['oi'])
            calc_oi_chg = q_data['oi'] - base_oi if q_data['oi'] > 0 else q_data.get('oi_chg', 0)
            dash_quotes_dict[tok] = {'ltp': q_data['ltp'], 'volume': q_data['volume'], 'oi': q_data['oi'], 'oi_chg': calc_oi_chg, 'net_change': q_data['net_change']}

        expiry_dt_val = pd.Timestamp(current_expiry_options['parsed_expiry'].iloc[0])
        t = max((expiry_dt_val.replace(hour=15, minute=30) - datetime.now()).total_seconds() / (365.0 * 24 * 3600), 0.0001)

        raw_grid_payload = []
        for st in strikes:
            ce_sub = current_expiry_options[(current_expiry_options['strike'] == st) & (current_expiry_options['symbol'].str.upper().str.endswith('CE'))]
            pe_sub = current_expiry_options[(current_expiry_options['strike'] == st) & (current_expiry_options['symbol'].str.upper().str.endswith('PE'))]
            
            ce_token = str(ce_sub['token'].iloc[0]).strip() if not ce_sub.empty else ""
            pe_token = str(pe_sub['token'].iloc[0]).strip() if not pe_sub.empty else ""
            
            ce_q = dash_quotes_dict.get(ce_token, {'ltp': 0, 'volume': 0, 'oi': 0, 'oi_chg': 0, 'net_change': 0})
            pe_q = dash_quotes_dict.get(pe_token, {'ltp': 0, 'volume': 0, 'oi': 0, 'oi_chg': 0, 'net_change': 0})

            ce_gks = calculate_iv_and_greeks('CE', spot_price, st, t, 0.07, ce_q['ltp'])
            pe_gks = calculate_iv_and_greeks('PE', spot_price, st, t, 0.07, pe_q['ltp'])
            
            raw_grid_payload.append({
                'strike': st, 'ce_oi': ce_q['oi'], 'ce_vol': ce_q['volume'], 'pe_vol': pe_q['volume'], 'pe_oi': pe_q['oi'],
                'ce_vega': ce_gks['vega'], 'ce_theta': ce_gks['theta'], 'ce_gamma': ce_gks['gamma'], 'ce_delta': ce_gks['delta'], 'ce_iv': ce_gks['iv'] if ce_gks['iv'] > 1 else 0,
                'ce_oi_chg': ce_q['oi_chg'], 'ce_change': ce_q['net_change'], 'ce_ltp': ce_q['ltp'],
                'pe_ltp': pe_q['ltp'], 'pe_change': pe_q['net_change'], 'pe_oi_chg': pe_q['oi_chg'],
                'pe_iv': pe_gks['iv'] if pe_gks['iv'] > 1 else 0, 'pe_delta': pe_gks['delta'], 'pe_gamma': pe_gks['gamma'], 'pe_theta': pe_gks['theta'], 'pe_vega': pe_gks['vega'],
                'is_itm_ce': bool(st < spot_price), 'is_itm_pe': bool(st > spot_price)
            })

        execute_python_scenario_tracker(raw_rows=raw_grid_payload, spot_price=spot_price, lot_size=lot_size, active_expiry=current_expiry, symbol_name=clean_active_symbol)
    except Exception as e:
        print(f"❌ fetch_symbol_data Exception: {e}")


# --- SCRIP MASTER DOWNLOADER ---
def initialize_scrip_master(smart_api):
    global full_scrip_df, options_cache, symbol_expiries_cache
    try:
        url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
        resp = requests.get(url, timeout=30)
        if resp.status_code == 200:
            scrip_data = resp.json()
            full_scrip_df = pd.DataFrame(scrip_data)
            if 'symboltoken' in full_scrip_df.columns and 'token' not in full_scrip_df.columns: full_scrip_df['token'] = full_scrip_df['symboltoken']
            if 'tradingsymbol' in full_scrip_df.columns and 'symbol' not in full_scrip_df.columns: full_scrip_df['symbol'] = full_scrip_df['tradingsymbol']
            if 'strike' in full_scrip_df.columns: full_scrip_df['strike'] = pd.to_numeric(full_scrip_df['strike'], errors='coerce').fillna(0) / 100.0
            
            fno_opts = full_scrip_df[(full_scrip_df['strike'] > 0) & (full_scrip_df['exch_seg'].isin(['NFO', 'MCX', 'BFO', 'BSE']))].copy()
            fno_opts['strike'] = pd.to_numeric(fno_opts['strike'], errors='coerce')
            fno_opts['parsed_expiry'] = pd.to_datetime(fno_opts['expiry'], format='mixed', errors='coerce')
            
            today_dt = pd.Timestamp(datetime.now().date())
            for name, group in fno_opts.groupby(fno_opts['name'].str.upper()):
                clean_name = name.strip()
                if clean_name in ['BSX', 'BSEX']: clean_name = 'SENSEX'
                options_cache[clean_name] = group
                valid_group = group[group['parsed_expiry'] >= today_dt].copy()
                valid_group_sorted = valid_group.sort_values(by='parsed_expiry')
                unique_expiries = []
                for exp_dt in valid_group_sorted['parsed_expiry'].unique():
                    formatted_exp = pd.Timestamp(exp_dt).strftime('%d%b%Y').upper()
                    if formatted_exp not in unique_expiries: unique_expiries.append(formatted_exp)
                symbol_expiries_cache[clean_name] = unique_expiries

            if "SENSEX" not in options_cache and "BSX" in options_cache:
                options_cache["SENSEX"] = options_cache["BSX"]
                symbol_expiries_cache["SENSEX"] = symbol_expiries_cache.get("BSX", [])
    except Exception: pass


# --- HTML TEMPLATE (REMOVED CALLS/PUTS BANNER, GREEN/RED HEADERS, WHITE IN-LINE WINNER CARDS) ---
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
        select { padding: 6px; font-size: 14px; background: #ffffff; border: 1px solid #ced4da; border-radius: 4px; }
        
        .yellow-pill-badge { background: #ffd43b; color: #000000; font-size: 10px; font-weight: 700; padding: 2px 8px; border-radius: 10px; border: 1px solid #fab005; display: inline-block; margin-top: 3px; }
        .k3-banner { background: #e6fcf5; padding: 8px; margin-top: 6px; text-align: center; font-size: 14px; font-weight: bold; border-radius: 5px; color: #0ca678; border: 1px solid #20c997; }
        
        .table-container { margin-top: 6px; max-height: 780px; overflow-y: auto; overflow-x: auto; border: 1px solid #dee2e6; border-radius: 6px; position: relative; -webkit-overflow-scrolling: touch; }
        
        table { width: max-content; min-width: 100%; border-collapse: collapse; background: #ffffff; font-size: 11px; text-align: center; }
        
        /* 📌 Column headers with Call=Green, Put=Red */
        th { border: 1px solid #dee2e6; padding: 4px 5px; white-space: nowrap; position: relative; color: #212529; font-weight: bold; }
        .th-ce { background: #d4edda !important; color: #155724 !important; }
        .th-pe { background: #f8d7da !important; color: #721c24 !important; }
        
        td { border: none; border-bottom: 1px solid #f1f3f5; padding: 4px 5px; white-space: nowrap; position: relative; }
        
        th { position: sticky; top: 0; z-index: 11; }
        
        .strike-col { background: #ff922b !important; color: #ffffff !important; font-weight: bold; font-size: 11px; border: 1px solid #dee2e6 !important; }
        th.strike-col { position: sticky; top: 0; z-index: 14; }

        .ce-oi-col, .ce-vol-col, .pe-vol-col, .pe-oi-col { min-width: 80px !important; width: 80px !important; }
        
        /* 📌 Pure White Background Cards placed in line after 1 column from Strike */
        .winner-card-inline { background: #ffffff !important; border: 1px solid #ced4da; border-radius: 5px; padding: 4px 8px; text-align: center; font-size: 11px; font-weight: bold; color: #000000; box-shadow: 0 1px 3px rgba(0,0,0,0.08); display: inline-block; min-width: 140px; }

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
        .pct-sub { font-size: 9px; font-weight: bold; display: block; color: #000000; margin-top: 1px; }
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
    
    <div class="table-container" id="tableContainer">
        <table id="optionChainTable">
            <thead>
                <!-- 📌 Calls=Green background & Puts=Red background -->
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
            isFirstLoad = true;
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
                
                let k3Div = document.getElementById("k3Display");
                k3Div.innerText = data.K3;
                let k3TextLower = data.K3.toLowerCase();
                if(k3TextLower.includes("bull")) k3Div.style.color = "#0000FF";
                else if(k3TextLower.includes("bear") || k3TextLower.includes("bloodbath")) k3Div.style.color = "#FF0000";
                else k3Div.style.color = "#000000";

                let tbody = document.getElementById("gridBody"); tbody.innerHTML = "";
                
                /* 📌 Top Winner Row inside table placed 1 column away from Strike with pure white background */
                let winnerRow = document.createElement("tr");
                winnerRow.innerHTML = `
                    <td colspan="8"></td>
                    <td colspan="2" style="padding:4px;"><div class="winner-card-inline">${data.D5}<br>${data.H5}</div></td>
                    <td class="strike-col" style="background:#f1f3f5 !important; color:#000 !important; font-size:10px;">OUTPUT</td>
                    <td colspan="2" style="padding:4px;"><div class="winner-card-inline">${data.Q5}<br>${data.U5}</div></td>
                    <td colspan="8"></td>
                `;
                tbody.appendChild(winnerRow);

                if (data.grid_rows && data.grid_rows.length > 0) {
                    data.grid_rows.forEach(row => {
                        let tr = document.createElement("tr");
                        if (row.is_spot_row) {
                            tr.classList.add("spot-row", "spot-target-row");
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
                            tr.innerHTML = `<td></td><td></td><td></td><td></td><td></td><td></td><td colspan="2" style="position:relative;">${ceOverlay}</td><td></td><td></td><td class="spot-price-cell strike-col"><span class="spot-tag-red">SPOT</span><span class="spot-value-green">${renderSpotValue}</span></td><td></td><td></td><td colspan="2" style="position:relative;">${peOverlay}</td><td></td><td></td><td></td><td></td><td></td><td></td>`;
                        } else {
                            tr.innerHTML = `<td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_vega}</td><td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_theta}</td><td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_gamma}</td><td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_delta}</td><td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_iv}</td><td class="${row.ce_oi_chg >= 0 ? 'chg-pos' : 'chg-neg'} ${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_oi_chg}</td><td class="ce-oi-col ${row.ce_oi_color}">${row.ce_oi}<span class="pct-sub">${row.ce_oi_pct}%</span></td><td class="ce-vol-col ${row.ce_vol_color}">${row.ce_vol}<span class="pct-sub">${row.ce_vol_pct}%</span></td><td class="${row.ce_change >= 0 ? 'chg-pos' : 'chg-neg'} ${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_change}</td><td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_ltp}</td><td class="strike-col">${row.strike}</td><td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_ltp}</td><td class="${row.pe_change >= 0 ? 'chg-pos' : 'chg-neg'} ${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_change}</td><td class="pe-vol-col ${row.pe_vol_color}">${row.pe_vol}<span class="pct-sub">${row.pe_vol_pct}%</span></td><td class="pe-oi-col ${row.pe_oi_color}">${row.pe_oi}<span class="pct-sub">${row.pe_oi_pct}%</span></td><td class="${row.pe_oi_chg >= 0 ? 'chg-pos' : 'chg-neg'} ${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_oi_chg}</td><td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_iv}</td><td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_delta}</td><td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_gamma}</td><td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_theta}</td><td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_vega}</td>`;
                        }
                        tbody.appendChild(tr);
                    });

                    if (isFirstLoad) {
                        setTimeout(() => {
                            let spotRowTarget = document.querySelector('.spot-target-row');
                            let container = document.getElementById('tableContainer');
                            if (spotRowTarget && container) {
                                let rowTop = spotRowTarget.offsetTop;
                                let rowHeight = spotRowTarget.offsetHeight;
                                let containerHeight = container.clientHeight;
                                container.scrollTop = rowTop - (containerHeight / 2) + (rowHeight / 2);

                                let strikeCell = spotRowTarget.querySelector('.strike-col');
                                if (strikeCell) {
                                    let cellLeft = strikeCell.offsetLeft;
                                    let cellWidth = strikeCell.offsetWidth;
                                    let containerWidth = container.clientWidth;
                                    container.scrollLeft = cellLeft - (containerWidth / 2) + (cellWidth / 2);
                                }
                            }
                        }, 150);
                        isFirstLoad = false;
                    }
                }
            });
        }
        setInterval(fetchData, 3000); window.onload = initApp;
    </script>
</body>
</html>
"""


# --- FLASK API ROUTES ---
@app.route('/')
def home(): 
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/init-meta')
def get_initial_metadata():
    global current_active_symbol_ui
    return jsonify({
        "symbols": sorted(list(options_cache.keys())) if options_cache else ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"], 
        "expiries": symbol_expiries_cache, 
        "active_symbol": current_active_symbol_ui
    })

@app.route('/api/set-symbol/<symbol>', defaults={'expiry': None}, methods=['POST'])
@app.route('/api/set-symbol/<symbol>/<expiry>', methods=['POST'])
def set_symbol_api(symbol, expiry):
    global user_selected_expiries, current_active_symbol_ui, smart_api_global
    sym = symbol.upper()
    current_active_symbol_ui = sym
    try:
        if expiry: user_selected_expiries[sym] = expiry.upper()
        if smart_api_global and sym in options_cache:
            threading.Thread(target=fetch_symbol_data, args=(smart_api_global, sym), daemon=True).start()
    except Exception: pass
    return jsonify({"status": "success"})

@app.route('/api/data/<symbol>')
def get_symbol_runtime_data(symbol):
    sym = symbol.upper()
    vault = multi_symbol_runtime_states.get(sym, {})
    if not vault.get("grid_rows") and smart_api_global and sym in options_cache:
        threading.Thread(target=fetch_symbol_data, args=(smart_api_global, sym), daemon=True).start()
        
    return jsonify({
        "lot_size": vault.get("lot_size", 1),
        "D5": vault.get("D5_TEXT", "-----"),
        "H5": vault.get("H5_TEXT", "-----"),
        "Q5": vault.get("Q5_TEXT", "-----"),
        "U5": vault.get("U5_TEXT", "-----"),
        "K3": vault.get("K3_TEXT", f"{sym} is Neutral"),
        "grid_rows": vault.get("grid_rows", []),
        "spot_price": vault.get("spot_price", 0)
    })


# --- BACKGROUND PROCESS ENGINE ---
def run_bot_lock_loop():
    global last_session_date, current_active_symbol_ui, options_cache, smart_api_global
    print("🚀 Starting Background Bot Loop...")
    time.sleep(3)
    
    try: 
        smart_api = SmartConnect(api_key=API_KEY)
        smart_api_global = smart_api
    except Exception as e: 
        print(f"❌ Init Error: {e}")
        return
    
    while True:
        try:
            now_dt = datetime.now()
            today_date_str = now_dt.strftime("%Y-%m-%d")
            
            if last_session_date != today_date_str:
                totp = pyotp.TOTP(TOTP_SECRET.replace(" ", "")).now() if TOTP_SECRET else "123456"
                print(f"🔐 Generating Session for Client ID: {CLIENT_ID}")
                session_res = smart_api.generateSession(CLIENT_ID, PIN, totp)
                if session_res and session_res.get('status'):
                    last_session_date = today_date_str
                    print("🔐 Session Generated Successfully! Loading Scrip Master...")
                    initialize_scrip_master(smart_api)
            
            if options_cache:
                active_sym = current_active_symbol_ui
                if active_sym: 
                    fetch_symbol_data(smart_api, active_sym)
                
                for background_sym in ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"]:
                    if background_sym != active_sym and background_sym in options_cache:
                        fetch_symbol_data(smart_api, background_sym)
                        time.sleep(0.1)

        except Exception as e: 
            print(f"⚠ Loop Error: {e}")
        time.sleep(3)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    run_bot_lock_loop()
