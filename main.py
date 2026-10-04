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
# 1. FORCE FLUSH LOGS
# ==========================================
def print(*args, **kwargs):
    kwargs['flush'] = True
    builtins.print(*args, **kwargs)

# ==========================================
# 2. FLASK WEB SERVER FOR RENDER
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return "Angel One Option Chain & S20 Fixed Bot is active!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# ==========================================
# 3. CONFIGURATION & GOOGLE SHEETS
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
# 4. MEMORY & UTILITIES
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
            "requests": [{
                "repeatCell": {
                    "range": {
                        "sheetId": sheet.id,
                        "startRowIndex": row - 1, "endRowIndex": row,
                        "startColumnIndex": col - 1, "endColumnIndex": col
                    },
                    "cell": {"userEnteredFormat": {"textFormat": {"foregroundColor": color_dict}}},
                    "fields": "userEnteredFormat.textFormat.foregroundColor"
                }
            }]
        }
        spreadsheet_obj.batch_update(body)
    except Exception as e:
        print(f"⚠ K3 Color Formatting Error: {e}")

# ==========================================
# 5. IV & GREEKS CALCULATION
# ==========================================
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
# 6. MASTER SCENARIO ENGINE (WITH S20 FIX)
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

        # Range A8:V28 liya hai taaki S20 aur aakhri columns miss na ho
        range_values = dashboard_sheet.get("A8:V28")
        if not range_values: return

        MARKET_ROW = 18
        ceVolArray, ceOiArray, peVolArray, peOiArray = [], [], [], []

        # CE Side Loop
        for r in range(15, 29):
            if r == MARKET_ROW: continue
            array_index = r - 8
            if array_index < 0 or array_index >= len(range_values): continue
            
            row_data = range_values[array_index]
            while len(row_data) < 22: row_data.append("0")

            strike_val = row_data[13]
            strike = float(strike_val) if strike_val not in [None, "", "NAN"] else 0
            if not strike: continue

            ce_oi_raw = str(row_data[7]).replace("%", "").strip() if len(row_data) > 7 else "0"
            ce_vol_raw = str(row_data[8]).replace("%", "").strip() if len(row_data) > 8 else "0"

            if "strong" in ce_oi_raw.lower(): ce_oi_raw = "100"
            if "strong" in ce_vol_raw.lower(): ce_vol_raw = "100"

            ceOiArray.append({'strike': strike, 'val': float(ce_oi_raw) if ce_oi_raw.replace('.','',1).isdigit() else 0, 'row': r})
            ceVolArray.append({'strike': strike, 'val': float(ce_vol_raw) if ce_vol_raw.replace('.','',1).isdigit() else 0, 'row': r})

        # PE Side Loop (Including S20 - Row 21 tak)
        for r in range(8, 22):
            if r == MARKET_ROW: continue
            array_index = r - 8
            if array_index < 0 or array_index >= len(range_values): continue
            
            row_data = range_values[array_index]
            while len(row_data) < 22: row_data.append("0") # Padding taaki row skip na ho

            strike_val = row_data[13]
            strike = float(strike_val) if strike_val not in [None, "", "NAN"] else 0
            if not strike: continue

            pe_vol_raw = str(row_data[18]).replace("%", "").strip() if len(row_data) > 18 else "0"
            pe_oi_raw = str(row_data[19]).replace("%", "").strip() if len(row_data) > 19 else "0"

            if "strong" in pe_vol_raw.lower(): pe_vol_raw = "100"
            if "strong" in pe_oi_raw.lower(): pe_oi_raw = "100"

            peVolArray.append({'strike': strike, 'val': float(pe_vol_raw) if pe_vol_raw.replace('.','',1).isdigit() else 0, 'row': r})
            peOiArray.append({'strike': strike, 'val': float(pe_oi_raw) if pe_oi_raw.replace('.','',1).isdigit() else 0, 'row': r})

        def extract_top_two(data_array):
            filtered = [item for item in data_array if item['val'] > 0]
            if not filtered:
                return {'highest': {'strike': 0, 'val': 0, 'row': 0}, 'second': {'strike': 0, 'val': 0, 'row': 0}, 'pct': 0}
            
            filtered.sort(key=lambda x: (x['val'], x['row']), reverse=True)
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
                return "wtt" if top_obj['second']['strike'] < top_obj['highest']['strike'] else "wtb"
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
                    if top_obj['pct'] < 82.0: return current_weak_hist + " to strong"
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

        ceWinner = "VOL"
        peWinner = "VOL"
        
        # Winner calculation simplified for stable output
        current_time_str = now_dt.strftime("%I:%M %p").lower()

        def calculate_independent_cell(top_obj, cache_key, side_label, is_winner, raw_state, final_status_text):
            if not top_obj or not top_obj['highest']['strike']: 
                return {'text': "(-----)", 'status': "stable strong", 'scenarioStatus': "stable strong"}
            
            strike = top_obj['highest']['strike']
            second_strike = top_obj['second']['strike'] if (top_obj['second'] and top_obj['second']['val'] > 0) else strike
            display_status = final_status_text
            scenario_status = final_status_text

            if not is_winner and raw_state != "STRONG":
                display_status = "stable strong"
                scenario_status = "stable strong"

            output_text = f"({current_time_str} - {side_label}-{strike:g}-{display_status}-{second_strike:g})"
            return {'text': output_text, 'status': display_status, 'scenarioStatus': scenario_status}

        ceOiResult  = calculate_independent_cell(ceOiTop, "CE_OI", "Eor OI", True, ceOiRawState, ceOiStatusText)
        ceVolResult = calculate_independent_cell(ceVolTop, "CE_VOL", "Eor VOL", True, ceVolRawState, ceVolStatusText)
        peVolResult = calculate_independent_cell(peVolTop, "PE_VOL", "Eos VOL", True, peVolRawState, peVolStatusText)
        peOiResult  = calculate_independent_cell(peOiTop, "PE_OI", "Eos OI", True, peOiRawState, peOiStatusText)

        checkCe = ceOiResult['scenarioStatus']
        checkPe = peOiResult['scenarioStatus']

        active_symbol_name = "Nifty"
        try:
            j4_val = str(dashboard_sheet.cell(4, 10).value).strip()
            if j4_val and j4_val not in ["NONE", "NAN", "0", ""]:
                active_symbol_name = j4_val.replace("-EQ", "").replace(".NS", "").strip()
        except Exception:
            pass

        k3Text = f"{active_symbol_name} is Neutral"
        if checkCe == "wtt" and checkPe == "wtb": k3Text = f"{active_symbol_name} is 8th scenario both side Risky"
        elif checkCe == "wtb" and checkPe == "wtt": k3Text = f"{active_symbol_name} is 9th scenario both side Risky"
        elif checkCe == "wtt" and checkPe == "wtt": k3Text = f"{active_symbol_name} is Bullrun"
        elif checkCe == "wtb" and checkPe == "wtb": k3Text = f"{active_symbol_name} is bloodbath"

        safe_update_cell(dashboard_sheet, "D5", ceOiResult['text'])
        safe_update_cell(dashboard_sheet, "U5", peOiResult['text'])
        safe_update_cell(dashboard_sheet, "H5", ceVolResult['text'])
        safe_update_cell(dashboard_sheet, "Q5", peVolResult['text'])
        
        update_k3_with_color(spreadsheet, dashboard_sheet, "K3", k3Text)
        print(f"✅ SUCCESS -> K3 Updated with: '{k3Text}'")

    except Exception as err:
        print(f"❌ Critical Scenario Engine Error: {err}")

# ==========================================
# 7. MAIN FETCH LOOP & BOT RUNNER
# ==========================================
full_scrip_df = pd.DataFrame()
eq_cache = {}
options_cache = {}
morning_oi_dict = {}
baseline_date = None
cached_selected_symbol = None

def initialize_scrip_master(smart_api):
    global full_scrip_df, eq_cache, options_cache
    try:
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

            fno_opts = full_scrip_df[(full_scrip_df['strike'] > 0) & (full_scrip_df['exch_seg'] == 'NFO')].copy()
            fno_opts['strike'] = pd.to_numeric(fno_opts['strike'], errors='coerce')
            fno_opts['parsed_expiry'] = pd.to_datetime(fno_opts['expiry'], format='%d%b%Y', errors='coerce').fillna(pd.to_datetime(fno_opts['expiry'], errors='coerce'))
            
            for name, group in fno_opts.groupby(fno_opts['name'].str.upper()):
                options_cache[name.strip()] = group
            print(f"✅ Scrip Master Cached! Total Records: {len(full_scrip_df)}")
    except Exception as e:
        print(f"⚠ Scrip Master Error: {e}")

def fetch_and_update_option_chain(smart_api):
    global morning_oi_dict, baseline_date, cached_selected_symbol, options_cache, eq_cache
    try:
        now_dt = datetime.now()
        current_date_str = now_dt.strftime('%Y-%m-%d')
        if baseline_date != current_date_str:
            morning_oi_dict = {}
            baseline_date = current_date_str

        if not options_cache:
            initialize_scrip_master(smart_api)
            if not options_cache: return

        clean_symbol = "NIFTY"
        try:
            val = str(dashboard_sheet.cell(4, 10).value).strip().upper()
            if val and val not in ["NONE", "NAN", "0"]:
                clean_symbol = val.replace("-EQ", "").replace(".NS", "").strip()
        except Exception:
            pass

        spot_token, spot_exchange = eq_cache.get(clean_symbol, ("mis", "NSE"))
        spot_price = 24000.0
        if spot_token != "mis":
            try:
                spot_quote = smart_api.getMarketData("FULL", {spot_exchange: [spot_token]})
                if spot_quote and spot_quote.get('status'):
                    spot_price = float(spot_quote['data']['fetched'][0]['ltp'])
            except Exception:
                pass

        sym_options = options_cache.get(clean_symbol, pd.DataFrame())
        if sym_options.empty: return

        valid_opts = sym_options[sym_options['parsed_expiry'] >= pd.Timestamp(now_dt.date())].copy()
        if valid_opts.empty: return

        current_expiry_dt = sorted(valid_opts['parsed_expiry'].unique())[0]
        current_expiry_options = valid_opts[valid_opts['parsed_expiry'] == current_expiry_dt].copy()

        all_master_strikes = sorted(current_expiry_options['strike'].unique())
        if all_master_strikes:
            closest_idx = min(range(len(all_master_strikes)), key=lambda i: abs(all_master_strikes[i] - spot_price))
            strikes = all_master_strikes[max(0, closest_idx - 20):min(len(all_master_strikes), closest_idx + 21)]
        else:
            strikes = []

        filtered_opts = current_expiry_options[current_expiry_options['strike'].isin(strikes)].copy()
        tokens_map = {}
        for _, row in filtered_opts.iterrows():
            exch, tok = str(row['exch_seg']).strip(), str(row['token']).strip()
            tokens_map.setdefault(exch, []).append(tok)

        all_fetched_quotes = []
        for exch, t_list in tokens_map.items():
            for i in range(0, len(t_list), 40):
                try:
                    res = smart_api.getMarketData("FULL", {exch: t_list[i:i+40]})
                    if res and res.get('status'):
                        all_fetched_quotes.extend(res['data'].get('fetched', []))
                except Exception:
                    pass

        quotes_dict = {}
        for item in all_fetched_quotes:
            tok = str(item.get('symbolToken', item.get('token', ''))).strip()
            quotes_dict[tok] = {
                'ltp': float(item.get('ltp', 0)),
                'volume': float(item.get('tradeVolume', 0)),
                'oi': float(item.get('opnInterest', 0))
            }

        exp_date = current_expiry_dt.to_pydatetime().replace(hour=15, minute=30)
        t = max((exp_date - datetime.now()).total_seconds() / (365.0 * 24 * 3600), 0.0001)

        output_rows = [[
            "CE Vega", "CE Theta", "CE Gamma", "CE Delta", "CE IV", "CE OI Chg", "CE OI", "CE Vol", "CE Change", "CE LTP",
            "Strike Price",
            "PE LTP", "PE Change", "PE Vol", "PE OI", "PE OI Chg", "PE IV", "PE Delta", "PE Gamma", "PE Theta", "PE Vega"
        ]]

        for st in strikes:
            ce_sub = current_expiry_options[(current_expiry_options['strike'] == st) & (current_expiry_options['symbol'].str.upper().str.endswith('CE'))]
            pe_sub = current_expiry_options[(current_expiry_options['strike'] == st) & (current_expiry_options['symbol'].str.upper().str.endswith('PE'))]

            ce_tok = str(ce_sub['token'].values[0]).strip() if not ce_sub.empty else ""
            pe_tok = str(pe_sub['token'].values[0]).strip() if not pe_sub.empty else ""

            ce_q = quotes_dict.get(ce_tok, {'ltp': 0, 'volume': 0, 'oi': 0})
            pe_q = quotes_dict.get(pe_tok, {'ltp': 0, 'volume': 0, 'oi': 0})

            ce_gks = calculate_iv_and_greeks('CE', spot_price, st, t, 0.07, ce_q['ltp'])
            pe_gks = calculate_iv_and_greeks('PE', spot_price, st, t, 0.07, pe_q['ltp'])

            output_rows.append([
                ce_gks['vega'], ce_gks['theta'], ce_gks['gamma'], ce_gks['delta'], ce_gks['iv'], 0, ce_q['oi'], ce_q['volume'], 0, ce_q['ltp'],
                st,
                pe_q['ltp'], 0, pe_q['volume'], pe_q['oi'], 0, pe_gks['iv'], pe_gks['delta'], pe_gks['gamma'], pe_gks['theta'], pe_gks['vega']
            ])

        if len(output_rows) > 1:
            data_sheet.update(range_name=f"A10:U{10 + len(output_rows) - 1}", values=output_rows)

        execute_python_scenario_tracker()

    except Exception as ex:
        print(f"❌ Exception in fetch loop: {str(ex)}")

def run_bot_loop():
    print("🚀 Starting 0-to-1 Clean Bot...")
    try:
        smart_api = SmartConnect(api_key=API_KEY)
    except Exception as e:
        print(f"❌ SmartConnect Error: {e}")
        return

    while True:
        try:
            totp = pyotp.TOTP(TOTP_SECRET.replace(" ", "")).now() if TOTP_SECRET else "123456"
            data = smart_api.generateSession(CLIENT_ID, PIN, totp)
            if data and data.get('status'):
                print("✅ Session Generated Successfully!")
                initialize_scrip_master(smart_api)
                break
            time.sleep(5)
        except Exception:
            time.sleep(5)

    while True:
        try:
            fetch_and_update_option_chain(smart_api)
        except Exception:
            pass
        time.sleep(1)

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    run_bot_loop()
