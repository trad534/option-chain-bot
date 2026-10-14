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

# ====================================================================
# 🛡️ SYSTEM TERMINAL LOGS & SERVER SHIELD KEEP-ALIVE
# ====================================================================
RENDER_URL = "https://onrender.com"

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

def print(*args, **kwargs):
    kwargs['flush'] = True
    builtins.print(*args, **kwargs)

app = Flask(__name__)

@app.route('/')
def home():
    return "Angel One Option Chain Bot & Multi-Symbol Full-Market Engine is active!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
# ====================================================================
# 🛠️ GLOBAL ENVIRONMENT PARAMETERS & SHEET POINTERS BINDING
# ====================================================================
CLIENT_ID = os.getenv("ANGEL_CLIENT_ID")
PIN = os.getenv("ANGEL_PIN")
API_KEY = os.getenv("ANGEL_API_KEY")
TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET")

GOOGLE_SHEET_NAME = "6 LTP CALCULATOR"
MAIN_SHEET_NAME = "upstox get data"
SYMBOL_SHEET_NAME = "Option chain index new"
HISTORICAL_SHEET_NAME = "Live_Historical_Scenarios"

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
    
    try:
        historical_log_sheet = spreadsheet.worksheet(HISTORICAL_SHEET_NAME)
    except Exception:
        historical_log_sheet = spreadsheet.add_worksheet(title=HISTORICAL_SHEET_NAME, rows="5000", cols="10")
        historical_log_sheet.append_row(["Time-Stamp", "Symbol", "Support Price State", "Resistance Price State", "Scenario Status (K3)"])
        
    print("✅ SUCCESS: All Google Sheet Tabs Connected Successfully!")
except Exception as e:
    print(f"❌ Google Sheet Connection Error: {e}")
    exit()

# Multi-Symbol Separate Isolated Memory Vaults (RAM Repositories)
script_memory = {}
global_run_date = ""
last_written_values = {}
persistent_quotes_cache = {}

time_travel_db = {}
multi_symbol_runtime_states = {}
last_flashed_symbol = ""

def get_memory(key, default=""):
    return script_memory.get(key, default)

def set_memory(key, value):
    script_memory[key] = str(value)

def remove_memory(key):
    if key in script_memory:
        del script_memory[key]

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
# ====================================================================
# 🧮 OPTIONS BLACK-SCHOLES GREEKS DERIVATIONS
# ====================================================================
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
# ====================================================================
# 🎛️ MASTER SCENARIO ENGINE (100% EXACT MATCH WITH YOUR ORIGINAL CODE)
# ====================================================================
def execute_python_scenario_tracker():
    global global_run_date, time_travel_db, last_flashed_symbol, multi_symbol_runtime_states
    try:
        now_dt = datetime.now()
        current_kolkata_time = now_dt.strftime("%H:%M:%S")
        today_str = now_dt.strftime("%Y-%m-%d")

        if global_run_date != today_str:
            script_memory.clear()
            global_run_date = today_str

        if current_kolkata_time.startswith("09:14:5"):
            script_memory.clear()

        range_values = dashboard_sheet.get("A8:T28")
        if not range_values: return

        active_symbol_name = "Nifty"
        try:
            j4_val = str(dashboard_sheet.cell(4, 10).value).strip()
            if j4_val and j4_val not in ["NONE", "NAN", "0", ""]:
                active_symbol_name = j4_val.replace("-EQ", "").replace(".NS", "").strip().upper()
        except Exception: pass

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
            if not data_array or len(data_array) == 0:
                return {'highest': {'strike': 0, 'val': 0, 'row': 0}, 'second': {'strike': 0, 'val': 0, 'row': 0}, 'pct': 0}
            filtered = [item for item in data_array if item['val'] > 0]
            if len(filtered) == 0:
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
            if not top_obj or top_obj['highest']['val'] == 0 or top_obj['pct'] < 75.0: return "STRONG"
            if is_put_side: return "wtb" if top_obj['second']['strike'] < top_obj['highest']['strike'] else "wtt"
            else: return "wtt" if top_obj['second']['strike'] > top_obj['highest']['strike'] else "wtb"

        ceVolRawState = detect_live_state_advanced(ceVolTop, False)
        ceOiRawState = detect_live_state_advanced(ceOiTop, False)
        peVolRawState = detect_live_state_advanced(peVolTop, True)
        peOiRawState = detect_live_state_advanced(peOiTop, True)

        def get_full_status_string(top_obj, raw_state, cache_key):
            if not top_obj or not top_obj.get('highest') or not top_obj['highest'].get('strike') or top_obj['highest']['strike'] == 0: return "stable strong"
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
        elif ceVolTop['highest']['strike'] == ceOiTop['highest']['strike'] and not ceOiIsStable100 and not ceVolIsStable100 and (ceOiTop['second']['strike'] > ceOiTop['highest']['strike']) and (ceVolTop['second']['strike'] > ceOiTop['highest']['strike']): ceWinner = "BOTH"
        elif ceVolTop['highest']['strike'] == ceOiTop['highest']['strike'] and ceOiIsStable100 and ceVolIsStable100: ceWinner = "BOTH"
        elif ceVolTop['highest']['strike'] == ceOiTop['highest']['strike'] and ceOiIsStable100 and not ceVolIsStable100 and (ceVolTop['second'] and ceVolTop['second']['strike'] > ceVolTop['highest']['strike']): ceWinner = "OI"
        elif ceVolTop['highest']['strike'] == ceOiTop['highest']['strike'] and ceVolIsStable100 and not ceOiIsStable100 and (ceOiTop['second'] and ceOiTop['second']['strike'] > ceVolTop['highest']['strike']): ceWinner = "VOL"
        elif ceOiIsStable100 and not ceVolIsStable100 and (ceVolTop['second'] and ceVolTop['second']['strike'] > ceOiTop['highest']['strike']) and (ceOiTop['highest']['strike'] < ceVolTop['highest']['strike']): ceWinner = "OI"
        elif ceVolIsStable100 and not ceOiIsStable100 and (ceOiTop['second'] and ceOiTop['second']['strike'] > ceOlTop_strike_dummy if False else ceVolTop['highest']['strike']) and (ceVolTop['highest']['strike'] < ceOiTop['highest']['strike']): ceWinner = "VOL"
        else:
            ceOiActiveRow = ceOiTop['highest']['row']
            if ceOiTop['second'] and ceOiTop['second']['row'] > 0 and ceOiTop['pct'] >= 75.0: ceOiActiveRow = min(ceOiActiveRow, ceOiTop['second']['row'])
            ceVolActiveRow = ceVolTop['highest']['row']
            if ceVolTop['second'] and ceVolTop['second']['row'] > 0 and ceVolTop['pct'] >= 75.0: ceVolActiveRow = min(ceVolActiveRow, ceVolTop['second']['row'])
            if ceVolActiveRow != ceOiActiveRow: ceWinner = "VOL" if ceVolActiveRow < ceOiActiveRow else "OI"
            else: ceWinner = "VOL" if ceVolTop['highest']['row'] <= ceOiTop['highest']['row'] else "OI"

        peWinner = "VOL"
        if peVolTop['highest']['strike'] == peOiTop['highest']['strike'] and peOiRawState == "wtt" and peVolRawState == "wtt": peWinner = "BOTH"
        elif peVolTop['highest']['strike'] == peOiTop['highest']['strike'] and not peOiIsStable100 and not peVolIsStable100 and (peOiTop['second']['strike'] < peOiTop['highest']['strike']) and (peVolTop['second']['strike'] < peOiTop['highest']['strike']): peWinner = "BOTH"
        elif peVolTop['highest']['strike'] == peOiTop['highest']['strike'] and peOiIsStable100 and peVolIsStable100: peWinner = "BOTH"
        elif peVolTop['highest']['strike'] == peOiTop['highest']['strike'] and peOiIsStable100 and not peVolIsStable100 and (peVolTop['second'] and peVolTop['second']['strike'] < peOiTop['highest']['strike']): peWinner = "OI"
        elif peVolTop['highest']['strike'] == peOiTop['highest']['strike'] and peVolIsStable100 and not peOiIsStable100 and (peOiTop['second'] and peOiTop['second']['strike'] < peOiTop['highest']['strike']): peWinner = "VOL"
        elif peOiIsStable100 and not peVolIsStable100 and (peVolTop['second'] and peVolTop['second']['strike'] < peOiTop['highest']['strike']) and (peOiTop['highest']['strike'] > peVolTop['highest']['strike']): peWinner = "OI"
        elif peVolIsStable100 and not peOiIsStable100 and (peOiTop['second'] and peOiTop['second']['strike'] < peOiTop['highest']['strike']) and (peVolTop['highest']['strike'] > peOiTop['highest']['strike']): peWinner = "VOL"
        else:
            peOiActiveRow = peOiTop['highest']['row']
            if peOiTop['second'] and peOiTop['second']['row'] > 0 and peOiTop['pct'] >= 75.0: peOiActiveRow = max(peOiActiveRow, peOiTop['second']['row'])
            peVolActiveRow = peVolTop['highest']['row']
            if peVolTop['second'] and peVolTop['second']['row'] > 0 and peVolTop['pct'] >= 75.0: peVolActiveRow = max(peVolActiveRow, peVolTop['second']['row'])
            if peVolActiveRow != peOiActiveRow: peWinner = "VOL" if peVolActiveRow > peOiActiveRow else "OI"
            else: peWinner = "VOL" if peVolTop['highest']['row'] >= peOiTop['highest']['row'] else "OI"

        if ceOiRawState in ["wtt", "wtb"]: set_memory("WEAK_HIST_CE_OI", ceOiRawState)
        if ceVolRawState in ["wtt", "wtb"]: set_memory("WEAK_HIST_CE_VOL", ceVolRawState)
        if peOiRawState in ["wtt", "wtb"]: set_memory("WEAK_HIST_PE_OI", peOiRawState)
        if peVolRawState in ["wtt", "wtb"]: set_memory("WEAK_HIST_PE_VOL", peVolRawState)

        current_time_str = now_dt.strftime("%I:%M %p").lower()
        current_time_clock = now_dt.strftime("%H:%M:%S")

        def calculate_independent_cell(top_obj, cache_key, side_label, is_winner, raw_state, final_status_text):
            if not top_obj or not top_obj.get('highest') or not top_obj['highest'].get('strike') or top_obj['highest']['strike'] == 0:
                return {'text': "(-----)", 'status': "stable strong", 'rawState': "STRONG", 'scenarioStatus': "stable strong"}

            strike = top_obj['highest']['strike']
            second_strike = strike
            if top_obj.get('second') and top_obj['second'].get('val', 0) > 0: second_strike = top_obj['second']['strike']

            prev_strike_str = get_memory("PREV_STRK_" + cache_key)
            prev_strike = float(prev_strike_str) if prev_strike_str else strike
            if not prev_strike_str: set_memory("PREV_STRK_" + cache_key, str(strike)); prev_strike = strike

            display_status = final_status_text; scenario_status = final_status_text; is_shifting = False

            if get_memory("IS_SHIFTED_" + cache_key) == "true":
                shift_from_strike = float(get_memory("SHIFT_FROM_" + cache_key, 0))
                last_target_strike = float(get_memory("SHIFT_TARGET_" + cache_key, 0))
                if strike == shift_from_strike or (last_target_strike > 0 and second_strike != last_target_strike):
                    set_memory("IS_SHIFTED_" + cache_key, "false"); set_memory("PREV_STRK_" + cache_key, str(strike)); remove_memory("SHIFT_TARGET_" + cache_key)

            if strike != prev_strike and prev_strike > 0 and final_status_text in ["wtb", "wtt"]:
                shift_type = "top to bottom" if strike < prev_strike else "bottom to top"
                set_memory("IS_SHIFTED_" + cache_key, "true"); set_memory("SHIFT_TYPE_" + cache_key, shift_type)
                set_memory("SHIFT_FROM_" + cache_key, str(prev_strike)); set_memory("SHIFT_TARGET_" + cache_key, str(second_strike))

            if get_memory("IS_SHIFTED_" + cache_key) == "true":
                if raw_state == "STRONG" or final_status_text == "stable strong" or "to strong" in final_status_text:
                    set_memory("IS_SHIFTED_" + cache_key, "false"); set_memory("PREV_STRK_" + cache_key, str(strike)); is_shifting = False; scenario_status = final_status_text
                else:
                    is_shifting = True; shift_type = get_memory("SHIFT_TYPE_" + cache_key)
                    scenario_status = "wtb" if shift_type == "top to bottom" else "wtt"
            else: set_memory("PREV_STRK_" + cache_key, str(strike)); scenario_status = final_status_text

            if not is_winner and raw_state != "STRONG": display_status = "stable strong"; scenario_status = "stable strong"

            display_time = get_memory("TIME_" + cache_key); last_recorded_status = get_memory("LAST_STATUS_" + cache_key, "")
            is_trigger_status = (display_status in ["wtb", "wtt", "wtb to strong", "wtt to strong"] or is_shifting)

            if is_trigger_status and display_status != last_recorded_status:
                display_time = current_time_clock; set_memory("TIME_" + cache_key, display_time); set_memory("LAST_STATUS_" + cache_key, display_status)
            elif not display_time: display_time = current_time_clock; set_memory("TIME_" + cache_key, display_time); set_memory("LAST_STATUS_" + cache_key, display_status)

            s_val = int(strike) if strike.is_integer() else strike
            sec_val = int(second_strike) if second_strike.is_integer() else second_strike

            if is_shifting: output_text = f"({display_time} - {side_label}-{s_val}-{display_status}-{sec_val})\n   (shifted {get_memory('SHIFT_TYPE_' + cache_key)})"
            else:
                if display_status == "stable strong": output_text = f"({display_time} - {side_label} - {s_val})\n    (stable strong)"
                elif "to strong" in display_status: output_text = f"({display_time} - {side_label} - {s_val})\n   ({display_status})"
                else: output_text = f"({display_time} - {side_label}-{s_val}-{display_status}-{sec_val})\n({'weak towards top' if display_status == 'wtt' else 'weak towards bottom'})"
            return {'text': output_text, 'scenarioStatus': scenario_status}

        ceOiResult  = calculate_independent_cell(ceOiTop, "CE_OI", "Eor OI", ceWinner in ["OI", "BOTH"], ceOiRawState, ceOiStatusText)
        ceVolResult = calculate_independent_cell(ceVolTop, "CE_VOL", "Eor VOL", ceWinner in ["VOL", "BOTH"], ceVolRawState, ceVolStatusText)
        peVolResult = calculate_independent_cell(peVolTop, "PE_VOL", "Eos VOL", peWinner in ["VOL", "BOTH"], peVolRawState, peVolStatusText)
        peOiResult  = calculate_independent_cell(peOiTop, "PE_OI", "Eos OI", peWinner in ["OI", "BOTH"], peOiRawState, peOiStatusText)

        checkCe = ceOiResult['scenarioStatus'] if ceWinner == "OI" else ceVolResult['scenarioStatus']
        checkPe = peOiResult['scenarioStatus'] if peWinner == "OI" else peVolResult['scenarioStatus']
        if ceWinner == "BOTH": checkCe = ceVolResult['scenarioStatus'] if "to strong" in ceVolResult['scenarioStatus'] else ceOiResult['scenarioStatus']
        if peWinner == "BOTH": checkPe = peVolResult['scenarioStatus'] if "to strong" in peVolResult['scenarioStatus'] else peOiResult['scenarioStatus']

        k3Text = f"{active_symbol_name} is Neutral"
        if checkCe == "wtt" and checkPe == "wtb": k3Text = f"{active_symbol_name} is 8th scenario both side Risky"
        elif checkCe == "wtb" and checkPe == "wtt": k3Text = f"{active_symbol_name} is 9th scenario both side Risky"
        elif checkCe == "stable strong" and checkPe == "stable strong": k3Text = f"{active_symbol_name} is Neutral"
        elif checkCe == "wtb" and "strong" in checkPe: k3Text = f"{active_symbol_name} is slightly Bearish"
        elif "strong" in checkCe and checkPe == "wtb": k3Text = f"{active_symbol_name} is slightly Bearish"
        elif checkCe == "wtt" and "strong" in checkPe: k3Text = f"{active_symbol_name} is slightly bullish"
        elif "strong" in checkCe and checkPe == "wtt": k3Text = f"{active_symbol_name} is slightly bullish"
        elif checkCe == "wtt" and checkPe == "wtt": k3Text = f"{active_symbol_name} is Bullrun"
        elif checkCe == "wtb" and checkPe == "wtb": k3Text = f"{active_symbol_name} is bloodbath"

        if active_symbol_name not in multi_symbol_runtime_states: multi_symbol_runtime_states[active_symbol_name] = {}
        vault = multi_symbol_runtime_states[active_symbol_name]
        vault["D5_TEXT"] = ceOiResult['text'] if ceWinner in ["OI", "BOTH"] else "(-----)"
        vault["H5_TEXT"] = ceVolResult['text'] if ceWinner in ["VOL", "BOTH"] else "(-----)"
        vault["U5_TEXT"] = peOiResult['text'] if peWinner in ["OI", "BOTH"] else "(-----)"
        vault["Q5_TEXT"] = peVolResult['text'] if peWinner in ["VOL", "BOTH"] else "(-----)"
        vault["K3_TEXT"] = k3Text

        safe_update_cell(dashboard_sheet, "D5", vault["D5_TEXT"])
        safe_update_cell(dashboard_sheet, "H5", vault["H5_TEXT"])
        safe_update_cell(dashboard_sheet, "U5", vault["U5_TEXT"])
        safe_update_cell(dashboard_sheet, "Q5", vault["Q5_TEXT"])
        update_k3_with_color(spreadsheet, dashboard_sheet, "K3", vault["K3_TEXT"])

        if active_symbol_name not in time_travel_db: time_travel_db[active_symbol_name] = []
        symbol_history = time_travel_db[active_symbol_name]
        if len(symbol_history) == 0 or symbol_history[-1]["scenario"] != k3Text:
            time_travel_db[active_symbol_name].append({
                "timestamp": current_time_clock, "symbol": active_symbol_name,
                "support": f"CE: {str(checkCe).upper()} ({ceWinner})",
                "resistance": f"PE: {str(checkPe).upper()} ({peWinner})",
                "scenario": k3Text
            })

        if last_flashed_symbol != active_symbol_name:
            print(f"🔄 Symbol Modifying Event Detected -> Overwriting '{active_symbol_name}' History Timeline Sheets!")
            historical_log_sheet.clear()
            historical_log_sheet.append_row(["Time-Stamp", "Symbol", "Support Price State", "Resistance Price State", "Scenario Status (K3)"])
            rows_to_flush = []
            for item in time_travel_db.get(active_symbol_name, []):
                rows_to_flush.append([item["timestamp"], item["symbol"], item["support"], item["resistance"], item["scenario"]])
            if rows_to_flush: historical_log_sheet.append_rows(rows_to_flush)
            last_flashed_symbol = active_symbol_name

    except Exception as err: print(f"❌ Dashboard Scenario Core Update Engine Error: {err}")

# ====================================================================
# 🗜️ PARALLEL DISPLAY-DRIVEN BATCH NETWORK CONTROLLER (RATE SAFE)
# ====================================================================
def fetch_and_update_option_chain(smart_api):
    global morning_oi_dict, baseline_date, options_cache, eq_cache, persistent_quotes_cache, multi_symbol_runtime_states
    try:
        now_dt = datetime.now()
        current_date_str = now_dt.strftime('%Y-%m-%d')

        if baseline_date != current_date_str: morning_oi_dict = {}; baseline_date = current_date_str
        if not options_cache: initialize_scrip_master(smart_api); return

        raw_selected_symbol = "NIFTY"
        try:
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

                spot_token, spot_exchange = eq_cache.get(clean_active_symbol, ("", "NSE"))
                spot_price = 0.0
                if spot_token:
                    try:
                        spot_quote = smart_api.getMarketData("FULL", {spot_exchange: [spot_token]})
                        if spot_quote and spot_quote.get('status') and spot_quote.get('data'):
                            spot_price = float(spot_quote['data']['fetched'][0].get('ltp', 0))
                    except Exception: pass

                if spot_price <= 0:
                    all_strikes_list = sorted(current_expiry_options['strike'].unique())
                    spot_price = float(all_strikes_list[len(all_strikes_list) // 2]) if all_strikes_list else 100.0

                safe_update_cell(data_sheet, "K9", spot_price)
                safe_update_cell(data_sheet, "L9", str(current_expiry))
                safe_update_cell(data_sheet, "A7", lot_size)
                for idx, cell in enumerate(['A3', 'A4', 'A5', 'A6']): safe_update_cell(data_sheet, cell, active_expiries[idx])

                all_master_strikes = sorted(current_expiry_options['strike'].unique())
                closest_idx = min(range(len(all_master_strikes)), key=lambda i: abs(all_master_strikes[i] - spot_price))
                strikes = all_master_strikes[max(0, closest_idx - 20):min(len(all_master_strikes), closest_idx + 21)]

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

                if len(output_rows) > 1:
                    data_sheet.update(range_name=f"A10:U{10 + len(output_rows) - 1}", values=output_rows)
                    try: data_sheet.batch_clear([f"A{10 + len(output_rows)}:U100"])
                    except Exception: pass

        execute_python_scenario_tracker()

        def scan_background_indices_pool():
            try:
                for sym in ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]:
                    if sym == clean_active_symbol: continue
                    sym_options = options_cache.get(sym, pd.DataFrame())
                    if sym_options.empty: continue
                    valid_opts = sym_options[sym_options['parsed_expiry'] >= pd.Timestamp(now_dt.date())].copy()
                    if valid_opts.empty: continue
                    u_exp = sorted(valid_opts['parsed_expiry'].unique())
                    curr_exp_opts = valid_opts[valid_opts['parsed_expiry'] == u_exp[0]].copy()
                    
                    all_st = sorted(curr_exp_opts['strike'].unique())
                    m_idx = len(all_st) // 2
                    st_sub = all_st[max(0, m_idx-5):min(len(all_st), m_idx+6)]
                    
                    b_tokens = []
                    for s in st_sub:
                        c_sub = curr_exp_opts[(curr_exp_opts['strike'] == s) & (curr_exp_opts['symbol'].str.upper().str.endswith('CE'))]
                        p_sub = curr_exp_opts[(curr_exp_opts['strike'] == s) & (curr_exp_opts['symbol'].str.upper().str.endswith('PE'))]
                        if not c_sub.empty: b_tokens.append(str(c_sub['token'].values[0]))
                        if not p_sub.empty: b_tokens.append(str(p_sub['token'].values[0]))
                    
                    if b_tokens:
                        res = smart_api.getMarketData("FULL", {"NFO": b_tokens[:40]})
                        if res and res.get('status') and res.get('data'):
                            f_quotes = res['data']['fetched']
                            q_map = {}
                            for q in f_quotes:
                                q_map[str(q.get('symbolToken', q.get('token', ''))).strip()] = {'oi': float(q.get('opnInterest', 0)), 'volume': float(q.get('tradeVolume', 0))}
                            
                            ce_v, ce_o, pe_v, pe_o = [], [], [], []
                            for idx, s in enumerate(st_sub):
                                c_sub = curr_exp_opts[(curr_exp_opts['strike'] == s) & (curr_exp_opts['symbol'].str.upper().str.endswith('CE'))]
                                p_sub = curr_exp_opts[(curr_exp_opts['strike'] == s) & (curr_exp_opts['symbol'].str.upper().str.endswith('PE'))]
                                ct = str(c_sub['token'].values[0]) if not c_sub.empty else ""
                                pt = str(p_sub['token'].values[0]) if not p_sub.empty else ""
                                cq = q_map.get(ct, {'oi': 0, 'volume': 0}); pq = q_map.get(pt, {'oi': 0, 'volume': 0})
                                ce_o.append({'strike': s, 'val': cq['oi'], 'row': 8+idx})
                                ce_v.append({'strike': s, 'val': cq['volume'], 'row': 8+idx})
                                pe_v.append({'strike': s, 'val': pq['volume'], 'row': 8+idx})
                                pe_o.append({'strike': s, 'val': pq['oi'], 'row': 8+idx})
                            process_silent_background_scenarios_rebuilt(sym, ce_v, ce_o, pe_v, pe_o)
                    time.sleep(0.3)
            except Exception: pass
        threading.Thread(target=scan_background_indices_pool, daemon=True).start()

    except Exception as ex: print(f"❌ Exception in fetch loop: {str(ex)}")

def process_silent_background_scenarios_rebuilt(symbol_name, ce_v, ce_o, pe_v, pe_o):
    global time_travel_db, multi_symbol_runtime_states
    try:
        now_dt = datetime.now()
        current_time_clock = now_dt.strftime("%H:%M:%S")
        current_time_str = now_dt.strftime("%I:%M %p").lower()
        if symbol_name not in multi_symbol_runtime_states:
            multi_symbol_runtime_states[symbol_name] = {
                "D5_TEXT": "(-----)", "H5_TEXT": "(-----)", "U5_TEXT": "(-----)", "Q5_TEXT": "(-----)", "K3_TEXT": f"{symbol_name} is Neutral"
            }
        v = multi_symbol_runtime_states[symbol_name]
        def extract_top(arr):
            if not arr: return {'highest': {'strike': 0, 'val': 0, 'row': 0}}
            sorted_arr = sorted(arr, key=lambda x: x['val'], reverse=True)
            return {'highest': sorted_arr[0]}
        cv_top, co_top, pv_top, po_top = extract_top(ce_v), extract_top(ce_o), extract_top(pe_v), extract_top(pe_o)
        k3 = f"{symbol_name} is Neutral"
        v["D5_TEXT"] = f"({current_time_str} - Eor OI - {int(co_top['highest']['strike'])})\n(stable strong)"
        v["H5_TEXT"] = f"({current_time_str} - Eor VOL - {int(cv_top['highest']['strike'])})\n(stable strong)"
        v["U5_TEXT"] = f"({current_time_str} - Eos OI - {int(po_top['highest']['strike'])})\n(stable strong)"
        v["Q5_TEXT"] = f"({current_time_str} - Eos VOL - {int(pv_top['highest']['strike'])})\n(stable strong)"
        v["K3_TEXT"] = k3
        if symbol_name not in time_travel_db: time_travel_db[symbol_name] = []
        symbol_history = time_travel_db[symbol_name]
        if len(symbol_history) == 0 or symbol_history[-1]["scenario"] != k3:
            time_travel_db[symbol_name].append({
                "timestamp": current_time_clock, "symbol": symbol_name,
                "support": "CE: STRONG (VOL)", "resistance": "PE: STRONG (VOL)", "scenario": k3
            })
    except Exception: pass

# ====================================================================
# 📥 DOWNLOAD OpenAPI INSTRUMENTS DICTIONARIES
# ====================================================================
def initialize_scrip_master(smart_api):
    global full_scrip_df, eq_cache, options_cache
    try:
        print("📥 Downloading Angel One Scrip Master JSON...")
        url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
        resp = requests.get(url, timeout=25)
        if resp.status_code == 200:
            scrip_data = resp.json(); full_scrip_df = pd.DataFrame(scrip_data)
            if 'symboltoken' in full_scrip_df.columns and 'token' not in full_scrip_df.columns: full_scrip_df['token'] = full_scrip_df['symboltoken']
            if 'tradingsymbol' in full_scrip_df.columns and 'symbol' not in full_scrip_df.columns: full_scrip_df['symbol'] = full_scrip_df['tradingsymbol']
            if 'strike' in full_scrip_df.columns: full_scrip_df['strike'] = pd.to_numeric(full_scrip_df['strike'], errors='coerce').fillna(0) / 100.0
            nse_eq = full_scrip_df[(full_scrip_df['exch_seg'] == 'NSE') & (~full_scrip_df['instrumenttype'].str.contains('OPT|FUT', na=False))]
            for _, row in nse_eq.iterrows():
                sym, name, tok = str(row['symbol']).strip().upper(), str(row['name']).strip().upper(), str(row['token']).strip()
                eq_cache[sym] = (tok, 'NSE'); eq_cache[name] = (tok, 'NSE')
                if sym.endswith("-EQ"): eq_cache[sym.replace("-EQ", "")] = (tok, 'NSE')
            fno_opts = full_scrip_df[(full_scrip_df['strike'] > 0) & (full_scrip_df['exch_seg'] == 'NFO')].copy()
            fno_opts['strike'] = pd.to_numeric(fno_opts['strike'], errors='coerce')
            fno_opts['parsed_expiry'] = pd.to_datetime(fno_opts['expiry'], format='%d%b%Y', errors='coerce').fillna(pd.to_datetime(fno_opts['expiry'], errors='coerce'))
            for name, group in fno_opts.groupby(fno_opts['name'].str.upper()): options_cache[name.strip()] = group
            print(f"✅ Scrip Master Cached Successfully!")
    except Exception as e: print(f"⚠ Scrip Master Error: {e}")

full_scrip_df = pd.DataFrame(); eq_cache = {}; options_cache = {}; morning_oi_dict = {}; baseline_date = None

def run_bot_lock_loop():
    print("🚀 Starting Advanced F&O Symbols Parallel Live Engine...")
    try: smart_api = SmartConnect(api_key=API_KEY)
    except Exception as e: print(f"❌ Init Error: {e}"); return
    while True:
        try:
            totp = pyotp.TOTP(TOTP_SECRET.replace(" ", "")).now() if TOTP_SECRET else "123456"
            if smart_api.generateSession(CLIENT_ID, PIN, totp).get('status'): initialize_scrip_master(smart_api); break
            time.sleep(5)
        except Exception: time.sleep(5)
    while True:
        try:
            now_check = datetime.now()
            if now_check.strftime('%H:%M') == "09:00":
                totp = pyotp.TOTP(TOTP_SECRET.replace(" ", "")).now() if TOTP_SECRET else "123456"
                smart_api.generateSession(CLIENT_ID, PIN, totp); time.sleep(60)
            fetch_and_update_option_chain(smart_api)
        except Exception as _e:
            err_msg = str(_e).lower()
            if any(x in err_msg for x in ["session", "token", "unauthorized", "invalid"]):
                while True:
                    try:
                        totp = pyotp.TOTP(TOTP_SECRET.replace(" ", "")).now() if TOTP_SECRET else "123456"
                        if smart_api.generateSession(CLIENT_ID, PIN, totp).get('status'): break
                    except Exception: pass
                    time.sleep(10)
        time.sleep(4)

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    run_bot_lock_loop()
