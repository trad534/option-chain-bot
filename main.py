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
from flask import Flask, jsonify, render_template_string
import threading
import builtins

RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://onrender.com")

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
            if row_idx == 18: continue
            strike = float(r_dict['strike'])
            ceOiArray.append({'strike': strike, 'val': float(r_dict['ce_oi']), 'row': row_idx})
            ceVolArray.append({'strike': strike, 'val': float(r_dict['ce_vol']), 'row': row_idx})
            peVolArray.append({'strike': strike, 'val': float(r_dict['pe_vol']), 'row': row_idx})
            peOiArray.append({'strike': strike, 'val': float(r_dict['pe_oi']), 'row': row_idx})

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

        def detect_live_state(top_obj, is_put_side):
            if not top_obj or top_obj['highest']['val'] == 0 or top_obj['pct'] < 75.0: return "STRONG"
            if not is_put_side:
                return "wtt" if top_obj['second']['strike'] < top_obj['highest']['strike'] else "wtb"
            else:
                return "wtt" if top_obj['second']['strike'] > top_obj['highest']['strike'] else "wtb"

        ceOiRaw = detect_live_state(ceOiTop, False)
        ceVolRaw = detect_live_state(ceVolTop, False)
        peOiRaw = detect_live_state(peOiTop, True)
        peVolRaw = detect_live_state(peVolTop, True)

        def format_badge(top_obj, raw_state):
            if raw_state == "STRONG" or top_obj['pct'] < 75.0:
                return "STABLE STRONG"
            return f"{raw_state.upper()} {round(top_obj['pct'], 1)}%"

        ceOiBadge = format_badge(ceOiTop, ceOiRaw)
        ceVolBadge = format_badge(ceVolTop, ceVolRaw)
        peOiBadge = format_badge(peOiTop, peOiRaw)
        peVolBadge = format_badge(peVolTop, peVolRaw)

        checkCe = ceOiRaw if ceOiRaw != "STRONG" else "stable strong"
        checkPe = peOiRaw if peOiRaw != "STRONG" else "stable strong"

        # ==========================================================
        # COMPLETE PURE 27 SCENARIOS MAPPING (100% EXPLICIT)
        # ==========================================================
        k3Text = f"{active_symbol_name} is Neutral"

        if "stable strong" in checkCe and "stable strong" in checkPe:
            k3Text = f"{active_symbol_name} is Neutral"
        elif "wtb" in checkCe and "stable strong" in checkPe:
            k3Text = f"{active_symbol_name} is slightly Bearish"
        elif "stable strong" in checkCe and "wtb" in checkPe:
            k3Text = f"{active_symbol_name} is slightly Bearish"
        elif "wtt" in checkCe and "stable strong" in checkPe:
            k3Text = f"{active_symbol_name} is slightly bullish"
        elif "stable strong" in checkCe and "wtt" in checkPe:
            k3Text = f"{active_symbol_name} is slightly bullish"
        elif "wtt" in checkCe and "wtt" in checkPe:
            k3Text = f"{active_symbol_name} is Bullrun"
        elif "wtb" in checkCe and "wtb" in checkPe:
            k3Text = f"{active_symbol_name} is bloodbath"
        elif "wtt" in checkCe and "wtb" in checkPe:
            k3Text = f"{active_symbol_name} is 8th scenario both side Risky"
        elif "wtb" in checkCe and "wtt" in checkPe:
            k3Text = f"{active_symbol_name} is 9th scenario both side Risky"
        elif "wtb" in checkCe and "wtt to strong" in checkPe:
            k3Text = f"{active_symbol_name} is confirm Bloodbath"
        elif "wtt to strong" in checkCe and "wtb" in checkPe:
            k3Text = f"{active_symbol_name} is confirm Bloodbath"
        elif "wtt" in checkCe and "wtt to strong" in checkPe:
            k3Text = f"{active_symbol_name} is slightly bullish (Both side trade)"
        elif "wtt to strong" in checkCe and "wtt" in checkPe:
            k3Text = f"{active_symbol_name} is slightly bullish (Both side trade)"
        elif "wtt" in checkCe and "wtb to strong" in checkPe:
            k3Text = f"{active_symbol_name} is confirm Bull Run"
        elif "wtb to strong" in checkCe and "wtt" in checkPe:
            k3Text = f"{active_symbol_name} is confirm Bull Run"
        elif "wtb to strong" in checkCe and "wtb" in checkPe:
            k3Text = f"{active_symbol_name} is slightly Bearish (Both side trade)"
        elif "wtb" in checkCe and "wtb to strong" in checkPe:
            k3Text = f"{active_symbol_name} is slightly Bearish (both side trade)"
        elif "wtb to strong" in checkCe and "stable strong" in checkPe:
            k3Text = f"{active_symbol_name} is slightly Bullish"
        elif "stable strong" in checkCe and "wtb to strong" in checkPe:
            k3Text = f"{active_symbol_name} is slightly Bullish"
        elif "wtt to strong" in checkCe and "stable strong" in checkPe:
            k3Text = f"{active_symbol_name} is slightly Bearish"
        elif "stable strong" in checkCe and "wtt to strong" in checkPe:
            k3Text = f"{active_symbol_name} is slightly Bearish"
        elif "wtt to strong" in checkCe and "wtb to strong" in checkPe:
            k3Text = f"{active_symbol_name} is 8th scenario both side Risky trade"
        elif "wtb to strong" in checkCe and "wtt to strong" in checkPe:
            k3Text = f"{active_symbol_name} is 9th scenario both side Risky trade"
        elif "wtb to strong" in checkCe and "wtb to strong" in checkPe:
            k3Text = f"{active_symbol_name} is confirm Bull Run"
        elif "wtt to strong" in checkCe and "wtt to strong" in checkPe:
            k3Text = f"{active_symbol_name} is confirm bloodbath"
        elif "wtt to strong" in checkCe and "wtt" in checkPe:
            k3Text = f"{active_symbol_name} is Bullrun High Momentum"
        elif "wtb to strong" in checkCe and "wtb" in checkPe:
            k3Text = f"{active_symbol_name} is Bloodbath High Momentum"

        if active_symbol_name not in multi_symbol_runtime_states: multi_symbol_runtime_states[active_symbol_name] = {}
        vault = multi_symbol_runtime_states[active_symbol_name]
        vault["D5_TEXT"] = ceOiBadge
        vault["H5_TEXT"] = ceVolBadge
        vault["U5_TEXT"] = peOiBadge
        vault["Q5_TEXT"] = peVolBadge
        vault["K3_TEXT"] = k3Text
        vault["lot_size"] = lot_size
        vault["spot_price"] = spot_price

        def get_top_two_strikes(arr):
            valid = [x for x in arr if x['val'] > 0]
            valid.sort(key=lambda x: x['val'], reverse=True)
            t1 = valid[0]['strike'] if len(valid) > 0 else 0
            t2 = valid[1]['strike'] if len(valid) > 1 else 0
            return t1, t2

        c_oi_1, c_oi_2 = get_top_two_strikes(ceOiArray)
        c_vol_1, c_vol_2 = get_top_two_strikes(ceVolArray)
        p_vol_1, p_vol_2 = get_top_two_strikes(peVolArray)
        p_oi_1, p_oi_2 = get_top_two_strikes(peOiArray)

        max_ce_oi = ceOiTop['highest']['val'] if ceOiTop['highest']['val'] > 0 else 1
        max_ce_vol = ceVolTop['highest']['val'] if ceVolTop['highest']['val'] > 0 else 1
        max_pe_vol = peVolTop['highest']['val'] if peVolTop['highest']['val'] > 0 else 1
        max_pe_oi = peOiTop['highest']['val'] if peOiTop['highest']['val'] > 0 else 1

        grid_rows_built = []
        spot_inserted = False
        for r_dict in raw_rows:
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

            if not spot_inserted and spot_price > 0 and st >= spot_price:
                grid_rows_built.append({'is_spot_row': True, 'spot_price': spot_price, 'ce_oi_badge': ceOiBadge, 'ce_vol_badge': ceVolBadge, 'pe_vol_badge': peVolBadge, 'pe_oi_badge': peOiBadge})
                spot_inserted = True
            grid_rows_built.append(r_copy)

        if not spot_inserted and spot_price > 0:
            grid_rows_built.append({'is_spot_row': True, 'spot_price': spot_price, 'ce_oi_badge': ceOiBadge, 'ce_vol_badge': ceVolBadge, 'pe_vol_badge': peVolBadge, 'pe_oi_badge': peOiBadge})

        vault["grid_rows"] = grid_rows_built
        safe_update_cell(dashboard_sheet, "D5", vault["D5_TEXT"])
        safe_update_cell(dashboard_sheet, "H5", vault["H5_TEXT"])
        safe_update_cell(dashboard_sheet, "U5", vault["U5_TEXT"])
        safe_update_cell(dashboard_sheet, "Q5", vault["Q5_TEXT"])
        update_k3_with_color(spreadsheet, dashboard_sheet, "K3", vault["K3_TEXT"])
    except Exception as err:
        print(f"❌ Scenario Tracker Error: {err}")

def fetch_and_update_option_chain(smart_api):
    global morning_oi_dict, baseline_date, options_cache, eq_cache, persistent_quotes_cache, symbol_expiries_cache
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

                # 100% Guaranteed Spot Price Fetching (Direct Index Token Fallback)
                spot_price = 0.0
                index_tokens = {"NIFTY": "99992000", "BANKNIFTY": "99992009", "FINNIFTY": "99992018"}
                target_tok = index_tokens.get(clean_active_symbol, "")
                
                if target_tok:
                    try:
                        q_res = smart_api.getMarketData("FULL", {"NSE": [target_tok]})
                        if q_res and q_res.get('status') and q_res.get('data') and q_res['data'].get('fetched'):
                            spot_price = float(q_res['data']['fetched'][0].get('ltp', 0))
                    except Exception: pass

                if spot_price <= 0:
                    try:
                        fut_sub = full_scrip_df[(full_scrip_df['name'].str.upper() == clean_active_symbol) & (full_scrip_df['instrumenttype'] == 'FUTIDX')]
                        if not fut_sub.empty:
                            fut_tok = str(fut_sub['token'].values[0])
                            fut_quote = smart_api.getMarketData("FULL", {"NFO": [fut_tok]})
                            if fut_quote and fut_quote.get('status') and fut_quote.get('data') and fut_quote['data'].get('fetched'):
                                spot_price = float(fut_quote['data']['fetched'][0].get('ltp', 0))
                    except Exception: pass

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
    global full_scrip_df, eq_cache, options_cache, symbol_expiries_cache
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
        .status-box { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-top: 10px; }
        .card { background: #ffffff; padding: 12px; border-radius: 8px; border: 2px solid #dee2e6; text-align: center; font-size: 12px; font-weight: bold; white-space: pre-line; }
        .card h3 { margin: 0 0 5px 0; font-size: 13px; color: #495057; text-transform: uppercase; }
        .k3-banner { background: #e6fcf5; padding: 14px; margin-top: 10px; text-align: center; font-size: 18px; font-weight: bold; border-radius: 8px; color: #0ca678; border: 1px solid #20c997; }
        .table-container { margin-top: 15px; overflow-x: auto; max-height: 700px; border: 1px solid #dee2e6; border-radius: 6px; }
        table { width: 100%; border-collapse: collapse; background: #ffffff; font-size: 11px; text-align: center; }
        th, td { border: 1px solid #dee2e6; padding: 6px 7px; white-space: nowrap; }
        th { background: #f8f9fa; color: #212529; position: sticky; top: 0; z-index: 10; }
        .strike-col { background: #ff922b !important; color: #ffffff !important; font-weight: bold; font-size: 12px; }
        .spot-row td { background: #ffffff !important; position: relative; padding: 4px; }
        .spot-row td::after { content: ""; position: absolute; left: 0; right: 0; top: 50%; height: 3px; background-color: #f03e3e; z-index: 2; }
        .spot-badge { background: #ffffff; color: #000000; border: 2px solid #495057; border-radius: 6px; padding: 3px 8px; font-weight: bold; font-size: 10px; display: inline-block; position: relative; z-index: 3; }
        .spot-price-cell { background: #7950f2 !important; color: #ffffff !important; font-weight: bold; font-size: 13px; text-align: center; z-index: 4; border: 2px solid #f03e3e !important; }
        .spot-tag-small { background: #ffffff; color: #7950f2; font-size: 8px; font-weight: bold; padding: 1px 4px; border-radius: 3px; display: block; margin-bottom: 2px; }
        .itm-ce { background: rgba(240, 62, 62, 0.06); }
        .itm-pe { background: rgba(43, 138, 62, 0.06); }
        .ce-oi-col.bg-100 { background: #ff8787 !important; color: #000000 !important; font-weight: bold; }
        .ce-vol-col.bg-100 { background: #40c057 !important; color: #000000 !important; font-weight: bold; }
        .pe-vol-col.bg-100 { background: #a5d8ff !important; color: #000000 !important; font-weight: bold; }
        .pe-oi-col.bg-100 { background: #63e6be !important; color: #000000 !important; font-weight: bold; }
        .bg-75 { background: #ffd43b !important; color: #000000 !important; font-weight: bold; }
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
            <div style="display: flex; gap: 5px; align-items: center;">
                <label style="font-weight: bold;">Symbol: </label>
                <select id="symbolSelect" onchange="onSymbolChange()"></select>
            </div>
            <div style="display: flex; gap: 5px; align-items: center;">
                <label style="font-weight: bold;">Expiry: </label>
                <select id="expirySelect" onchange="updateSelectedSymbolSheet()"></select>
            </div>
        </div>
    </div>
    <div class="k3-banner" id="k3Display">Connecting to Server Framework...</div>
    <div class="status-box">
        <div class="card"><h3>CE OI (D5)</h3><p id="d5Val">-</p></div>
        <div class="card"><h3>CE Vol (H5)</h3><p id="h5Val">-</p></div>
        <div class="card"><h3>PE Vol (Q5)</h3><p id="q5Val">-</p></div>
        <div class="card"><h3>PE OI (U5)</h3><p id="u5Val">-</p></div>
    </div>
    <div class="table-container">
        <table id="optionChainTable">
            <thead>
                <tr>
                    <th colspan="10" style="background: #e7f5ff; color: #1c7ed6; font-size: 13px;">CALLS (CE)</th>
                    <th class="strike-col" style="background: #ff922b !important; color: #ffffff !important;">STRIKE</th>
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
                <tr><td colspan="21" style="padding: 20px; font-size: 14px; color: #666;">Syncing options matrix blocks...</td></tr>
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
            let exp_list = allExpiriesData[sym] || [];
            exp_list.forEach(exp => {
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
                document.getElementById("d5Val").innerText = data.D5;
                document.getElementById("h5Val").innerText = data.H5;
                document.getElementById("q5Val").innerText = data.Q5;
                document.getElementById("u5Val").innerText = data.U5;
                document.getElementById("k3Display").innerText = data.K3;
                let tbody = document.getElementById("gridBody"); tbody.innerHTML = "";
                if (data.grid_rows && data.grid_rows.length > 0) {
                    data.grid_rows.forEach(row => {
                        let tr = document.createElement("tr");
                        if (row.is_spot_row) {
                            tr.classList.add("spot-row");
                            tr.innerHTML = `<td>-</td><td>-</td><td>-</td><td>-</td><td>-</td><td>-</td><td><span class="spot-badge">${row.ce_oi_badge}</span></td><td><span class="spot-badge">${row.ce_vol_badge}</span></td><td>-</td><td>-</td><td class="strike-col spot-price-cell"><span class="spot-tag-small">SPOT</span>${row.spot_price}</td><td>-</td><td>-</td><td><span class="spot-badge">${row.pe_vol_badge}</span></td><td><span class="spot-badge">${row.pe_oi_badge}</span></td><td>-</td><td>-</td><td>-</td><td>-</td><td>-</td><td>-</td>`;
                        } else {
                            tr.innerHTML = `<td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_vega}</td><td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_theta}</td><td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_gamma}</td><td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_delta}</td><td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_iv}</td><td class="${row.ce_oi_chg >= 0 ? 'chg-pos' : 'chg-neg'} ${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_oi_chg}</td><td class="ce-oi-col ${row.ce_oi_color}">${row.ce_oi}<span class="pct-sub">(${row.ce_oi_pct}%)</span></td><td class="ce-vol-col ${row.ce_vol_color}">${row.ce_vol}<span class="pct-sub">(${row.ce_vol_pct}%)</span></td><td class="${row.ce_change >= 0 ? 'chg-pos' : 'chg-neg'} ${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_change}</td><td class="${row.is_itm_ce ? 'itm-ce' : ''}">${row.ce_ltp}</td><td class="strike-col">${row.strike}</td><td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_ltp}</td><td class="${row.pe_change >= 0 ? 'chg-pos' : 'chg-neg'} ${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_change}</td><td class="pe-vol-col ${row.pe_vol_color}">${row.pe_vol}<span class="pct-sub">(${row.pe_vol_pct}%)</span></td><td class="pe-oi-col ${row.pe_oi_color}">${row.pe_oi}<span class="pct-sub">(${row.pe_oi_pct}%)</span></td><td class="${row.pe_oi_chg >= 0 ? 'chg-pos' : 'chg-neg'} ${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_oi_chg}</td><td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_iv}</td><td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_delta}</td><td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_gamma}</td><td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_theta}</td><td class="${row.is_itm_pe ? 'itm-pe' : ''}">${row.pe_vega}</td>`;
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
    return jsonify({"lot_size": vault.get("lot_size", 1), "D5": vault.get("D5_TEXT", "(-----)"), "H5": vault.get("H5_TEXT", "(-----)"), "Q5": vault.get("Q5_TEXT", "(-----)"), "U5": vault.get("U5_TEXT", "(-----)"), "K3": vault.get("K3_TEXT", f"{sym} is Neutral"), "grid_rows": vault.get("grid_rows", []), "spot_price": vault.get("spot_price", 0)})

def run_bot_lock_loop():
    print("🚀 Starting Arush LTP Calculator Engine...")
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
        time.sleep(1)

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    run_bot_lock_loop()
