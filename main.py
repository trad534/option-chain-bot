# -----------------------------
# Part 1: Imports & Setup
# -----------------------------
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
from datetime import datetime, timedelta
from SmartApi import SmartConnect
from flask import Flask
import threading
import builtins

RENDER_URL = "https://option-chain-bot.onrender.com"

def self_ping():
    while True:
        try:
            if RENDER_URL:
                response = requests.get(RENDER_URL)
                print(f"Self-ping sent successfully: Status {response.status_code}")
        except Exception as e:
            print(f"Self-ping error: {e}")
        time.sleep(180)

ping_thread = threading.Thread(target=self_ping, daemon=True)
ping_thread.start()

# ==========================================
# FORCE FLUSH LOGS IN RENDER
# ==========================================
def print(*args, **kwargs):
    kwargs['flush'] = True
    builtins.print(*args, **kwargs)

# ==========================================
# FLASK WEB SERVER FOR RENDER PORT
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return "Angel One Option Chain Bot & 265-Symbol Background Memory Engine is active!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# ==========================================
# CONFIGURATION & GOOGLE SHEETS
# ==========================================
CLIENT_ID = os.getenv("ANGEL_CLIENT_ID")
PIN = os.getenv("ANGEL_PIN")
API_KEY = os.getenv("ANGEL_API_KEY")
TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET")

GOOGLE_SHEET_NAME = "6 LTP CALCULATOR"
MAIN_SHEET_NAME = "upstox get data"
SYMBOL_SHEET_NAME = "Option chain index new"

def get_gspread_client():
    if os.path.exists("/etc/secrets/Credentials.json"):
        return gspread.service_account(filename="/etc/secrets/Credentials.json")
    if os.path.exists("Credentials.json"):
        return gspread.service_account(filename="Credentials.json")
    env_creds = os.getenv("GOOGLE_CREDENTIALS")
    if env_creds:
        creds_dict = json.loads(env_creds)
        return gspread.service_account_from_dict(creds_dict)
    raise Exception("No Credentials found for Google Sheets!")

try:
    gc = get_gspread_client()
    spreadsheet = gc.open(GOOGLE_SHEET_NAME)
    data_sheet = spreadsheet.worksheet(MAIN_SHEET_NAME)
    dashboard_sheet = spreadsheet.worksheet(SYMBOL_SHEET_NAME)
    print("✅ SUCCESS: Google Sheets Connected Successfully!")
except Exception as e:
    print(f"❌ Google Sheet Connection Error: {e}")
    exit()

# ==========================================
# MULTI-SYMBOL MEMORY & ISOLATED NAMESPACES
# ==========================================
all_symbols_global_history = {}
symbol_memories = {}
global_run_date = ""
last_written_values = {}
active_current_symbol = "RELIANCE"

def get_memory(key, default=""):
    global active_current_symbol
    if active_current_symbol not in symbol_memories:
        symbol_memories[active_current_symbol] = {}
    return symbol_memories[active_current_symbol].get(key, default)

def set_memory(key, value):
    global active_current_symbol
    if active_current_symbol not in symbol_memories:
        symbol_memories[active_current_symbol] = {}
    symbol_memories[active_current_symbol][key] = str(value)

def remove_memory(key):
    global active_current_symbol
    if active_current_symbol in symbol_memories and key in symbol_memories[active_current_symbol]:
        del symbol_memories[active_current_symbol][key]

def log_symbol_scenario(symbol, ce_state, pe_state, scenario_text, current_time_str):
    if not symbol:
        return
    clean_sym = symbol.replace("-EQ", "").replace(".NS", "").strip()
    if clean_sym not in all_symbols_global_history:
        all_symbols_global_history[clean_sym] = []
    
    history_list = all_symbols_global_history[clean_sym]
    if history_list and history_list[-1]['scenario'] == scenario_text:
        return

    history_list.append({
        "time": current_time_str,
        "ce_state": ce_state,
        "pe_state": pe_state,
        "scenario": scenario_text
    })
    
    print(f"📝 [LOG TRACKER] Saved scenario for {clean_sym} | Total tracked symbols: {len(all_symbols_global_history)} | Latest: {scenario_text} (CE: {ce_state}, PE: {pe_state}, Time: {current_time_str})")

def safe_update_cell(sheet, cell_name, new_value):
    global last_written_values
    cache_key = f"{sheet.title}_{cell_name}"
    str_val = str(new_value)
    if last_written_values.get(cache_key) != str_val:
        try:
            sheet.update_acell(cell_name, str_val)
            last_written_values[cache_key] = str_val
            time.sleep(0.3)
        except Exception as e:
            print(f"⚠ Cell Update Error on {cell_name}: {e}")

def update_k3_with_color(spreadsheet_obj, sheet, cell_name, text_value):
    try:
        sheet.update_acell(cell_name, text_value)
        text_lower = text_value.lower()
        if "bull" in text_lower:
            color_dict = {"red": 0.0, "green": 0.0, "blue": 1.0}
        elif "bear" in text_lower or "bloodbath" in text_lower:
            color_dict = {"red": 1.0, "green": 0.0, "blue": 0.0}
        else:
            color_dict = {"red": 0.0, "green": 0.0, "blue": 0.0}

        row, col = gspread.utils.a1_to_rowcol(cell_name)
        body = {
            "requests": [
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet.id,
                            "startRowIndex": row - 1,
                            "endRowIndex": row,
                            "startColumnIndex": col - 1,
                            "endColumnIndex": col
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "textFormat": {
                                    "foregroundColor": color_dict
                                }
                            }
                        },
                        "fields": "userEnteredFormat.textFormat.foregroundColor"
                    }
                }
            ]
        }
        spreadsheet_obj.batch_update(body)
    except Exception as e:
        print(f"⚠ K3 Color Formatting Error: {e}")

def calculate_iv_and_greeks(flag, S, K, t, r, ltp):
    if t <= 0 or S <= 0 or K <= 0 or ltp <= 0.05:
        delta = 1.0 if (flag == 'CE' and S > K) else (0.0 if flag == 'CE' else (-1.0 if S < K else 0.0))
        return {'iv': 0, 'delta': round(delta, 3), 'gamma': 0, 'theta': 0, 'vega': 0}

    intrinsic = max(0, S - K * np.exp(-r * t)) if flag == 'CE' else max(0, K * np.exp(-r * t) - S)
    if ltp < intrinsic:
        ltp = intrinsic + 0.05

    def objective_function(sigma):
        if sigma <= 0: sigma = 1e-4
        d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * t) / (sigma * np.sqrt(t))
        d2 = d1 - sigma * np.sqrt(t)
        price = S * norm.cdf(d1) - K * np.exp(-r * t) * norm.cdf(d2) if flag == 'CE' else K * np.exp(-r * t) * norm.cdf(-d2) - S * norm.cdf(-d1)
        return price - ltp

    try:
        sigma = brentq(objective_function, 1e-4, 5.0, xtol=1e-6, maxiter=100)
    except Exception:
        sigma = 0.20

    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * t) / (sigma * np.sqrt(t))
    d2 = d1 - sigma * np.sqrt(t)
    gamma = norm.pdf(d1) / (S * sigma * np.sqrt(t))
    vega_val = S * norm.pdf(d1) * np.sqrt(t) / 100

    if flag == 'CE':
        delta = norm.cdf(d1)
        theta = (- (S * norm.pdf(d1) * sigma) / (2 * np.sqrt(t)) - r * K * np.exp(-r * t) * norm.cdf(d2)) / 365
    else:
        delta = norm.cdf(d1) - 1
        theta = (- (S * norm.pdf(d1) * sigma) / (2 * np.sqrt(t)) + r * K * np.exp(-r * t) * norm.cdf(-d2)) / 365

    return {
        'iv': round(sigma * 100, 2), 'delta': round(delta, 3),
        'gamma': round(gamma, 4), 'theta': round(theta, 2), 'vega': round(vega_val, 2)
    }

# ==========================================
# MASTER SCENARIO ENGINE (UNTOUCHED)
# ==========================================
def execute_python_scenario_tracker():
    global global_run_date
    try:
        now_dt = datetime.now()
        current_kolkata_time = now_dt.strftime("%H:%M:%S")
        today_str = now_dt.strftime("%Y-%m-%d")

        if global_run_date != today_str:
            symbol_memories.clear()
            global_run_date = today_str

        if current_kolkata_time.startswith("09:14:5"):
            symbol_memories.clear()

        range_values = dashboard_sheet.get("A8:T28")
        if not range_values: return

        MARKET_ROW = 18
        ceVolArray, ceOiArray, peVolArray, peOiArray = [], [], [], []

        for r in range(15, 29):
            if r == MARKET_ROW: continue
            array_index = r - 8
            if array_index < 0 or array_index >= len(range_values): continue
            
            row_data = range_values[array_index]
            if len(row_data) <= 13: continue

            strike_val = row_data[13]
            strike = float(strike_val) if strike_val not in [None, "", "NAN"] else 0
            if not strike: continue

            ce_oi_val = row_data[7] if len(row_data) > 7 else "0"
            ce_vol_val = row_data[8] if len(row_data) > 8 else "0"
            
            ce_oi_raw = str(ce_oi_val).replace("%", "").strip() if ce_oi_val else "0"
            ce_vol_raw = str(ce_vol_val).replace("%", "").strip() if ce_vol_val else "0"

            if "strong" in ce_oi_raw.lower(): ce_oi_raw = "100"
            if "strong" in ce_vol_raw.lower(): ce_vol_raw = "100"

            ceOiArray.append({'strike': strike, 'val': float(ce_oi_raw) if ce_oi_raw.replace('.','',1).isdigit() else 0, 'row': r})
            ceVolArray.append({'strike': strike, 'val': float(ce_vol_raw) if ce_vol_raw.replace('.','',1).isdigit() else 0, 'row': r})

        for r in range(8, 22):
            if r == MARKET_ROW: continue
            array_index = r - 8
            if array_index < 0 or array_index >= len(range_values): continue
            
            row_data = range_values[array_index]
            if len(row_data) <= 19: continue

            strike_val = row_data[13]
            strike = float(strike_val) if strike_val not in [None, "", "NAN"] else 0
            if not strike: continue

            pe_vol_val = row_data[18] if len(row_data) > 18 else "0"
            pe_oi_val = row_data[19] if len(row_data) > 19 else "0"
            
            pe_vol_raw = str(pe_vol_val).replace("%", "").strip() if pe_vol_val else "0"
            pe_oi_raw = str(pe_oi_val).replace("%", "").strip() if pe_oi_val else "0"

            if "strong" in pe_vol_raw.lower(): pe_vol_raw = "100"
            if "strong" in pe_oi_raw.lower(): pe_oi_raw = "100"

            peVolArray.append({'strike': strike, 'val': float(pe_vol_raw) if pe_vol_raw.replace('.','',1).isdigit() else 0, 'row': r})
            peOiArray.append({'strike': strike, 'val': float(pe_oi_raw) if pe_oi_raw.replace('.','',1).isdigit() else 0, 'row': r})

        def extract_top_two(data_array):
            if not data_array:
                return {'highest': {'strike': 0, 'val': 0, 'row': 0}, 'second': {'strike': 0, 'val': 0, 'row': 0}, 'pct': 0}
            filtered = [item for item in data_array if item['val'] > 0]
            if not filtered:
                return {'highest': {'strike': 0, 'val': 0, 'row': 0}, 'second': {'strike': 0, 'val': 0, 'row': 0}, 'pct': 0}
            filtered.sort(key=lambda x: x['val'], reverse=True)
            highest = filtered[0]
            second = filtered[1] if len(filtered) > 1 else {'strike': highest['strike'], 'val': 0, 'row': highest['row']}
            pct = (second['val'] / highest['val']) * 100 if highest['val'] > 0 else 0
            return {'highest': highest, 'second': second, 'pct': pct}

        ceVolTop = extract_top_two(ceVolArray)
        ceOiTop = extract_top_two(ceOiArray)
        peVolTop = extract_top_two(peVolArray)
        peOiTop = extract_top_two(peOiArray)

        if not ceVolTop['highest']['strike'] or not ceOiTop['highest']['strike'] or not peVolTop['highest']['strike'] or not peOiTop['highest']['strike']: return

        def detect_live_state_advanced(top_obj, is_put_side):
            if not top_obj or top_obj['highest']['val'] == 0 or top_obj['pct'] < 75.0:
                return "STRONG"
            if is_put_side:
                return "wtb" if top_obj['second']['strike'] < top_obj['highest']['strike'] else "wtt"
            else:
                return "wtt" if top_obj['second']['strike'] > top_obj['highest']['strike'] else "wtb"

        ceVolRawState = detect_live_state_advanced(ceVolTop, False)
        ceOiRawState = detect_live_state_advanced(ceOiTop, False)
        peVolRawState = detect_live_state_advanced(peVolTop, True)
        peOiRawState = detect_live_state_advanced(peOiTop, True)

        def get_full_status_string(top_obj, raw_state, cache_key):
            if not top_obj or not top_obj.get('highest') or not top_obj['highest'].get('strike') or top_obj['highest']['strike'] == 0:
                return "stable strong"
            strike = top_obj['highest']['strike']
            prev_strike_str = get_memory("PREV_STRK_" + cache_key)
            prev_strike = float(prev_strike_str) if prev_strike_str else strike
            current_weak_hist = get_memory("WEAK_HIST_" + cache_key, "")

            if raw_state == "STRONG":
                if current_weak_hist == "wtt":
                    return "wtt to strong"
                if current_weak_hist == "wtb":
                    return "wtb to strong"
                if strike != prev_strike:
                    return "strong"
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
        elif ceVolIsStable100 and not ceOiIsStable100 and (ceVolTop['second'] and ceVolTop['second']['strike'] > ceOiTop['highest']['strike']) and (ceVolTop['highest']['strike'] < ceVolTop['highest']['strike']):
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
        elif peVolTop['highest']['strike'] == peOiTop['highest']['strike'] and peVolIsStable100 and not peOiIsStable100 and (peVolTop['second'] and peVolTop['second']['strike'] < peOiTop['highest']['strike']):
            peWinner = "VOL"
        elif peOiIsStable100 and not peVolIsStable100 and (peVolTop['second'] and peVolTop['second']['strike'] < peOiTop['highest']['strike']) and (peOiTop['highest']['strike'] > peVolTop['highest']['strike']):
            peWinner = "OI"
        elif peVolIsStable100 and not peOiIsStable100 and (peVolTop['second'] and peVolTop['second']['strike'] < peOiTop['highest']['strike']) and (peOiTop['highest']['strike'] > peVolTop['highest']['strike']):
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

        if ceOiRawState == "wtt" or ceOiRawState == "wtb": set_memory("WEAK_HIST_CE_OI", ceOiRawState)
        if ceVolRawState == "wtt" or ceVolRawState == "wtb": set_memory("WEAK_HIST_CE_VOL", ceVolRawState)
        if peOiRawState == "wtt" or peOiRawState == "wtb": set_memory("WEAK_HIST_PE_OI", peOiRawState)
        if peVolRawState == "wtt" or peVolRawState == "wtb": set_memory("WEAK_HIST_PE_VOL", peVolRawState)

        current_time_str = now_dt.strftime("%I:%M %p").lower()

        def calculate_independent_cell(top_obj, cache_key, side_label, is_winner, raw_state, final_status_text):
            if not top_obj or not top_obj.get('highest') or not top_obj['highest'].get('strike') or top_obj['highest']['strike'] == 0:
                return {'text': "(-----)", 'status': "stable strong", 'rawState': "STRONG", 'scenarioStatus': "stable strong"}

            strike = top_obj['highest']['strike']
            second_strike = strike
            if top_obj.get('second') and top_obj['second'].get('val', 0) > 0:
                second_strike = top_obj['second']['strike']

            prev_strike_str = get_memory("PREV_STRK_" + cache_key)
            prev_strike = float(prev_strike_str) if prev_strike_str else strike

            if not prev_strike_str:
                set_memory("PREV_STRK_" + cache_key, str(strike))
                prev_strike = strike

            display_status = final_status_text
            scenario_status = final_status_text

            is_shifting = False
            shift_type = get_memory("SHIFT_TYPE_" + cache_key, "")
            shift_from_strike = float(get_memory("SHIFT_FROM_" + cache_key, 0)) if get_memory("SHIFT_FROM_" + cache_key) else 0.0
            last_target_strike = float(get_memory("SHIFT_TARGET_" + cache_key, 0)) if get_memory("SHIFT_TARGET_" + cache_key) else 0.0

            if raw_state == "STRONG" or "to strong" in final_status_text or final_status_text == "stable strong" or (last_target_strike > 0 and second_strike != last_target_strike):
                set_memory("IS_SHIFTED_" + cache_key, "false")
                set_memory("PREV_STRK_" + cache_key, str(strike))
                remove_memory("SHIFT_TYPE_" + cache_key)
                remove_memory("SHIFT_FROM_" + cache_key)
                remove_memory("SHIFT_TARGET_" + cache_key)
                is_shifting = False
                scenario_status = final_status_text
            else:
                if strike != prev_strike and prev_strike > 0 and (final_status_text == "wtb" or final_status_text == "wtt"):
                    if strike < prev_strike:
                        shift_type = "top to bottom"
                        scenario_status = "wtb"
                    else:
                        shift_type = "bottom to top"
                        scenario_status = "wtt"
                    set_memory("IS_SHIFTED_" + cache_key, "true")
                    set_memory("SHIFT_TYPE_" + cache_key, shift_type)
                    set_memory("SHIFT_FROM_" + cache_key, str(prev_strike))
                    set_memory("SHIFT_TARGET_" + cache_key, str(second_strike))

                if get_memory("IS_SHIFTED_" + cache_key) == "true":
                    is_shifting = True
                    shift_type = get_memory("SHIFT_TYPE_" + cache_key)
                    scenario_status = "wtb" if shift_type == "top to bottom" else "wtt"
                else:
                    set_memory("PREV_STRK_" + cache_key, str(strike))
                    scenario_status = final_status_text

            if not is_winner and raw_state != "STRONG":
                display_status = "stable strong"
                scenario_status = "stable strong"

            display_time = get_memory("TIME_" + cache_key)
            last_recorded_status = get_memory("LAST_STATUS_" + cache_key, "")

            is_trigger_status = (
                display_status == "wtb" or
                display_status == "wtt" or
                display_status == "wtb to strong" or
                display_status == "wtt to strong" or
                is_shifting
            )

            if is_trigger_status and display_status != last_recorded_status:
                display_time = current_time_str
                set_memory("TIME_" + cache_key, display_time)
                set_memory("LAST_STATUS_" + cache_key, display_status)
            elif not display_time:
                display_time = current_time_str
                set_memory("TIME_" + cache_key, display_time)
                set_memory("LAST_STATUS_" + cache_key, display_status)

            return {'status': display_status, 'scenarioStatus': scenario_status, 'rawState': raw_state}

        ceOiResult  = calculate_independent_cell(ceOiTop, "CE_OI", "Eor OI", ceWinner == "OI" or ceWinner == "BOTH", ceOiRawState, ceOiStatusText)
        ceVolResult = calculate_independent_cell(ceVolTop, "CE_VOL", "Eor VOL", ceWinner == "VOL" or ceWinner == "BOTH", ceVolRawState, ceVolStatusText)
        peVolResult = calculate_independent_cell(peVolTop, "PE_VOL", "Eos VOL", peWinner == "VOL" or peWinner == "BOTH", peVolRawState, peVolStatusText)
        peOiResult  = calculate_independent_cell(peOiTop, "PE_OI", "Eos OI", peWinner == "OI" or peWinner == "BOTH", peOiRawState, peOiStatusText)

        finalCeState = ceOiResult['scenarioStatus'] if ceWinner == "OI" else ceVolResult['scenarioStatus']
        finalPeState = peOiResult['scenarioStatus'] if peWinner == "OI" else peVolResult['scenarioStatus']

        if ceWinner == "BOTH": finalCeState = ceVolResult['scenarioStatus'] if "to strong" in ceVolResult['scenarioStatus'] else ceOiResult['scenarioStatus']
        if peWinner == "BOTH": finalPeState = peVolResult['scenarioStatus'] if "to strong" in peVolResult['scenarioStatus'] else peOiResult['scenarioStatus']

        checkCe = finalCeState
        checkPe = finalPeState

        k3Text = f"{active_current_symbol} is Neutral"
        if checkCe == "wtt" and checkPe == "wtb": k3Text = f"{active_current_symbol} is 8th scenario both side Risky"
        elif checkCe == "wtb" and checkPe == "wtt": k3Text = f"{active_current_symbol} is 9th scenario both side Risky"
        elif checkCe == "stable strong" and checkPe == "stable strong": k3Text = f"{active_current_symbol} is Neutral"
        elif checkCe == "wtb" and checkPe == "stable strong": k3Text = f"{active_current_symbol} is slightly Bearish"
        elif checkCe == "stable strong" and checkPe == "wtb": k3Text = f"{active_current_symbol} is slightly Bearish"
        elif checkCe == "wtt" and checkPe == "stable strong": k3Text = f"{active_current_symbol} is slightly bullish"
        elif checkCe == "stable strong" and checkPe == "wtt": k3Text = f"{active_current_symbol} is slightly bullish"
        elif checkCe == "wtt" and checkPe == "wtt": k3Text = f"{active_current_symbol} is Bullrun"
        elif checkCe == "wtb" and checkPe == "wtb": k3Text = f"{active_current_symbol} is bloodbath"
        elif checkCe == "wtb" and checkPe == "wtt to strong": k3Text = f"{active_current_symbol} is confirm Bloodbath"
        elif checkCe == "wtt to strong" and checkPe == "wtb": k3Text = f"{active_current_symbol} is confirm Bloodbath"
        elif checkCe == "wtt" and checkPe == "wtt to strong": k3Text = f"{active_current_symbol} is slightly bullish (Both side trade)"
        elif checkCe == "wtt to strong" and checkPe == "wtt": k3Text = f"{active_current_symbol} is slightly bullish (Both side trade)"
        elif checkCe == "wtt" and checkPe == "wtb to strong": k3Text = f"{active_current_symbol} is confirm Bull Run"
        elif checkCe == "wtb to strong" and checkPe == "wtt": k3Text = f"{active_current_symbol} is confirm Bull Run"
        elif checkCe == "wtb to strong" and checkPe == "wtb": k3Text = f"{active_current_symbol} is slightly Bearish (Both side trade)"
        elif checkCe == "wtb" and checkPe == "wtb to strong": k3Text = f"{active_current_symbol} is slightly Bearish (both side trade)"
        elif checkCe == "wtb to strong" and checkPe == "stable strong": k3Text = f"{active_current_symbol} is slightly Bullish"
        elif checkCe == "stable strong" and checkPe == "wtb to strong": k3Text = f"{active_current_symbol} is slightly Bullish"
        elif checkCe == "wtt to strong" and checkPe == "stable strong": k3Text = f"{active_current_symbol} is slightly Bearish"
        elif checkCe == "stable strong" and checkPe == "wtt to strong": k3Text = f"{active_current_symbol} is slightly Bearish"
        elif checkCe == "wtt to strong" and checkPe == "wtb to strong": k3Text = f"{active_current_symbol} is 8th scenario both side Risky trade"
        elif checkCe == "wtb to strong" and checkPe == "wtt to strong": k3Text = f"{active_current_symbol} is 9th scenario both side Risky trade"
        elif checkCe == "wtb to strong" and checkPe == "wtb to strong": k3Text = f"{active_current_symbol} is confirm Bull Run"
        elif checkCe == "wtt to strong" and checkPe == "wtt to strong": k3Text = f"{active_current_symbol} is confirm bloodbath"

        return checkCe, checkPe, k3Text
    except Exception as e:
        return "stable strong", "stable strong", f"{symbol} is Neutral"


# ==========================================
# BACKGROUND 265-SYMBOL TRACKER ENGINE
# ==========================================
def background_all_symbols_tracker_loop(smart_api):
    global options_cache, eq_cache, active_current_symbol
    print("🚀 Background 265-Symbol Memory Tracker with Exact Timestamps Started...")
    while True:
        try:
            if not options_cache:
                time.sleep(10)
                continue

            now_dt = datetime.now()
            current_time_str = now_dt.strftime("%H:%M")

            for sym_name, sym_options in list(options_cache.items()):
                if sym_options.empty:
                    continue
                
                clean_sym = sym_name.replace("-EQ", "").replace(".NS", "").strip()
                if clean_sym in ["TMCV", "TATA", "TATAMOTORS"]:
                    clean_sym = "TATAMOTORS"

                # Isolate namespace memory for this specific symbol
                active_current_symbol = clean_sym

                spot_token, spot_exchange = "", "NSE"
                if clean_sym in eq_cache:
                    spot_token, spot_exchange = eq_cache[clean_sym]
                elif clean_sym + "-EQ" in eq_cache:
                    spot_token, spot_exchange = eq_cache[clean_sym + "-EQ"]

                spot_price = 0.0
                if spot_token:
                    try:
                        spot_quote = smart_api.getMarketData("FULL", {spot_exchange: [spot_token]})
                        if spot_quote and spot_quote.get('status') and spot_quote.get('data'):
                            item = spot_quote['data']['fetched'][0]
                            spot_price = float(item.get('ltp', item.get('LTP', item.get('close', 0))))
                    except Exception:
                        pass

                today_date = pd.Timestamp(now_dt.date())
                valid_opts = sym_options[sym_options['parsed_expiry'] >= today_date].copy()
                if valid_opts.empty:
                    continue

                unique_expiries_dt = sorted(valid_opts['parsed_expiry'].unique())
                current_expiry_dt = unique_expiries_dt[0]
                current_expiry_options = valid_opts[valid_opts['parsed_expiry'] == current_expiry_dt].copy()

                if spot_price <= 0:
                    all_strikes_list = sorted(current_expiry_options['strike'].unique())
                    spot_price = float(all_strikes_list[len(all_strikes_list) // 2]) if all_strikes_list else 100.0

                all_master_strikes = sorted(current_expiry_options['strike'].unique())
                if all_master_strikes:
                    closest_idx = min(range(len(all_master_strikes)), key=lambda i: abs(all_master_strikes[i] - spot_price))
                    strikes = all_master_strikes[max(0, closest_idx - 5):min(len(all_master_strikes), closest_idx + 6)]
                else:
                    strikes = []

                filtered_opts = current_expiry_options[current_expiry_options['strike'].isin(strikes)].copy()
                exchange_tokens_map = {}
                for _, row in filtered_opts.iterrows():
                    exch, tok = str(row['exch_seg']).strip(), str(row['token']).strip()
                    if exch not in exchange_tokens_map: exchange_tokens_map[exch] = []
                    if tok not in exchange_tokens_map[exch]: exchange_tokens_map[exch].append(tok)

                all_fetched_quotes = []
                for exch, tokens_list in exchange_tokens_map.items():
                    for i in range(0, len(tokens_list), 40):
                        batch_tokens = tokens_list[i:i+40]
                        if not batch_tokens: continue
                        try:
                            res = smart_api.getMarketData("FULL", {exch: batch_tokens})
                            if res and res.get('status') and res.get('data'):
                                all_fetched_quotes.extend(res['data'].get('fetched', []))
                        except Exception:
                            pass

                # Build temporary rows to simulate dashboard grid evaluation for this symbol
                quotes_dict = {}
                for item in all_fetched_quotes:
                    tok = str(item.get('symbolToken', item.get('symboltoken', item.get('token', '')))).strip()
                    ltp = float(item.get('ltp', item.get('LTP', item.get('close', 0))))
                    volume = float(item.get('tradeVolume', item.get('volume', 0)))
                    oi = float(item.get('opnInterest', item.get('oi', 0)))
                    quotes_dict[tok] = {'ltp': ltp, 'volume': volume, 'oi': oi}

                # Run robust background scenario analysis with exact timestamp memory preservation
                ce_state, pe_state, scenario_text = compute_symbol_scenario_in_memory(clean_sym, current_expiry_options, strikes, quotes_dict, spot_price, now_dt)
                
                # Log the actual evaluated states with exact timestamps
                log_symbol_scenario(clean_sym, ce_state, pe_state, scenario_text, current_time_str)
                
                time.sleep(0.3)

        except Exception as bg_err:
            print(f"⚠ Background Tracker Error: {bg_err}")
            time.sleep(5)
        time.sleep(20)


# ==========================================
# FETCH & UPDATE LOOP (PRIMARY ACTIVE SYMBOL)
# ==========================================
def fetch_and_update_option_chain(smart_api):
    global morning_oi_dict, baseline_date, cached_selected_symbol, options_cache, eq_cache, active_current_symbol
    try:
        now_dt = datetime.now()
        current_date_str = now_dt.strftime('%Y-%m-%d')

        if baseline_date != current_date_str:
            morning_oi_dict = {}
            baseline_date = current_date_str

        if not options_cache:
            initialize_scrip_master(smart_api)
            if not options_cache: return

        raw_selected_symbol = cached_selected_symbol if cached_selected_symbol else "RELIANCE"
        try:
            val = str(dashboard_sheet.cell(4, 10).value).strip().upper()
            if val and val not in ["NONE", "NAN", "0"]:
                raw_selected_symbol = val
        except Exception:
            pass

        clean_symbol = raw_selected_symbol.replace("-EQ", "").replace(".NS", "").strip()
        if clean_symbol in ["TATAMOTORS", "TATA"]: clean_symbol = "TMCV"

        active_current_symbol = clean_symbol

        if cached_selected_symbol != clean_symbol:
            print(f"\n⚡ Active Symbol Switched To: {clean_symbol} (Memory Retained)")
            cached_selected_symbol = clean_symbol

        spot_token, spot_exchange = "", "NSE"
        if clean_symbol in eq_cache:
            spot_token, spot_exchange = eq_cache[clean_symbol]
        elif clean_symbol + "-EQ" in eq_cache:
            spot_token, spot_exchange = eq_cache[clean_symbol + "-EQ"]

        spot_price = 0.0
        if spot_token:
            try:
                spot_quote = smart_api.getMarketData("FULL", {spot_exchange: [spot_token]})
                if spot_quote and spot_quote.get('status') and spot_quote.get('data'):
                    item = spot_quote['data']['fetched'][0]
                    spot_price = float(item.get('ltp', item.get('LTP', item.get('close', 0))))
            except Exception:
                pass

        sym_options = options_cache.get(clean_symbol, pd.DataFrame())
        if sym_options.empty:
            for key in options_cache:
                if clean_symbol in key:
                    sym_options = options_cache[key]
                    break
        if sym_options.empty: return

        today_date = pd.Timestamp(now_dt.date())
        valid_opts = sym_options[sym_options['parsed_expiry'] >= today_date].copy()
        if valid_opts.empty: return

        unique_expiries_dt = sorted(valid_opts['parsed_expiry'].unique())
        expiry_strings_sorted = [pd.Timestamp(dt).strftime('%d%b%Y').upper() for dt in unique_expiries_dt]
        active_expiries = expiry_strings_sorted[:4]
        while len(active_expiries) < 4: active_expiries.append(active_expiries[-1] if active_expiries else "N/A")

        current_expiry = active_expiries[0]
        current_expiry_dt = unique_expiries_dt[0]
        current_expiry_options = valid_opts[valid_opts['parsed_expiry'] == current_expiry_dt].copy()

        lot_size = int(float(current_expiry_options['lotsize'].iloc[0])) if 'lotsize' in current_expiry_options.columns and not current_expiry_options['lotsize'].empty else 1

        if spot_price <= 0:
            all_strikes_list = sorted(current_expiry_options['strike'].unique())
            spot_price = float(all_strikes_list[len(all_strikes_list) // 2]) if all_strikes_list else 100.0

        try:
            safe_update_cell(data_sheet, "K9", spot_price)
            safe_update_cell(data_sheet, "L9", str(current_expiry))
            safe_update_cell(data_sheet, "A7", lot_size)
            expiry_cells = [['A3', active_expiries[0]], ['A4', active_expiries[1]], ['A5', active_expiries[2]], ['A6', active_expiries[3]]]
            for cell, val in expiry_cells:
                safe_update_cell(data_sheet, cell, val)
        except Exception:
            pass

        all_master_strikes = sorted(current_expiry_options['strike'].unique())
        if all_master_strikes:
            closest_idx = min(range(len(all_master_strikes)), key=lambda i: abs(all_master_strikes[i] - spot_price))
            strikes = all_master_strikes[max(0, closest_idx - 10):min(len(all_master_strikes), closest_idx + 11)]
        else:
            strikes = []

        filtered_opts = current_expiry_options[current_expiry_options['strike'].isin(strikes)].copy()
        exchange_tokens_map = {}
        for _, row in filtered_opts.iterrows():
            exch, tok = str(row['exch_seg']).strip(), str(row['token']).strip()
            if exch not in exchange_tokens_map: exchange_tokens_map[exch] = []
            if tok not in exchange_tokens_map[exch]: exchange_tokens_map[exch].append(tok)

        all_fetched_quotes = []
        for exch, tokens_list in exchange_tokens_map.items():
            for i in range(0, len(tokens_list), 40):
                batch_tokens = tokens_list[i:i+40]
                if not batch_tokens: continue
                try:
                    res = smart_api.getMarketData("FULL", {exch: batch_tokens})
                    if res and res.get('status') and res.get('data'):
                        all_fetched_quotes.extend(res['data'].get('fetched', []))
                except Exception:
                    pass

        market_open_time = datetime.strptime("09:15:00", "%H:%M:%S").time()
        is_market_open_passed = now_dt.time() >= market_open_time

        quotes_dict = {}
        for item in all_fetched_quotes:
            tok = str(item.get('symbolToken', item.get('symboltoken', item.get('token', '')))).strip()
            ltp = float(item.get('ltp', item.get('LTP', item.get('close', 0))))
            volume = float(item.get('tradeVolume', item.get('volume', 0)))
            oi = float(item.get('opnInterest', item.get('oi', 0)))
            net_change = float(item.get('netChange', item.get('change', 0)))
            
            if is_market_open_passed and tok not in morning_oi_dict:
                morning_oi_dict[tok] = oi

            base_oi = morning_oi_dict.get(tok, oi)
            quotes_dict[tok] = {'ltp': ltp, 'volume': volume, 'oi': oi, 'oi_chg': oi - base_oi, 'net_change': net_change}

        exp_dt = datetime.strptime(str(current_expiry).title(), "%d%b%Y") if current_expiry != "N/A" else datetime.now()
        exp_date = exp_dt.replace(hour=15, minute=30, second=0)
        t = max((exp_date - datetime.now()).total_seconds() / (365.0 * 24 * 3600), 0.0001)
        r = 0.07

        output_rows = [[
            "CE Vega", "CE Theta", "CE Gamma", "CE Delta", "CE IV", "CE OI Chg", "CE OI", "CE Vol", "CE Change", "CE LTP",
            "Strike Price",
            "PE LTP", "PE Change", "PE Vol", "PE OI", "PE OI Chg", "PE IV", "PE Delta", "PE Gamma", "PE Theta", "PE Vega"
        ]]

        for st in strikes:
            ce_sub = current_expiry_options[(current_expiry_options['strike'] == st) & (current_expiry_options['symbol'].astype(str).str.upper().str.endswith('CE'))]
            pe_sub = current_expiry_options[(current_expiry_options['strike'] == st) & (current_expiry_options['symbol'].astype(str).str.upper().str.endswith('PE'))]

            ce_token = str(ce_sub['token'].values[0]).strip() if not ce_sub.empty else ""
            pe_token = str(pe_sub['token'].values[0]).strip() if not pe_sub.empty else ""

            ce_q = quotes_dict.get(ce_token, {'ltp': 0, 'volume': 0, 'oi': 0, 'oi_chg': 0, 'net_change': 0})
            pe_q = quotes_dict.get(pe_token, {'ltp': 0, 'volume': 0, 'oi': 0, 'oi_chg': 0, 'net_change': 0})

            ce_gks = calculate_iv_and_greeks('CE', spot_price, st, t, r, ce_q['ltp'])
            pe_gks = calculate_iv_and_greeks('PE', spot_price, st, t, r, pe_q['ltp'])

            ce_iv_val = ce_gks['iv'] if ce_gks['iv'] > 1.0 else 0
            pe_iv_val = pe_gks['iv'] if pe_gks['iv'] > 1.0 else 0

            output_rows.append([
                ce_gks['vega'], ce_gks['theta'], ce_gks['gamma'], ce_gks['delta'], ce_iv_val, ce_q['oi_chg'], ce_q['oi'], ce_q['volume'], ce_q['net_change'], ce_q['ltp'],
                st,
                pe_q['ltp'], pe_q['net_change'], pe_q['volume'], pe_q['oi'], pe_q['oi_chg'], pe_iv_val, pe_gks['delta'], pe_gks['gamma'], pe_gks['theta'], pe_gks['vega']
            ])

        if len(output_rows) > 1:
            last_row_num = 10 + len(output_rows) - 1
            data_sheet.update(range_name=f"A10:U{last_row_num}", values=output_rows)
            try:
                data_sheet.batch_clear([f"A{last_row_num + 1}:U100"])
            except Exception:
                pass

        execute_python_scenario_tracker()

    except Exception as ex:
        print(f"❌ Exception in fetch loop: {str(ex)}")
            

def initialize_scrip_master(smart_api):
    global full_scrip_df, eq_cache, options_cache
    try:
        print("📥 Downloading Angel One Scrip Master JSON...")
        url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
        resp = requests.get(url, timeout=25)
        if resp.status_code == 200:
            scrip_data = resp.json()
            full_scrip_df = pd.DataFrame(scrip_data)
            if 'symboltoken' in full_scrip_df.columns and 'token' not in full_scrip_df.columns:
                full_scrip_df['token'] = full_scrip_df['symboltoken']
            if 'tradingsymbol' in full_scrip_df.columns and 'symbol' not in full_scrip_df.columns:
                full_scrip_df['symbol'] = full_scrip_df['tradingsymbol']
            if 'strike' in full_scrip_df.columns:
                full_scrip_df['strike'] = pd.to_numeric(full_scrip_df['strike'], errors='coerce').fillna(0) / 100.0

            nse_eq = full_scrip_df[(full_scrip_df['exch_seg'] == 'NSE') & (~full_scrip_df['instrumenttype'].str.contains('OPT|FUT', na=False))]
            for _, row in nse_eq.iterrows():
                sym, name, tok = str(row['symbol']).strip().upper(), str(row['name']).strip().upper(), str(row['token']).strip()
                eq_cache[sym] = (tok, 'NSE')
                eq_cache[name] = (tok, 'NSE')
                if sym.endswith("-EQ"): eq_cache[sym.replace("-EQ", "")] = (tok, 'NSE')

            fno_opts = full_scrip_df[(full_scrip_df['strike'] > 0) & (full_scrip_df['exch_seg'] == 'NFO')].copy()
            fno_opts['strike'] = pd.to_numeric(fno_opts['strike'], errors='coerce')
            fno_opts['parsed_expiry'] = pd.to_datetime(fno_opts['expiry'], format='%d%b%Y', errors='coerce').fillna(pd.to_datetime(fno_opts['expiry'], errors='coerce'))
            
            for name, group in fno_opts.groupby(fno_opts['name'].str.upper()):
                options_cache[name.strip()] = group

            if "TMCV" in options_cache:
                options_cache["TATAMOTORS"] = options_cache["TMCV"]
                options_cache["TATA"] = options_cache["TMCV"]

            print(f"✅ Scrip Master Cached! Total Records: {len(full_scrip_df)}")
    except Exception as e:
        print(f"⚠ Scrip Master Error: {e}")

def reset_daily_memory():
    global all_symbols_global_history, symbol_memories
    all_symbols_global_history = {}
    symbol_memories = {}
    print("🔄 Daily reset complete: All symbol calculation memories cleared at market open (9:15).")

full_scrip_df = pd.DataFrame()
eq_cache = {}
options_cache = {}
morning_oi_dict = {}
baseline_date = None
cached_selected_symbol = None

def run_bot_lock_loop():
    print("🚀 Starting Bot with Full 265-Symbol Memory & Timestamp Engine...")
    reset_daily_memory()
    try:
        smart_api = SmartConnect(api_key=API_KEY)
    except Exception as e:
        print(f"❌ SmartConnect Init Error: {e}")
        return
        
    while True:
        try:
            print("🔄 Attempting to generate Angel One session...")
            totp = pyotp.TOTP(TOTP_SECRET.replace(" ", "")).now() if TOTP_SECRET else "123456"
            data = smart_api.generateSession(CLIENT_ID, PIN, totp)
            if data and data.get('status'):
                print("✅ Session Generated Successfully!")
                initialize_scrip_master(smart_api)
                
                # Start background thread for 265 symbols
                bg_thread = threading.Thread(target=background_all_symbols_tracker_loop, args=(smart_api,), daemon=True)
                bg_thread.start()
                break
            else:
                print(f"⚠ Session Failed: {data}")
                time.sleep(5)
        except Exception as e:
            print(f"❌ Session Exception: {e}")
            time.sleep(5)

    while True:
        try:
            fetch_and_update_option_chain(smart_api)
        except Exception as _e:
            err_msg = str(_e).lower()
            print(f"❌ Loop Error: {_e}")
            if "session" in err_msg or "token" in err_msg or "unauthorized" in err_msg or "invalid" in err_msg:
                print("🔄 Session expired detected! Attempting auto-reconnect...")
                reconnected = False
                while not reconnected:
                    try:
                        totp = pyotp.TOTP(TOTP_SECRET.replace(" ", "")).now() if TOTP_SECRET else "123456"
                        data = smart_api.generateSession(CLIENT_ID, PIN, totp)
                        if data and data.get('status'):
                            print("✅ Session Re-Generated Automatically!")
                            reconnected = True
                        else:
                            print(f"⚠ Auto-Reconnect Failed: {data}. Retrying in 10 seconds...")
                            time.sleep(10)
                    except Exception as recon_err:
                        print(f"❌ Auto-Reconnect Exception: {recon_err}. Retrying in 10 seconds...")
                        time.sleep(10)
        time.sleep(1)

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    run_bot_lock_loop()
