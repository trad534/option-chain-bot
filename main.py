import time
import os
import json
import requests
import pandas as pd
import numpy as np
import pyotp
import gspread
import gspread.utils
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
    return "Exact GAS Matched Option Chain Engine is active!"

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
# PYTHON MEMORY STORE (GAS Properties Emulation)
# ==========================================
script_memory = {}

def get_memory(key, default=""):
    return script_memory.get(key, default)

def set_memory(key, value):
    script_memory[key] = str(value)

def remove_memory(key):
    if key in script_memory:
        del script_memory[key]

def safe_update_cell(sheet, cell_name, new_value):
    try:
        sheet.update_acell(cell_name, str(new_value))
        time.sleep(0.3)
    except Exception as e:
        print(f"⚠ Cell Update Error on {cell_name}: {e}")

# ==========================================
# STRICT GAS MATCHED SCENARIO ENGINE
# ==========================================
def execute_strict_independent_matrix():
    try:
        now_dt = datetime.now()
        current_kolkata_time = now_dt.strftime("%H:%M:%S")
        today_str = now_dt.strftime("%Y-%m-%d")

        # Morning Reset at 09:14:5x
        if current_kolkata_time.startswith("09:14:5"):
            script_memory.clear()

        global_run_date = get_memory("GLOBAL_RUN_DATE_TRUE_FINAL_V9", "")
        force_clean = get_memory("FORCE_CLEAN_TRUE_FINAL_V9", "")
        if global_run_date != today_str or force_clean != "YES":
            script_memory.clear()
            set_memory("GLOBAL_RUN_DATE_TRUE_FINAL_V9", today_str)
            set_memory("FORCE_CLEAN_TRUE_FINAL_V9", "YES")

        range_values = dashboard_sheet.get("A8:T28")
        if not range_values: return

        MARKET_ROW = 18
        ceVolArray, ceOiArray, peVolArray, peOiArray = [], [], [], []

        # CE Scanning: Row 15 to 28
        for r in range(15, 29):
            if r == MARKET_ROW: continue
            array_index = r - 8
            if array_index < 0 or array_index >= len(range_values): continue
            
            row_data = range_values[array_index]
            if len(row_data) <= 13: continue

            strike_val = row_data[13] # Column N
            strike = float(strike_val) if strike_val not in [None, "", "NAN"] else 0
            if not strike: continue

            ce_oi_val = row_data[7] if len(row_data) > 7 else "0"   # Column H
            ce_vol_val = row_data[8] if len(row_data) > 8 else "0"  # Column I
            
            ce_oi_raw = str(ce_oi_val).replace("%", "").strip() if ce_oi_val else "0"
            ce_vol_raw = str(ce_vol_val).replace("%", "").strip() if ce_vol_val else "0"

            if "strong" in ce_oi_raw.lower(): ce_oi_raw = "100"
            if "strong" in ce_vol_raw.lower(): ce_vol_raw = "100"

            ceOiArray.append({'strike': strike, 'val': float(ce_oi_raw) if ce_oi_raw.replace('.','',1).isdigit() else 0, 'row': r})
            ceVolArray.append({'strike': strike, 'val': float(ce_vol_raw) if ce_vol_raw.replace('.','',1).isdigit() else 0, 'row': r})

        # PE Scanning: Row 8 to 21
        for r in range(8, 22):
            if r == MARKET_ROW: continue
            array_index = r - 8
            if array_index < 0 or array_index >= len(range_values): continue
            
            row_data = range_values[array_index]
            if len(row_data) <= 19: continue

            strike_val = row_data[13] # Column N
            strike = float(strike_val) if strike_val not in [None, "", "NAN"] else 0
            if not strike: continue

            pe_vol_val = row_data[18] if len(row_data) > 18 else "0" # Column S
            pe_oi_val = row_data[19] if len(row_data) > 19 else "0"  # Column T
            
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

        if not ceVolTop['highest']['strike'] or not ceOiTop['highest']['strike'] or not peVolTop['highest']['strike'] or not peOiTop['highest']['strike']:
            return

        # EXACT GAS detectLiveStateAdvanced Logic
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
            if not top_obj or not top_obj['highest'] or not top_obj['highest']['strike'] or top_obj['highest']['strike'] == 0:
                return "stable strong"
            
            # Agar percentage 75% ya usse zyada hai, toh yeh seedha WTT/WTB return karega ("to strong" nahi banega)
            if top_obj['pct'] >= 75.0:
                return raw_state

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

        # ==========================
        # CE WINNER ENGINE (CORRECTED)
        # ==========================
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
        elif ceVolIsStable100 and not ceOiIsStable100 and (ceOiTop['second'] and ceOiTop['second']['strike'] > ceOiTop['highest']['strike']) and (ceOiTop['highest']['strike'] < ceVolTop['highest']['strike']):
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

        # ==========================
        # PE WINNER ENGINE (CORRECTED)
        # ==========================
        peWinner = "VOL"
        if peVolTop['highest']['strike'] == peOiTop['highest']['strike'] and peOiRawState == "wtt" and peVolRawState == "wtt":
            peWinner = "BOTH"
        elif peVolTop['highest']['strike'] == peOiTop['highest']['strike'] and not peOiIsStable100 and not peVolIsStable100 and (peOiTop['second']['strike'] < peOiTop['highest']['strike']) and (peVolTop['second']['strike'] < peOiTop['highest']['strike']):
            peWinner = "BOTH"
        elif peVolTop['highest']['strike'] == peOiTop['highest']['strike'] and peOiIsStable100 and peVolIsStable100:
            peWinner = "BOTH"
        elif peVolTop['highest']['strike'] == peOiTop['highest']['strike'] and peOiIsStable100 and not peVolIsStable100 and (peVolTop['second'] and peVolTop['second']['strike'] < peOiTop['highest']['strike']):
            peWinner = "OI"
        elif peVolTop['highest']['strike'] == peOiTop['highest']['strike'] and peVolIsStable100 and not peOiIsStable100 and (peOiTop['second'] and peOiTop['second']['strike'] < peOiTop['highest']['strike']):
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

        if ceOiRawState in ["wtt", "wtb"]: set_memory("WEAK_HIST_CE_OI", ceOiRawState)
        if ceVolRawState in ["wtt", "wtb"]: set_memory("WEAK_HIST_CE_VOL", ceVolRawState)
        if peOiRawState in ["wtt", "wtb"]: set_memory("WEAK_HIST_PE_OI", peOiRawState)
        if peVolRawState in ["wtt", "wtb"]: set_memory("WEAK_HIST_PE_VOL", peVolRawState)

        current_time_str = now_dt.strftime("%I:%M %p").lower()

        # ==========================
        # CALCULATE INDEPENDENT CELL (EXACT GAS)
        # ==========================
        def calculate_independent_cell(top_obj, cache_key, side_label, is_winner, raw_state, final_status_text):
            if not top_obj or not top_obj['highest'] or not top_obj['highest']['strike'] or top_obj['highest']['strike'] == 0:
                return {'text': "(-----)", 'status': "stable strong", 'rawState': "STRONG", 'scenarioStatus': "stable strong"}

            strike = top_obj['highest']['strike']
            second_strike = top_obj['second']['strike'] if (top_obj['second'] and top_obj['second']['val'] > 0) else strike
            
            prev_strike_str = get_memory("PREV_STRK_" + cache_key)
            prev_strike = float(prev_strike_str) if prev_strike_str else strike
            
            if not prev_strike_str:
                set_memory("PREV_STRK_" + cache_key, str(strike))
                prev_strike = strike
            
            display_status = final_status_text 
            scenario_status = final_status_text
            
            is_shifting = False
            shift_type = get_memory("SHIFT_TYPE_" + cache_key, "")
            shift_from_strike = float(get_memory("SHIFT_FROM_" + cache_key, 0) or 0)
            last_target_strike = float(get_memory("SHIFT_TARGET_" + cache_key, 0) or 0)
            
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
            
            if not is_winner and raw_state != "STRONG": 
                display_status = "stable strong" 
                scenario_status = "stable strong"
            
            display_time = get_memory("TIME_" + cache_key)
            last_recorded_status = get_memory("LAST_STATUS_" + cache_key, "")

            is_trig = display_status in ["wtb", "wtt", "wtb to strong", "wtt to strong"] or is_shifting

            if is_trig and display_status != last_recorded_status:
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

        ceOiResult  = calculate_independent_cell(ceOiTop,  "CE_OI",  "Eor OI",  ceWinner in ["OI", "BOTH"], ceOiRawState,  ceOiStatusText)
        ceVolResult = calculate_independent_cell(ceVolTop, "CE_VOL", "Eor VOL", ceWinner in ["VOL", "BOTH"], ceVolRawState, ceVolStatusText)
        peVolResult = calculate_independent_cell(peVolTop, "PE_VOL", "Eos VOL", peWinner in ["VOL", "BOTH"], peVolRawState, peVolStatusText)
        peOiResult  = calculate_independent_cell(peOiTop,  "PE_OI",  "Eos OI",  peWinner in ["OI", "BOTH"], peOiRawState,  peOiStatusText)

        k3Text = "Nifty is Neutral"
        finalCeState = ceOiResult['scenarioStatus'] if ceWinner == "OI" else ceVolResult['scenarioStatus']
        finalPeState = peOiResult['scenarioStatus'] if ceWinner == "OI" else peVolResult['scenarioStatus']

        if ceWinner == "BOTH": finalCeState = ceVolResult['scenarioStatus'] if "to strong" in ceVolResult['scenarioStatus'] else ceOiResult['scenarioStatus']
        if peWinner == "BOTH": finalPeState = peVolResult['scenarioStatus'] if "to strong" in peVolResult['scenarioStatus'] else peOiResult['scenarioStatus']

        checkCe = finalCeState
        checkPe = finalPeState

        # ==========================================
        # 27 SCENARIOS MAPPING (EXACT GAS MATCH)
        # ==========================================
        if checkCe == "wtt" and checkPe == "wtb": k3Text = "Nifty is 8th scenario both side Risky"
        elif checkCe == "wtb" and checkPe == "wtt": k3Text = "Nifty is 9th scenario both side Risky"
        elif checkCe == "stable strong" and checkPe == "stable strong": k3Text = "Nifty is Neutral"
        elif checkCe == "wtb" and checkPe == "stable strong": k3Text = "Nifty is slightly Bearish"
        elif checkCe == "stable strong" and checkPe == "wtb": k3Text = "Nifty is slightly Bearish"
        elif checkCe == "wtt" and checkPe == "stable strong": k3Text = "Nifty is slightly bullish"
        elif checkCe == "stable strong" and checkPe == "wtt": k3Text = "Nifty is slightly bullish"
        elif checkCe == "wtt" and checkPe == "wtt": k3Text = "Nifty is Bullrun"
        elif checkCe == "wtb" and checkPe == "wtb": k3Text = "Nifty is bloodbath"
        elif checkCe == "wtb" and checkPe == "wtt to strong": k3Text = "Nifty is confirm Bloodbath"
        elif checkCe == "wtt to strong" and checkPe == "wtb": k3Text = "Nifty is confirm Bloodbath"
        elif checkCe == "wtt" and checkPe == "wtt to strong": k3Text = "Nifty is slightly bullish (Both side trade)"
        elif checkCe == "wtt to strong" and checkPe == "wtt": k3Text = "Nifty is slightly bullish (Both side trade)"
        elif checkCe == "wtt" and checkPe == "wtb to strong": k3Text = "Nifty is confirm Bull Run"
        elif checkCe == "wtb to strong" and checkPe == "wtt": k3Text = "Nifty is confirm Bull Run"
        elif checkCe == "wtb to strong" and checkPe == "wtb": k3Text = "Nifty is slightly Bearish (Both side trade)"
        elif checkCe == "wtb" and checkPe == "wtb to strong": k3Text = "Nifty is slightly Bearish (both side trade)"
        elif checkCe == "wtb to strong" and checkPe == "stable strong": k3Text = "Nifty is slightly Bullish"
        elif checkCe == "stable strong" and checkPe == "wtb to strong": k3Text = "Nifty is slightly Bullish"
        elif checkCe == "wtt to strong" and checkPe == "stable strong": k3Text = "Nifty is slightly Bearish"
        elif checkCe == "stable strong" and checkPe == "wtt to strong": k3Text = "Nifty is slightly Bearish"
        elif checkCe == "wtt to strong" and checkPe == "wtb to strong": k3Text = "Nifty is 8th scenario both side Risky trade"
        elif checkCe == "wtb to strong" and checkPe == "wtt to strong": k3Text = "Nifty is 9th scenario both side Risky trade"
        elif checkCe == "wtb to strong" and checkCe == "wtb to strong": k3Text = "Nifty is confirm Bull Run"
        elif checkCe == "wtt to strong" and checkCe == "wtt to strong": k3Text = "Nifty is confirm bloodbath"

        finalD5, finalH5, finalQ5, finalU5 = "(-----)", "(-----)", "(-----)", "(-----)"

        if ceWinner == "BOTH": finalD5, finalH5 = ceOiResult['text'], ceVolResult['text']
        elif ceWinner == "VOL": finalH5 = ceVolResult['text']
        elif ceWinner == "OI": finalD5 = ceOiResult['text']

        if peWinner == "BOTH": finalU5, finalQ5 = peOiResult['text'], peVolResult['text']
        elif peWinner == "VOL": finalQ5 = peVolResult['text']
        elif peWinner == "OI": finalU5 = peOiResult['text']

        safe_update_cell(dashboard_sheet, "D5", finalD5)
        safe_update_cell(dashboard_sheet, "U5", finalU5)
        safe_update_cell(dashboard_sheet, "H5", finalH5)
        safe_update_cell(dashboard_sheet, "Q5", finalQ5)
        safe_update_cell(dashboard_sheet, "K3", k3Text)

        print(f"✅ GAS Matched Success -> K3: '{k3Text}' (CE: {checkCe}, PE: {checkPe})")

    except Exception as err:
        print(f"❌ Strict Engine Error: {err}")

# Background runner thread setup for Flask + Main Loop
if __name__ == "__main__":
    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()

    print("🚀 Exact GAS Matched Python Engine started...")
    while True:
        execute_strict_independent_matrix()
        time.sleep(10)
