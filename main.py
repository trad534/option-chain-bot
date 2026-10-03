import time
import os
import json
import requests
import pandas as pd
import numpy as np
import pyotp
import gspread
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
    return "Angel One 27-Scenarios Multi-Symbol Memory Bot is active!"

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
# ROBUST IV & GREEKS
# ==========================================
def calculate_iv_and_greeks(flag, S, K, t, r, ltp):
    if t <= 0 or S <= 0 or K <= 0 or ltp <= 0.05:
        delta = 1.0 if (flag == 'CE' and S > K) else (-1.0 if (flag == 'PE' and S < K) else 0.0)
        return {'iv': 0, 'delta': round(delta, 3), 'gamma': 0, 'theta': 0, 'vega': 0}

    intrinsic = max(0, S - K * np.exp(-r * t)) if flag == 'CE' else max(0, K * np.exp(-r * t) - S)
    if ltp < intrinsic:
        ltp = intrinsic + 0.05

    def objective_function(sigma):
        sigma = max(sigma, 1e-4)
        d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * t) / (sigma * np.sqrt(t))
        d2 = d1 - sigma * np.sqrt(t)
        price = (S * norm.cdf(d1) - K * np.exp(-r * t) * norm.cdf(d2)) if flag == 'CE' else (K * np.exp(-r * t) * norm.cdf(-d2) - S * norm.cdf(-d1))
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
        'iv': round(sigma * 100, 2),
        'delta': round(delta, 3),
        'gamma': round(gamma, 4),
        'theta': round(theta, 2),
        'vega': round(vega_val, 2)
    }

# ==========================================
# GLOBAL STATE & MEMORY CACHES
# ==========================================
full_scrip_df = pd.DataFrame()
eq_cache = {}          
options_cache = {}     
morning_oi_memory = {} 
symbol_history_state = {} 
baseline_date = None
cached_selected_symbol = None

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

            print("⚡ Pre-indexing EQ and Options...")
            nse_eq = full_scrip_df[(full_scrip_df['exch_seg'] == 'NSE') & (~full_scrip_df['instrumenttype'].str.contains('OPT|FUT', na=False))]
            for _, row in nse_eq.iterrows():
                sym = str(row['symbol']).strip().upper()
                name = str(row['name']).strip().upper()
                tok = str(row['token']).strip()
                eq_cache[sym] = (tok, 'NSE')
                eq_cache[name] = (tok, 'NSE')
                if sym.endswith("-EQ"):
                    eq_cache[sym.replace("-EQ", "")] = (tok, 'NSE')

            fno_opts = full_scrip_df[(full_scrip_df['strike'] > 0) & (full_scrip_df['exch_seg'] == 'NFO')].copy()
            fno_opts['strike'] = pd.to_numeric(fno_opts['strike'], errors='coerce')
            fno_opts['parsed_expiry'] = pd.to_datetime(fno_opts['expiry'], format='%d%b%Y', errors='coerce')
            fno_opts['parsed_expiry'] = fno_opts['parsed_expiry'].fillna(pd.to_datetime(fno_opts['expiry'], errors='coerce'))
            
            for name, group in fno_opts.groupby(fno_opts['name'].str.upper()):
                options_cache[name.strip()] = group

            if "TMCV" in options_cache:
                options_cache["TATAMOTORS"] = options_cache["TMCV"]
                options_cache["TATA"] = options_cache["TMCV"]

            print(f"✅ Scrip Master Cached! Total Records: {len(full_scrip_df)}")
    except Exception as e:
        print(f"⚠ Scrip Master Download Error: {e}")

# ==========================================
# PYTHON PORTED FULL 27-SCENARIOS & WTB/WTT ENGINE
# ==========================================
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

def detect_live_state_advanced(top_obj, is_put_side):
    if not top_obj or top_obj['highest']['val'] == 0 or top_obj['pct'] < 75.0:
        return "STRONG"
    if is_put_side:
        return "wtb" if top_obj['second']['strike'] < top_obj['highest']['strike'] else "wtt"
    else:
        return "wtt" if top_obj['second']['strike'] > top_obj['highest']['strike'] else "wtb"

def compute_python_scenarios(rows_data, symbol_key):
    global symbol_history_state
    if symbol_key not in symbol_history_state:
        symbol_history_state[symbol_key] = {}
    
    market_row = 18
    ce_vol_arr, ce_oi_arr, pe_vol_arr, pe_oi_arr = [], [], [], []

    for r in range(15, 29):
        if r == market_row: continue
        idx = r - 8
        if idx < 0 or idx >= len(rows_data): continue
        row = rows_data[idx]
        strike = float(row[10]) if row[10] != "" else 0 
        if not strike: continue
        
        ce_oi_val = str(row[6]).replace("%", "").strip()   
        ce_vol_val = str(row[7]).replace("%", "").strip()  
        
        if "strong" in ce_oi_val.lower(): ce_oi_val = "100"
        if "strong" in ce_vol_val.lower(): ce_vol_val = "100"

        ce_oi_arr.append({'strike': strike, 'val': float(ce_oi_val) if ce_oi_val.replace('.','',1).isdigit() else 0, 'row': r})
        ce_vol_arr.append({'strike': strike, 'val': float(ce_vol_val) if ce_vol_val.replace('.','',1).isdigit() else 0, 'row': r})

    for r in range(8, 22):
        if r == market_row: continue
        idx = r - 8
        if idx < 0 or idx >= len(rows_data): continue
        row = rows_data[idx]
        strike = float(row[10]) if row[10] != "" else 0
        if not strike: continue

        pe_vol_val = str(row[13]).replace("%", "").strip() 
        pe_oi_val = str(row[14]).replace("%", "").strip()  

        if "strong" in pe_vol_val.lower(): pe_vol_val = "100"
        if "strong" in pe_oi_val.lower(): pe_oi_val = "100"

        pe_vol_arr.append({'strike': strike, 'val': float(pe_vol_val) if pe_vol_val.replace('.','',1).isdigit() else 0, 'row': r})
        pe_oi_arr.append({'strike': strike, 'val': float(pe_oi_val) if pe_oi_val.replace('.','',1).isdigit() else 0, 'row': r})

    ce_vol_top = extract_top_two(ce_vol_arr)
    ce_oi_top = extract_top_two(ce_oi_arr)
    pe_vol_top = extract_top_two(pe_vol_arr)
    pe_oi_top = extract_top_two(pe_oi_arr)

    ce_raw_state = detect_live_state_advanced(ce_vol_top, False)
    ce_oi_raw_state = detect_live_state_advanced(ce_oi_top, False)
    pe_raw_state = detect_live_state_advanced(pe_vol_top, True)
    pe_oi_raw_state = detect_live_state_advanced(pe_oi_top, True)

    check_ce = "stable strong" if ce_raw_state == "STRONG" else ce_raw_state
    check_pe = "stable strong" if pe_raw_state == "STRONG" else pe_raw_state

    # ==========================================
    # EXACT 27 SCENARIOS WITH DYNAMIC SYMBOL NAME
    # ==========================================
    prefix_name = symbol_key

    if check_ce == "wtt" and check_pe == "wtb":
        k3_text = f"{prefix_name} is 8th scenario both side Risky"
    elif check_ce == "wtb" and check_pe == "wtt":
        k3_text = f"{prefix_name} is 9th scenario both side Risky"
    elif check_ce == "stable strong" and check_pe == "stable strong":
        k3_text = f"{prefix_name} is Neutral"
    elif check_ce == "wtb" and check_pe == "stable strong":
        k3_text = f"{prefix_name} is slightly Bearish"
    elif check_ce == "stable strong" and check_pe == "wtb":
        k3_text = f"{prefix_name} is slightly Bearish"
    elif check_ce == "wtt" and check_pe == "stable strong":
        k3_text = f"{prefix_name} is slightly bullish"
    elif check_ce == "stable strong" and check_pe == "wtt":
        k3_text = f"{prefix_name} is slightly bullish"
    elif check_ce == "wtt" and check_pe == "wtt":
        k3_text = f"{prefix_name} is Bullrun"
    elif check_ce == "wtb" and check_pe == "wtb":
        k3_text = f"{prefix_name} is bloodbath"
    elif check_ce == "wtb" and check_pe == "wtt to strong":
        k3_text = f"{prefix_name} is confirm Bloodbath"
    elif check_ce == "wtt to strong" and check_pe == "wtb":
        k3_text = f"{prefix_name} is confirm Bloodbath"
    elif check_ce == "wtt" and check_pe == "wtt to strong":
        k3_text = f"{prefix_name} is slightly bullish (Both side trade)"
    elif check_ce == "wtt to strong" and check_pe == "wtt":
        k3_text = f"{prefix_name} is slightly bullish (Both side trade)"
    elif check_ce == "wtt" and check_pe == "wtb to strong":
        k3_text = f"{prefix_name} is confirm Bull Run"
    elif check_ce == "wtb to strong" and check_pe == "wtt":
        k3_text = f"{prefix_name} is confirm Bull Run"
    elif check_ce == "wtb to strong" and check_pe == "wtb":
        k3_text = f"{prefix_name} is slightly Bearish (Both side trade)"
    elif check_ce == "wtb" and check_pe == "wtb to strong":
        k3_text = f"{prefix_name} is slightly Bearish (both side trade)"
    elif check_ce == "wtb to strong" and check_pe == "stable strong":
        k3_text = f"{prefix_name} is slightly Bullish"
    elif check_ce == "stable strong" and check_pe == "wtb to strong":
        k3_text = f"{prefix_name} is slightly Bullish"
    elif check_ce == "wtt to strong" and check_pe == "stable strong":
        k3_text = f"{prefix_name} is slightly Bearish"
    elif check_ce == "stable strong" and check_pe == "wtt to strong":
        k3_text = f"{prefix_name} is slightly Bearish"
    elif check_ce == "wtt to strong" and check_pe == "wtb to strong":
        k3_text = f"{prefix_name} is 8th scenario both side Risky trade"
    elif check_ce == "wtb to strong" and check_pe == "wtt to strong":
        k3_text = f"{prefix_name} is 9th scenario both side Risky trade"
    elif check_ce == "wtb to strong" and check_pe == "wtb to strong":
        k3_text = f"{prefix_name} is confirm Bull Run"
    elif check_ce == "wtt to strong" and check_pe == "wtt to strong":
        k3_text = f"{prefix_name} is confirm bloodbath"
    else:
        k3_text = f"{prefix_name} is Neutral"

    return k3_text, ce_vol_top, ce_oi_top, pe_vol_top, pe_oi_top

# ==========================================
# FETCH & UPDATE LOOP WITH MEMORY PRESERVATION
# ==========================================
def fetch_and_update_option_chain(smart_api):
    global morning_oi_memory, baseline_date, cached_selected_symbol, options_cache, eq_cache
    try:
        now_dt = datetime.now()
        current_date_str = now_dt.strftime('%Y-%m-%d')

        if baseline_date != current_date_str:
            morning_oi_memory = {}
            baseline_date = current_date_str

        if not options_cache:
            initialize_scrip_master(smart_api)
            if not options_cache:
                return

        raw_selected_symbol = cached_selected_symbol if cached_selected_symbol else "RELIANCE"
        try:
            val = str(dashboard_sheet.cell(4, 10).value).strip().upper() # J4 cell
            if val and val not in ["NONE", "NAN", "0"]:
                raw_selected_symbol = val
        except Exception:
            pass

        clean_symbol = raw_selected_symbol.replace("-EQ", "").replace(".NS", "").strip()
        if clean_symbol in ["TATAMOTORS", "TATA"]:
            clean_symbol = "TMCV"

        if cached_selected_symbol != clean_symbol:
            print(f"\n⚡ Active Symbol Switched To: {clean_symbol} (Memory Retained from 9:15 AM)")
            cached_selected_symbol = clean_symbol

        spot_token, spot_exchange = eq_cache.get(clean_symbol, eq_cache.get(clean_symbol + "-EQ", ("", "NSE")))
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
        if sym_options.empty:
            return

        today_date = pd.Timestamp(now_dt.date())
        valid_opts = sym_options[sym_options['parsed_expiry'] >= today_date].copy()
        if valid_opts.empty:
            return

        unique_expiries_dt = sorted(valid_opts['parsed_expiry'].unique())
        expiry_strings_sorted = [pd.Timestamp(dt).strftime('%d%b%Y').upper() for dt in unique_expiries_dt]
        active_expiries = (expiry_strings_sorted + ['N/A', 'N/A', 'N/A', 'N/A'])[:4]

        current_expiry = active_expiries[0]
        current_expiry_dt = unique_expiries_dt[0]
        current_expiry_options = valid_opts[valid_opts['parsed_expiry'] == current_expiry_dt].copy()

        lot_size = 1
        if 'lotsize' in current_expiry_options.columns and not current_expiry_options['lotsize'].empty:
            try: lot_size = int(float(current_expiry_options['lotsize'].iloc[0]))
            except: lot_size = 1

        if spot_price <= 0:
            all_strikes_list = sorted(current_expiry_options['strike'].unique())
            spot_price = float(all_strikes_list[len(all_strikes_list) // 2]) if all_strikes_list else 100.0

        all_master_strikes = sorted(current_expiry_options['strike'].unique())
        if all_master_strikes:
            closest_idx = min(range(len(all_master_strikes)), key=lambda i: abs(all_master_strikes[i] - spot_price))
            strikes = all_master_strikes[max(0, closest_idx - 20):min(len(all_master_strikes), closest_idx + 21)]
        else:
            strikes = []

        filtered_opts = current_expiry_options[current_expiry_options['strike'].isin(strikes)].copy()
        exchange_tokens_map = {}
        for _, row in filtered_opts.iterrows():
            exch, tok = str(row['exch_seg']).strip(), str(row['token']).strip()
            exchange_tokens_map.setdefault(exch, []).append(tok)

        all_fetched_quotes = []
        for exch, tokens_list in exchange_tokens_map.items():
            for i in range(0, len(tokens_list), 40):
                batch_tokens = tokens_list[i:i+40]
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
            
            if is_market_open_passed and tok not in morning_oi_memory:
                morning_oi_memory[tok] = oi 

            base_oi = morning_oi_memory.get(tok, oi)
            quotes_dict[tok] = {'ltp': ltp, 'volume': volume, 'oi': oi, 'oi_chg': oi - base_oi, 'net_change': net_change}

        t = max((datetime.strptime(str(current_expiry).title(), "%d%b%Y").replace(hour=15, minute=30) - datetime.now()).total_seconds() / (365.0 * 24 * 3600), 0.0001)
        r = 0.07

        output_rows = []
        for st in strikes:
            ce_sub = current_expiry_options[(current_expiry_options['strike'] == st) & (current_expiry_options['symbol'].astype(str).str.upper().str.endswith('CE'))]
            pe_sub = current_expiry_options[(current_expiry_options['strike'] == st) & (current_expiry_options['symbol'].astype(str).str.upper().str.endswith('PE'))]

            ce_token = str(ce_sub['token'].values[0]).strip() if not ce_sub.empty else ""
            pe_token = str(pe_sub['token'].values[0]).strip() if not pe_sub.empty else ""

            ce_q = quotes_dict.get(ce_token, {'ltp': 0, 'volume': 0, 'oi': 0, 'oi_chg': 0, 'net_change': 0})
            pe_q = quotes_dict.get(pe_token, {'ltp': 0, 'volume': 0, 'oi': 0, 'oi_chg': 0, 'net_change': 0})

            ce_gks = calculate_iv_and_greeks('CE', spot_price, st, t, r, ce_q['ltp'])
            pe_gks = calculate_iv_and_greeks('PE', spot_price, st, t, r, pe_q['ltp'])

            output_rows.append([
                ce_gks['vega'], ce_gks['theta'], ce_gks['gamma'], ce_gks['delta'], ce_gks['iv'], ce_q['oi_chg'], ce_q['oi'], ce_q['volume'], ce_q['net_change'], ce_q['ltp'],
                st,
                pe_q['ltp'], pe_q['net_change'], pe_q['volume'], pe_q['oi'], pe_q['oi_chg'], pe_gks['iv'], pe_gks['delta'], pe_gks['gamma'], pe_gks['theta'], pe_gks['vega']
            ])

        if len(output_rows) > 0:
            k3_text, _, _, _, _ = compute_python_scenarios(output_rows, clean_symbol)

            data_sheet.update(range_name="K9", values=[[spot_price]])
            data_sheet.update(range_name="L9", values=[[str(current_expiry)]])
            data_sheet.update(range_name="A7", values=[[lot_size]])
            
            last_row_num = 9 + len(output_rows)
            dashboard_sheet.update(range_name=f"A10:U{last_row_num}", values=output_rows)
            dashboard_sheet.update(range_name="K3", value=[[k3_text]])
            print(f"⚡ Successfully Refreshed & Synced for {clean_symbol} | Scenario: {k3_text}")

    except Exception as ex:
        print(f"❌ Exception in fetch loop: {str(ex)}")

# ==========================================
# MAIN THREADS
# ==========================================
def run_bot_loop():
    print("🚀 Starting Advanced Memory-Synced Bot Thread with Dynamic 27 Scenarios...")
    smart_api = SmartConnect(api_key=API_KEY)
    
    while True:
        try:
            totp = pyotp.TOTP(TOTP_SECRET.replace(" ", "")).now() if TOTP_SECRET else "123456"
            data = smart_api.generateSession(CLIENT_ID, PIN, totp)
            if data and data.get('status'):
                print("✅ Angel One Session Generated!")
                initialize_scrip_master(smart_api)
                break
            time.sleep(5)
        except Exception as e:
            print(f"❌ Session Exception: {e}")
            time.sleep(5)

    while True:
        try:
            fetch_and_update_option_chain(smart_api)
        except Exception as _e:
            print(f"❌ Loop Error: {_e}")
        time.sleep(1)

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    run_bot_loop()
