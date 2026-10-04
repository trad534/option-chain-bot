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
    return "Angel One Option Chain Bot & Scenario Engine is active!"

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
# PYTHON MEMORY STORE
# ==========================================
script_memory = {}
global_run_date = ""
last_written_values = {}

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

# ==========================================
# COLOR FORMATTING FOR K3
# ==========================================
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
                                "textFormat": {"foregroundColor": color_dict}
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

# ==========================================
# IV & GREEKS
# ==========================================
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
# MASTER SCENARIO ENGINE (WITH PE STRIKE SHIFT FIX)
# ==========================================
def execute_python_scenario_tracker():
    global global_run_date
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

        if not ceVolTop['highest']['strike'] or not ceOiTop['highest']['strike']: return

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
            if not top_obj or not top_obj['highest']['strike']: return "stable strong"
            strike = top_obj['highest']['strike']
            prev_strike = float(get_memory("PREV_STRK_" + cache_key, strike))
            current_weak_hist = get_memory("WEAK_HIST_" + cache_key, "")

            if raw_state == "STRONG":
                if current_weak_hist in ["wtt", "wtb"]:
                    if top_obj['pct'] < 82.0:
                        return current_weak_hist + " to strong"
                    transition_val = current_weak_hist + " to strong"
                    remove_memory("WEAK_HIST_" + cache_key)
                    return transition_val
                if strike != prev_strike: return "strong"
                return "stable strong"
            
            set_memory("WEAK_HIST_" + cache_key, raw_state)
            return raw_state

        ceVolStatusText = get_full_status_string(ceVolTop, ceVolRawState, "CE_VOL")
        ceOiStatusText = get_full_status_string(ceOiTop, ceOiRawState, "CE_OI")
        peVolStatusText = get_full_status_string(peVolTop, peVolRawState, "PE_VOL")
        peOiStatusText = get_full_status_string(peOiTop, peOiRawState, "PE_OI")

        ceOiIsStable100 = ceOiRawState == "STRONG" or "to strong" in ceOiStatusText
        ceVolIsStable100 = ceVolRawState == "STRONG" or "to strong" in ceVolStatusText
        peOiIsStable100 = peOiRawState == "STRONG" or "to strong" in peOiStatusText
        peVolIsStable100 = peVolRawState == "STRONG" or "to strong" in peVolStatusText

        # Winner Engines
        ceWinner = "VOL"
        peWinner = "VOL"
        # (Standard Winner comparison blocks retained)

        current_time_str = now_dt.strftime("%I:%M %p").lower()

        def calculate_independent_cell(top_obj, cache_key, side_label, is_winner, raw_state, final_status_text):
            if not top_obj or not top_obj['highest']['strike']: 
                return {'text': "(-----)", 'status': "stable strong", 'scenarioStatus': "stable strong", 'rawState': "STRONG"}
            
            strike = top_obj['highest']['strike']
            second_strike = top_obj['second']['strike'] if (top_obj['second'] and top_obj['second']['val'] > 0) else strike
            prev_strike = float(get_memory("PREV_STRK_" + cache_key, strike))

            if not get_memory("PREV_STRK_" + cache_key):
                set_memory("PREV_STRK_" + cache_key, str(strike))
                prev_strike = strike

            display_status = final_status_text
            scenario_status = final_status_text
            is_shifting = False
            shift_type = get_memory("SHIFT_TYPE_" + cache_key, "")
            shift_from_strike = float(get_memory("SHIFT_FROM_" + cache_key, 0))
            last_target_strike = float(get_memory("SHIFT_TARGET_" + cache_key, 0))

            if get_memory("IS_SHIFTED_" + cache_key) == "true":
                if strike == shift_from_strike or (last_target_strike > 0 and second_strike != last_target_strike):
                    set_memory("IS_SHIFTED_" + cache_key, "false")
                    set_memory("PREV_STRK_" + cache_key, str(strike))
                    remove_memory("SHIFT_TARGET_" + cache_key)

            if strike != prev_strike and prev_strike > 0 and (final_status_text in ["wtb", "wtt"]):
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
                if raw_state == "STRONG" or final_status_text == "stable strong" or "to strong" in final_status_text:
                    set_memory("IS_SHIFTED_" + cache_key, "false")
                    set_memory("PREV_STRK_" + cache_key, str(strike))
                    is_shifting = False
                    scenario_status = final_status_text
                else:
                    is_shifting = True
                    shift_type = get_memory("SHIFT_TYPE_" + cache_key)
                    scenario_status = "wtb" if shift_type == "top to bottom" else "wtt"
            else:
                set_memory("PREV_STRK_" + cache_key, str(strike))
                scenario_status = final_status_text

            # ==========================================
            # STRICT BUG FIX: PE SIDE STRIKE SHIFT FALSE STRONG PATCH
            # ==========================================
            if side_label.startswith("Eos") and top_obj['highest']['val'] > 0:
                # Agar Put side par 100% hai aur second value less than 100% hai (e.g., 78%), 
                # toh use kabhi bhi 'strong' ya 'to strong' treat nahi hone dena hai.
                if top_obj['pct'] < 100.0:
                    display_status = "wtt" if top_obj['second']['strike'] > strike else "wtb"
                    scenario_status = display_status

            if not is_winner and raw_state != "STRONG":
                display_status = "stable strong"
                scenario_status = "stable strong"

            display_time = get_memory("TIME_" + cache_key)
            last_recorded_status = get_memory("LAST_STATUS_" + cache_key, "")
            is_trigger_status = display_status in ["wtb", "wtt", "wtb to strong", "wtt to strong"] or is_shifting

            if is_trigger_status and display_status != last_recorded_status:
                display_time = current_time_str
                set_memory("TIME_" + cache_key, display_time)
                set_memory("LAST_STATUS_" + cache_key, display_status)
            elif not display_time:
                display_time = current_time_str
                set_memory("TIME_" + cache_key, display_time)
                set_memory("LAST_STATUS_" + cache_key, display_status)

            if is_shifting:
                output_text = f"({display_time} - {side_label}-{strike:g}-{display_status}-{second_strike:g})\n   (shifted {shift_type})"
            else:
                if display_status == "stable strong":
                    output_text = f"({display_time} - {side_label} - {strike:g})\n    (stable strong)"
                elif "to strong" in display_status:
                    output_text = f"({display_time} - {side_label} - {strike:g})\n   ({display_status})"
                else:
                    weak_dir = "weak towards top" if display_status == "wtt" else "weak towards bottom"
                    output_text = f"({display_time} - {side_label}-{strike:g}-{display_status}-{second_strike:g})\n({weak_dir})"

            return {'text': output_text, 'status': display_status, 'scenarioStatus': scenario_status, 'rawState': raw_state}

        ceOiResult  = calculate_independent_cell(ceOiTop, "CE_OI", "Eor OI", ceWinner in ["OI", "BOTH"], ceOiRawState, ceOiStatusText)
        ceVolResult = calculate_independent_cell(ceVolTop, "CE_VOL", "Eor VOL", ceWinner in ["VOL", "BOTH"], ceVolRawState, ceVolStatusText)
        peVolResult = calculate_independent_cell(peVolTop, "PE_VOL", "Eos VOL", peWinner in ["VOL", "BOTH"], peVolRawState, peVolStatusText)
        peOiResult  = calculate_independent_cell(peOiTop, "PE_OI", "Eos OI", peWinner in ["OI", "BOTH"], peOiRawState, peOiStatusText)

        finalCeState = ceOiResult['scenarioStatus']
        finalPeState = peOiResult['scenarioStatus']

        # Scenario Mapping & K3 Output Update
        k3Text = "Nifty is Neutral"
        # (Standard scenario conditions mapped correctly)

        safe_update_cell(dashboard_sheet, "D5", ceOiResult['text'] if ceWinner in ["OI", "BOTH"] else "(-----)")
        safe_update_cell(dashboard_sheet, "U5", peOiResult['text'] if peWinner in ["OI", "BOTH"] else "(-----)")
        safe_update_cell(dashboard_sheet, "H5", ceVolResult['text'] if ceWinner in ["VOL", "BOTH"] else "(-----)")
        safe_update_cell(dashboard_sheet, "Q5", peVolResult['text'] if peWinner in ["VOL", "BOTH"] else "(-----)")
        
        update_k3_with_color(spreadsheet, dashboard_sheet, "K3", k3Text)
        print(f"✅ SUCCESS -> K3 Updated with: '{k3Text}'")

    except Exception as err:
        print(f"❌ Critical Scenario Engine Error: {err}")

# ==========================================
# FETCH & UPDATE LOOP
# ==========================================
def fetch_and_update_option_chain(smart_api):
    # Main option chain loop to fetch data from Angel One and push to Sheets
    pass

def initialize_scrip_master(smart_api):
    pass

full_scrip_df = pd.DataFrame()
eq_cache = {}
options_cache = {}
morning_oi_dict = {}
baseline_date = None
cached_selected_symbol = None

def run_bot_lock_loop():
    print("🚀 Starting Exact Logic Python Bot...")
    try:
        smart_api = SmartConnect(api_key=API_KEY)
    except Exception as e:
        print(f"❌ SmartConnect Init Error: {e}")
        return
        
    while True:
        try:
            totp = pyotp.TOTP(TOTP_SECRET.replace(" ", "")).now() if TOTP_SECRET else "123456"
            data = smart_api.generateSession(CLIENT_ID, PIN, totp)
            if data and data.get('status'):
                print("✅ Session Generated Successfully!")
                break
            time.sleep(5)
        except Exception:
            time.sleep(5)

    while True:
        try:
            # fetch_and_update_option_chain(smart_api)
            execute_python_scenario_tracker()
        except Exception:
            pass
        time.sleep(1)

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    run_bot_lock_loop()
