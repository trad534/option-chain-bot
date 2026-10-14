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
# 🛡️ RENDER SYSTEM KEEP-ALIVE NET SHIELD
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

# Force flush standard system print buffers for live tracking in Render logs terminal
def print(*args, **kwargs):
    kwargs['flush'] = True
    builtins.print(*args, **kwargs)

# ====================================================================
# 🌐 FLASK WEB MODULE FOR ENGINE PORT BINDING
# ====================================================================
app = Flask(__name__)

@app.route('/')
def home():
    return "Angel One Option Chain Bot & Multi-Symbol Full-Market Engine is active!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
# ====================================================================
# 🛠️ APP PARAMETERS CONFIGURATION & GOOGLE SHEETS BINDINGS
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
    
    # Check or auto-build dedicated historical logging tab safely
    try:
        historical_log_sheet = spreadsheet.worksheet(HISTORICAL_SHEET_NAME)
    except Exception:
        historical_log_sheet = spreadsheet.add_worksheet(title=HISTORICAL_SHEET_NAME, rows="5000", cols="10")
        historical_log_sheet.append_row(["Time-Stamp", "Symbol", "Support Price State", "Resistance Price State", "Scenario Status (K3)"])
        
    print("✅ SUCCESS: All Google Sheet Tabs Connected Successfully!")
except Exception as e:
    print(f"❌ Google Sheet Connection Error: {e}")
    exit()

# ====================================================================
# 🧠 DYNAMIC MULTI-SYMBOL REAL-TIME VAULT STORAGE (RAM)
# ====================================================================
script_memory = {}
global_run_date = ""
last_written_values = {}
persistent_quotes_cache = {}

# Master time-travel parameters storing exact structural frame arrays for all assets independently
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
# 🧮 OPTIONS BLACK-SCHOLES COMPUTATION PARSER
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
# 🎛️ FULL MARKET 265 SYMBOLS CALCULATION BLOCK (RAM MATRIX LOG)
# ====================================================================
def calculate_standalone_market_metrics(symbol_name, strikes, ceVolArray, ceOiArray, peVolArray, peOiArray):
    """
    Computes exact mathematical 75% thresholds and maps parallel winner rules
    independently for each symbol without affecting standard front dashboard.
    """
    global time_travel_db, multi_symbol_runtime_states, script_memory
    try:
        now_dt = datetime.now()
        current_time_str = now_dt.strftime("%I:%M %p").lower()
        current_time_clock = now_dt.strftime("%H:%M:%S")

        if symbol_name not in multi_symbol_runtime_states:
            multi_symbol_runtime_states[symbol_name] = {
                "PREV_STRK_CE_OI": None, "PREV_STRK_CE_VOL": None, "PREV_STRK_PE_OI": None, "PREV_STRK_PE_VOL": None,
                "IS_SHIFTED_CE_OI": "false", "IS_SHIFTED_CE_VOL": "false", "IS_SHIFTED_PE_OI": "false", "IS_SHIFTED_PE_VOL": "false",
                "SHIFT_TYPE_CE_OI": "", "SHIFT_TYPE_CE_VOL": "", "SHIFT_TYPE_PE_OI": "", "SHIFT_TYPE_PE_VOL": "",
                "SHIFT_TARGET_CE_OI": 0, "SHIFT_TARGET_CE_VOL": 0, "SHIFT_TARGET_PE_OI": 0, "SHIFT_TARGET_PE_VOL": 0,
                "SHIFT_FROM_CE_OI": 0, "SHIFT_FROM_CE_VOL": 0, "SHIFT_FROM_PE_OI": 0, "SHIFT_FROM_PE_VOL": 0,
                "TIME_CE_OI": "", "TIME_CE_VOL": "", "TIME_PE_OI": "", "TIME_PE_VOL": "",
                "LAST_STATUS_CE_OI": "", "LAST_STATUS_CE_VOL": "", "LAST_STATUS_PE_OI": "", "LAST_STATUS_PE_VOL": "",
                "WEAK_HIST_CE_OI": "", "WEAK_HIST_CE_VOL": "", "WEAK_HIST_PE_OI": "", "WEAK_HIST_PE_VOL": ""
            }

        state = multi_symbol_runtime_states[symbol_name]

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

        if ceVolTop['highest']['val'] == 0 or peVolTop['highest']['val'] == 0: return

        ceVolRawState = "STRONG" if ceVolTop['pct'] < 75.0 else ("wtt" if ceVolTop['second']['strike'] > ceVolTop['highest']['strike'] else "wtb")
        ceOiRawState = "STRONG" if ceOiTop['pct'] < 75.0 else ("wtt" if ceOiTop['second']['strike'] > ceOiTop['highest']['strike'] else "wtb")
        peVolRawState = "STRONG" if peVolTop['pct'] < 75.0 else ("wtb" if peVolTop['second']['strike'] < peVolTop['highest']['strike'] else "wtt")
        peOiRawState = "STRONG" if peOiTop['pct'] < 75.0 else ("wtb" if peOiTop['second']['strike'] < peOiTop['highest']['strike'] else "wtt")
        def check_status_text(top_obj, raw_state, weak_hist_key):
            if raw_state == "STRONG":
                if state[weak_hist_key] == "wtt": return "wtt to strong"
                if state[weak_hist_key] == "wtb": return "wtb to strong"
                return "stable strong"
            return raw_state

        ceVolStatusText = check_status_text(ceVolTop, ceVolRawState, "WEAK_HIST_CE_VOL")
        ceOiStatusText = check_status_text(ceOiTop, ceOiRawState, "WEAK_HIST_CE_OI")
        peVolStatusText = check_status_text(peVolTop, peVolRawState, "WEAK_HIST_PE_VOL")
        peOiStatusText = check_status_text(peOiTop, peOiRawState, "WEAK_HIST_PE_OI")

        ceOiIsStable100 = ceOiRawState == "STRONG" or "to strong" in ceOiStatusText
        ceVolIsStable100 = ceVolRawState == "STRONG" or "to strong" in ceVolStatusText
        peOiIsStable100 = peOiRawState == "STRONG" or "to strong" in peOiStatusText
        peVolIsStable100 = peVolRawState == "STRONG" or "to strong" in peVolStatusText

        # CE PRIORITY MATRIX LOOKUPS
        ceWinner = "VOL"
        if ceVolTop['highest']['strike'] == ceOiTop['highest']['strike'] and ceOiRawState == "wtb" and ceVolRawState == "wtb": ceWinner = "BOTH"
        elif ceVolTop['highest']['strike'] == ceOiTop['highest']['strike'] and not ceOiIsStable100 and not ceVolIsStable100 and (ceOiTop['second']['strike'] > ceOiTop['highest']['strike']) and (ceVolTop['second']['strike'] > ceOiTop['highest']['strike']): ceWinner = "BOTH"
        elif ceVolTop['highest']['strike'] == ceOiTop['highest']['strike'] and ceOiIsStable100 and ceVolIsStable100: ceWinner = "BOTH"
        elif ceVolTop['highest']['strike'] == ceOiTop['highest']['strike'] and ceOiIsStable100 and not ceVolIsStable100 and (ceVolTop['second'] and ceVolTop['second']['strike'] > ceVolTop['highest']['strike']): ceWinner = "OI"
        elif ceVolTop['highest']['strike'] == ceOiTop['highest']['strike'] and ceVolIsStable100 and not ceOiIsStable100 and (ceOiTop['second'] and ceOiTop['second']['strike'] > ceOiTop['highest']['strike']): ceWinner = "VOL"
        else: ceWinner = "VOL" if ceVolTop['highest']['row'] <= ceOiTop['highest']['row'] else "OI"

        # PE PRIORITY MATRIX LOOKUPS
        peWinner = "VOL"
        if peVolTop['highest']['strike'] == peOiTop['highest']['strike'] and peOiRawState == "wtt" and peVolRawState == "wtt": peWinner = "BOTH"
        elif peVolTop['highest']['strike'] == peOiTop['highest']['strike'] and not peOiIsStable100 and not peVolIsStable100 and (peOiTop['second']['strike'] < peOiTop['highest']['strike']) and (peVolTop['second']['strike'] < peOiTop['highest']['strike']): peWinner = "BOTH"
        elif peVolTop['highest']['strike'] == peOiTop['highest']['strike'] and peOiIsStable100 and peVolIsStable100: peWinner = "BOTH"
        else: peWinner = "VOL" if peVolTop['highest']['row'] >= peOiTop['highest']['row'] else "OI"

        if ceOiRawState in ["wtt", "wtb"]: state["WEAK_HIST_CE_OI"] = ceOiRawState
        if ceVolRawState in ["wtt", "wtb"]: state["WEAK_HIST_CE_VOL"] = ceVolRawState
        if peOiRawState in ["wtt", "wtb"]: state["WEAK_HIST_PE_OI"] = peOiRawState
        if peVolRawState in ["wtt", "wtb"]: state["WEAK_HIST_PE_VOL"] = peVolRawState

        def parse_cell_state(top_obj, cache_key, side_label, is_winner, raw_state, final_status_text):
            strike = top_obj['highest']['strike']
            second_strike = top_obj.get('second', {'strike': strike})['strike']
            
            if state[f"PREV_STRK_{cache_key}"] is None: state[f"PREV_STRK_{cache_key}"] = strike
            prev_strike = state[f"PREV_STRK_{cache_key}"]

            display_status = final_status_text
            scenario_status = final_status_text
            is_shifting = False

            if state[f"IS_SHIFTED_{cache_key}"] == "true":
                if strike == state[f"SHIFT_FROM_{cache_key}"] or (state[f"SHIFT_TARGET_{cache_key}"] > 0 and second_strike != state[f"SHIFT_TARGET_{cache_key}"]):
                    state[f"IS_SHIFTED_{cache_key}"] = "false"
                    state[f"PREV_STRK_{cache_key}"] = strike

            if strike != prev_strike and prev_strike > 0 and final_status_text in ["wtb", "wtt"]:
                state[f"SHIFT_TYPE_{cache_key}"] = "top to bottom" if strike < prev_strike else "bottom to top"
                state[f"IS_SHIFTED_{cache_key}"] = "true"
                state[f"SHIFT_FROM_{cache_key}"] = prev_strike
                state[f"SHIFT_TARGET_{cache_key}"] = second_strike

            if state[f"IS_SHIFTED_{cache_key}"] == "true":
                if raw_state == "STRONG" or final_status_text == "stable strong" or "to strong" in final_status_text:
                    state[f"IS_SHIFTED_{cache_key}"] = "false"
                    state[f"PREV_STRK_{cache_key}"] = strike
                    scenario_status = final_status_text
                else:
                    is_shifting = True
                    scenario_status = "wtb" if state[f"SHIFT_TYPE_{cache_key}"] == "top to bottom" else "wtt"
            else:
                state[f"PREV_STRK_{cache_key}"] = strike
                scenario_status = final_status_text

            if not is_winner and raw_state != "STRONG": display_status = "stable strong"; scenario_status = "stable strong"

            if state[f"TIME_{cache_key}"] and display_status != state[f"LAST_STATUS_{cache_key}"]:
                state[f"TIME_{cache_key}"] = current_time_str
                state[f"LAST_STATUS_{cache_key}"] = display_status
            elif not state[f"TIME_{cache_key}"]:
                state[f"TIME_{cache_key}"] = current_time_str
                state[f"LAST_STATUS_{cache_key}"] = display_status

            s_val = int(strike) if strike.is_integer() else strike
            sec_val = int(second_strike) if second_strike.is_integer() else second_strike

            if is_shifting: output_text = f"({state[f'TIME_{cache_key}']} - {side_label}-{s_val}-{display_status}-{sec_val})\n   (shifted {state[f'SHIFT_TYPE_{cache_key}']})"
            else:
                if display_status == "stable strong": output_text = f"({state[f'TIME_{cache_key}']} - {side_label} - {s_val})\n    (stable strong)"
                elif "to strong" in display_status: output_text = f"({state[f'TIME_{cache_key}']} - {side_label} - {s_val})\n   ({display_status})"
                else: output_text = f"({state[f'TIME_{cache_key}']} - {side_label}-{s_val}-{display_status}-{sec_val})\n({'weak towards top' if display_status == 'wtt' else 'weak towards bottom'})"
            return {'text': output_text, 'scenarioStatus': scenario_status}
        ceOiResult  = parse_cell_state(ceOiTop, "CE_OI", "Eor OI", ceWinner in ["OI", "BOTH"], ceOiRawState, ceOiStatusText)
        ceVolResult = parse_cell_state(ceVolTop, "CE_VOL", "Eor VOL", ceWinner in ["VOL", "BOTH"], ceVolRawState, ceVolStatusText)
        peVolResult = parse_cell_state(peVolTop, "PE_VOL", "Eos VOL", peWinner in ["VOL", "BOTH"], peVolRawState, peVolStatusText)
        peOiResult  = parse_cell_state(peOiTop, "PE_OI", "Eos OI", peWinner in ["OI", "BOTH"], peOiRawState, peOiStatusText)

        finalCeState = ceOiResult['scenarioStatus'] if ceWinner == "OI" else ceVolResult['scenarioStatus']
        finalPeState = peOiResult['scenarioStatus'] if peWinner == "OI" else peVolResult['scenarioStatus']
        if ceWinner == "BOTH": finalCeState = ceVolResult['scenarioStatus'] if "to strong" in ceVolResult['scenarioStatus'] else ceOiResult['scenarioStatus']
        if peWinner == "BOTH": finalPeState = peVolResult['scenarioStatus'] if "to strong" in peVolResult['scenarioStatus'] else peOiResult['scenarioStatus']

        k3Text = f"{symbol_name} is Neutral"
        if finalCeState == "wtt" and finalPeState == "wtb": k3Text = f"{symbol_name} is 8th scenario both side Risky"
        elif finalCeState == "wtb" and finalPeState == "wtt": k3Text = f"{symbol_name} is 9th scenario both side Risky"
        elif finalCeState == "stable strong" and finalPeState == "stable strong": k3Text = f"{symbol_name} is Neutral"
        elif finalCeState == "wtb" and "strong" in finalPeState: k3Text = f"{symbol_name} is slightly Bearish"
        elif "strong" in finalCeState and finalPeState == "wtb": k3Text = f"{symbol_name} is slightly Bearish"
        elif finalCeState == "wtt" and "strong" in finalPeState: k3Text = f"{symbol_name} is slightly bullish"
        elif "strong" in finalCeState and finalPeState == "wtt": k3Text = f"{symbol_name} is slightly bullish"
        elif finalCeState == "wtt" and finalPeState == "wtt": k3Text = f"{symbol_name} is Bullrun"
        elif finalCeState == "wtb" and finalPeState == "wtb": k3Text = f"{symbol_name} is bloodbath"

        # Safe lock arrays metrics inside multi-symbol vault state
        ce_s_val = int(ceVolTop['highest']['strike'])
        pe_s_val = int(peVolTop['highest']['strike'])

        state["D5_TEXT"] = f"({current_time_str} - Eor OI - {ce_s_val})\n({checkCe})"
        state["H5_TEXT"] = f"({current_time_str} - Eor VOL - {ce_s_val})\n({checkCe})"
        state["U5_TEXT"] = f"({current_time_str} - Eos OI - {pe_s_val})\n({checkPe})"
        state["Q5_TEXT"] = f"({current_time_str} - Eos VOL - {pe_s_val})\n({checkPe})"
        state["K3_TEXT"] = k3Text

        if symbol_name not in time_travel_db: time_travel_db[symbol_name] = []
        symbol_history = time_travel_db[symbol_name]

        if len(symbol_history) == 0 or symbol_history[-1]["scenario"] != k3Text:
            time_travel_db[symbol_name].append({
                "timestamp": current_time_clock, "symbol": symbol_name,
                "support": f"CE: {checkCe.upper()} ({ceWinner})",
                "resistance": f"PE: {checkPe.upper()} ({peWinner})",
                "scenario": k3Text
            })
            print(f"📊 [TIMELINE MATCHED] Dynamic line appended for {symbol_name} at {current_time_clock}")
    except Exception as e: 
        print(f"Error in silent structural tracker calculation loop: {e}")
# ====================================================================
# 🖥️ FRONT SCREEN DISPLAY INTERFACE UPDATE TRACKER
# ====================================================================
def execute_python_scenario_tracker():
    global last_flashed_symbol, multi_symbol_runtime_states, time_travel_db
    try:
        active_symbol_name = "Nifty"
        try:
            j4_val = str(dashboard_sheet.cell(4, 10).value).strip()
            if j4_val and j4_val not in ["NONE", "NAN", "0", ""]:
                active_symbol_name = j4_val.replace("-EQ", "").replace(".NS", "").strip()
        except Exception: pass

        # Injecting the saved historical vault configuration directly onto active screen matrix
        if active_symbol_name in multi_symbol_runtime_states:
            saved_state = multi_symbol_runtime_states[active_symbol_name]
            
            safe_update_cell(dashboard_sheet, "D5", saved_state.get("D5_TEXT", "(-----)"))
            safe_update_cell(dashboard_sheet, "H5", saved_state.get("H5_TEXT", "(-----)"))
            safe_update_cell(dashboard_sheet, "U5", saved_state.get("U5_TEXT", "(-----)"))
            safe_update_cell(dashboard_sheet, "Q5", saved_state.get("Q5_TEXT", "(-----)"))
            
            update_k3_with_color(spreadsheet, dashboard_sheet, "K3", saved_state.get("K3_TEXT", f"{active_symbol_name} is Neutral"))
            print(f"✨ [DASHBOARD INJECTED] Successfully forced past status on {active_symbol_name} cells screen!")

        # ⚡ EVENT DRIVEN HISTORICAL TIMELINE FLASH OVERWRITE ENGINE
        if last_flashed_symbol != active_symbol_name:
            print(f"🔄 Active Symbol Changed from '{last_flashed_symbol}' to '{active_symbol_name}' -> Overwriting sheet logs...")
            historical_log_sheet.clear()
            historical_log_sheet.append_row(["Time-Stamp", "Symbol", "Support Price State", "Resistance Price State", "Scenario Status (K3)"])
            
            rows_to_flush = []
            for item in time_travel_db.get(active_symbol_name, []):
                rows_to_flush.append([item["timestamp"], item["symbol"], item["support"], item["resistance"], item["scenario"]])
            
            if rows_to_flush: 
                historical_log_sheet.append_rows(rows_to_flush)
                print(f"✨ [FLASH COMPLETE] Overwrote {len(rows_to_flush)} timelines array lines for '{active_symbol_name}'!")
            last_flashed_symbol = active_symbol_name

    except Exception as err: 
        print(f"❌ Dashboard Direct Write Update Engine Error: {err}")
# ====================================================================
# 🗜️ HIGH RECOVERY API BATCH NETWORK FLOW ENGINE
# ====================================================================
def fetch_and_update_option_chain(smart_api):
    global morning_oi_dict, baseline_date, options_cache, eq_cache, persistent_quotes_cache
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

        # Packaging full 265 assets F&O data
        symbols_to_scan = list(options_cache.keys())
        all_tokens_to_fetch = []
        token_data_meta_map = {}

        for sym in symbols_to_scan:
            sym_options = options_cache.get(sym, pd.DataFrame())
            if sym_options.empty: continue
            valid_opts = sym_options[sym_options['parsed_expiry'] >= pd.Timestamp(now_dt.date())].copy()
            if valid_opts.empty: continue

            unique_expiries_dt = sorted(valid_opts['parsed_expiry'].unique())
            current_expiry_options = valid_opts[valid_opts['parsed_expiry'] == unique_expiries_dt[0]].copy()

            all_strikes = sorted(current_expiry_options['strike'].unique())
            mid_idx = len(all_strikes) // 2
            strikes_subset = all_strikes[max(0, mid_idx-5):min(len(all_strikes), mid_idx+6)]

            filtered_opts = current_expiry_options[current_expiry_options['strike'].isin(strikes_subset)]
            for _, row in filtered_opts.iterrows():
                tok = str(row['token']).strip()
                all_tokens_to_fetch.append(tok)
                token_data_meta_map[tok] = {'symbol': sym, 'options_df': current_expiry_options, 'strikes': strikes_subset}

        # Multi-Paginator Packet Call
        all_fetched_quotes = []
        chunks = [all_tokens_to_fetch[i:i + 40] for i in range(0, len(all_tokens_to_fetch), 40)]

        for batch in chunks[:10]:
            try:
                res = smart_api.getMarketData("FULL", {"NFO": batch})
                if res and res.get('status') and res.get('data'): all_fetched_quotes.extend(res['data'].get('fetched', []))
            except Exception: pass

        global_quotes_dict = {}
        for item in all_fetched_quotes:
            tok = str(item.get('symbolToken', item.get('token', ''))).strip()
            volume = float(item.get('tradeVolume', 0)); oi = float(item.get('opnInterest', 0))
            global_quotes_dict[tok] = {'volume': volume, 'oi': oi}

        # Silent parallel background extraction loop run
        for tok, meta in token_data_meta_map.items():
            process_silent_background_scenarios_rebuilt(meta['symbol'], global_quotes_dict, meta['options_df'], meta['strikes'])

        # Traditional Layout calculation block for dashboard active asset
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
                
                # FIXED PANDAS BLOCK CRASH INDEXING SLICES:
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
                    if datetime.now().time() >= datetime.strptime("09:15:00", "%H:%M:%S").time() and tok not in morning_oi_dict and q_data['oi'] > 0: morning_oi_dict[tok] = q_data['oi']
                    base_oi = morning_oi_dict.get(tok, q_data['oi'])
                    dash_quotes_dict[tok] = {'ltp': q_data['ltp'], 'volume': q_data['volume'], 'oi': q_data['oi'], 'oi_chg': q_data['oi'] - base_oi, 'net_change': q_data['net_change']}

                # FIXED TIME DELTA BLOCK STRINGS:
                exp_dt_parsed = pd.to_datetime(current_expiry_options['parsed_expiry'].iloc[0])
                t = max((exp_dt_parsed.replace(hour=15, minute=30) - datetime.now()).total_seconds() / (365.0 * 24 * 3600), 0.0001)

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

        # Synchronize dashboard engine completely
        execute_python_scenario_tracker()
    except Exception as ex: print(f"❌ Exception in fetch loop: {str(ex)}")

def process_silent_background_scenarios_rebuilt(symbol_name, quotes_dict, current_expiry_options, strikes):
    try:
        ceVolArray, ceOiArray, peVolArray, peOiArray = [], [], [], []
        for idx, st in enumerate(strikes):
            ce_sub = current_expiry_options[(current_expiry_options['strike'] == st) & (current_expiry_options['symbol'].astype(str).str.upper().str.endswith('CE'))]
            pe_sub = current_expiry_options[(current_expiry_options['strike'] == st) & (current_expiry_options['symbol'].astype(str).str.upper().str.endswith('PE'))]
            ce_tok = str(ce_sub['token'].values[0]).strip() if not ce_sub.empty else ""
            pe_tok = str(pe_sub['token'].values[0]).strip() if not pe_sub.empty else ""
            ce_q = quotes_dict.get(ce_tok, {'oi': 0, 'volume': 0}); pe_q = quotes_dict.get(pe_tok, {'oi': 0, 'volume': 0})
            ceOiArray.append({'strike': st, 'val': float(ce_q['oi']), 'row': 8+idx})
            ceVolArray.append({'strike': st, 'val': float(ce_q['volume']), 'row': 8+idx})
            peVolArray.append({'strike': st, 'val': float(pe_q['volume']), 'row': 8+idx})
            peOiArray.append({'strike': st, 'val': float(ce_q['oi']), 'row': 8+idx})
        calculate_standalone_market_metrics(symbol_name, strikes, ceVolArray, ceOiArray, peVolArray, peOiArray)
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

# ====================================================================
# ⚡ MAIN INFINITE CRASH-PROOF THREAD LOOP
# ====================================================================
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
