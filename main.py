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
    return "Angel One Option Chain Bot is active and running!"

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
        if flag == 'CE':
            delta = 1.0 if S > K else 0.0
        else:
            delta = -1.0 if S < K else 0.0
        return {'iv': 0, 'delta': round(delta, 3), 'gamma': 0, 'theta': 0, 'vega': 0}

    if flag == 'CE':
        intrinsic = max(0, S - K * np.exp(-r * t))
    else:
        intrinsic = max(0, K * np.exp(-r * t) - S)
        
    if ltp < intrinsic:
        ltp = intrinsic + 0.05

    def objective_function(sigma):
        if sigma <= 0:
            sigma = 1e-4
        d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * t) / (sigma * np.sqrt(t))
        d2 = d1 - sigma * np.sqrt(t)
        if flag == 'CE':
            price = S * norm.cdf(d1) - K * np.exp(-r * t) * norm.cdf(d2)
        else:
            price = K * np.exp(-r * t) * norm.cdf(-d2) - S * norm.cdf(-d1)
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
# SCRIP MASTER LOADER (ANGEL ONE)
# ==========================================
full_scrip_df = pd.DataFrame()
morning_oi_dict = {}
baseline_date = None

def initialize_scrip_master(smart_api):
    global full_scrip_df
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
                
            print(f"✅ Angel One Scrip Master Loaded! Total Records: {len(full_scrip_df)}")
    except Exception as e:
        print(f"⚠ Scrip Master Download Error: {e}")

def update_watchlist_once():
    try:
        indices_list = ['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'SENSEX']
        mcx_list = ['CRUDEOIL', 'NATURALGAS', 'GOLD', 'SILVER', 'COPPER', 'ZINC', 'ALUMINI', 'LEAD']
        nifty50_stocks = [
            'RELIANCE-EQ', 'TCS-EQ', 'INFY-EQ', 'HDFCBANK-EQ', 'ICICIBANK-EQ', 
            'SBIN-EQ', 'BHARTIARTL-EQ', 'KOTAKBANK-EQ', 'LT-EQ', 'AXISBANK-EQ', 
            'TATAMOTORS-EQ', 'TATASTEEL-EQ', 'ITC-EQ', 'SUNPHARMA-EQ', 'BAJFINANCE-EQ'
        ]
        combined_symbols = indices_list + mcx_list + nifty50_stocks
        watchlist_output = [[sym] for sym in combined_symbols]
        if watchlist_output:
            data_sheet.batch_clear(["Z10:Z250"])
            data_sheet.update(range_name=f"Z10:Z{9 + len(watchlist_output)}", values=watchlist_output)
            print("✅ Watchlist Successfully Written to Column Z!")
    except Exception as wl_err:
        print(f"⚠ Watchlist update error: {wl_err}")

# ==========================================
# FETCH & UPDATE LOOP
# ==========================================
def fetch_and_update_option_chain(smart_api):
    global full_scrip_df, morning_oi_dict, baseline_date
    try:
        now_dt = datetime.now()
        current_time_str = now_dt.strftime('%H:%M:%S')
        current_date_str = now_dt.strftime('%Y-%m-%d')
        
        print(f"\n🔄 Loop executed at: {current_time_str}")
        data_sheet.update(range_name="A2", values=[[f"Angel One Bot Working - {current_time_str}"]])

        if baseline_date != current_date_str:
            morning_oi_dict = {}
            baseline_date = current_date_str

        if full_scrip_df is None or full_scrip_df.empty:
            initialize_scrip_master(smart_api)
            if full_scrip_df.empty:
                print("⚠ Scrip master empty, retrying next loop...")
                return

        raw_selected_symbol = "NIFTY"
        try:
            val = str(dashboard_sheet.cell(4, 10).value).strip().upper() # Cell J4
            if val and val != "NONE" and val != "NAN" and val != "0":
                raw_selected_symbol = val
        except Exception as e:
            print(f"⚠ Error reading J4 symbol from sheet: {e}")

        watchlist_symbol = raw_selected_symbol
        clean_symbol = raw_selected_symbol.replace("-EQ", "").replace(".NS", "").strip()
        print(f"🎯 Active Selected Symbol strictly from J4: {watchlist_symbol} (Clean: {clean_symbol})")

        # Bulletproof Fuzzy Matching for Options (Fixed variable declaration)
        search_keyword = clean_symbol
        if "TATA" in clean_symbol:
            search_keyword = "TATA"

        sym_options = full_scrip_df[
            (full_scrip_df['strike'] > 0) &
            (
                (full_scrip_df['name'].astype(str).str.upper().str.contains(clean_symbol)) |
                (full_scrip_df['symbol'].astype(str).str.upper().str.contains(clean_symbol)) |
                (full_scrip_df['symbol'].astype(str).str.upper().str.contains(search_keyword))
            )
        ].copy()

        if "TATA" in clean_symbol and not sym_options.empty:
            specific_filtered = sym_options[sym_options['symbol'].astype(str).str.upper().str.startswith(clean_symbol)]
            if not specific_filtered.empty:
                sym_options = specific_filtered

        if sym_options.empty:
            print(f"❌ Critical: No options found for {clean_symbol}!")
            return

        sym_options['strike'] = pd.to_numeric(sym_options['strike'], errors='coerce')
        sym_options = sym_options[sym_options['strike'] > 0]

        sym_options['parsed_expiry'] = pd.to_datetime(sym_options['expiry'], format='%d%b%Y', errors='coerce')
        if sym_options['parsed_expiry'].isna().all():
            sym_options['parsed_expiry'] = pd.to_datetime(sym_options['expiry'], errors='coerce')
            
        sym_options = sym_options.dropna(subset=['parsed_expiry'])
        today_date = pd.Timestamp(now_dt.date())
        sym_options = sym_options[sym_options['parsed_expiry'] >= today_date]

        if sym_options.empty:
            print(f"⚠ No valid expiry dates found for {clean_symbol}")
            return

        unique_expiries_dt = sorted(sym_options['parsed_expiry'].unique())
        expiry_strings_sorted = [pd.Timestamp(dt).strftime('%d%b%Y').upper() for dt in unique_expiries_dt]

        active_expiries = expiry_strings_sorted[:4]
        while len(active_expiries) < 4:
            active_expiries.append(active_expiries[-1] if active_expiries else "N/A")

        current_expiry = active_expiries[0]
        current_expiry_dt = unique_expiries_dt[0]
        current_expiry_options = sym_options[sym_options['parsed_expiry'] == current_expiry_dt].copy()

        lot_size = 1
        if 'lotsize' in current_expiry_options.columns and not current_expiry_options['lotsize'].empty:
            try:
                lot_size = int(float(current_expiry_options['lotsize'].iloc[0]))
            except:
                lot_size = 1

        spot_token = ""
        spot_exchange = "NSE"
        
        spot_map = {
            "NIFTY": ("26000", "NSE"), 
            "BANKNIFTY": ("26009", "NSE"), 
            "FINNIFTY": ("26037", "NSE"), 
            "MIDCPNIFTY": ("26074", "NSE"),
            "SENSEX": ("999907000", "BSE")
        }
        
        if clean_symbol in spot_map:
            spot_token, spot_exchange = spot_map[clean_symbol]
        else:
            eq_match = full_scrip_df[
                (full_scrip_df['exch_seg'] == 'NSE') & 
                (
                    (full_scrip_df['symbol'].astype(str).str.upper() == clean_symbol) | 
                    (full_scrip_df['symbol'].astype(str).str.upper() == clean_symbol + "-EQ") |
                    (full_scrip_df['name'].astype(str).str.upper() == clean_symbol)
                ) &
                (~full_scrip_df['instrumenttype'].isin(['OPTIDX', 'OPTSTK', 'OPTCUR', 'FUTCOM', 'OPTCOM', 'FUTSTK', 'FUTIDX']))
            ]
            
            if not eq_match.empty:
                spot_token = str(eq_match.iloc[0]['token']).strip()
                spot_exchange = "NSE"
            else:
                spot_token = ""

        spot_price = 0.0
        if spot_token:
            try:
                spot_quote = smart_api.getMarketData("FULL", {spot_exchange: [spot_token]})
                if spot_quote and spot_quote.get('status') and spot_quote.get('data') and spot_quote['data'].get('fetched'):
                    item = spot_quote['data']['fetched'][0]
                    spot_price = float(item.get('ltp', item.get('LTP', item.get('close', 0))))
            except Exception as e:
                print(f"⚠ Spot fetch warning: {e}")
        
        if spot_price <= 0:
            all_strikes_list = sorted(current_expiry_options['strike'].unique())
            if all_strikes_list:
                spot_price = float(all_strikes_list[len(all_strikes_list) // 2])
            else:
                spot_price = 100.0

        print(f"🎯 Symbol: {clean_symbol} | Spot Token: {spot_token} | Spot Price: {spot_price}")

        try:
            data_sheet.update(range_name="K9", values=[[spot_price]])
            data_sheet.update(range_name="L9", values=[[str(current_expiry)]])
            data_sheet.update(range_name="A7", values=[[lot_size]])
            
            expiry_cells = [
                ['A3', active_expiries[0]], ['A4', active_expiries[1]], 
                ['A5', active_expiries[2]], ['A6', active_expiries[3]]
            ]
            data_sheet.batch_update([{'range': cell, 'values': [[val]]} for cell, val in expiry_cells])
        except Exception as sheet_err:
            print(f"⚠ Error updating header cells: {sheet_err}")

        all_master_strikes = sorted(current_expiry_options['strike'].unique())

        if len(all_master_strikes) > 0:
            closest_idx = min(range(len(all_master_strikes)), key=lambda i: abs(all_master_strikes[i] - spot_price))
            start_idx = max(0, closest_idx - 20)
            end_idx = min(len(all_master_strikes), closest_idx + 21)
            strikes = all_master_strikes[start_idx:end_idx]
            
            if len(all_master_strikes) < 41:
                strikes = all_master_strikes
            else:
                if len(strikes) < 41:
                    if start_idx == 0:
                        end_idx = min(len(all_master_strikes), 41)
                        strikes = all_master_strikes[:end_idx]
                    elif end_idx == len(all_master_strikes):
                        start_idx = max(0, len(all_master_strikes) - 41)
                        strikes = all_master_strikes[start_idx:]
        else:
            strikes = []

        filtered_opts = current_expiry_options[current_expiry_options['strike'].isin(strikes)].copy()
        
        exchange_tokens_map = {}
        for _, row in filtered_opts.iterrows():
            exch = str(row['exch_seg']).strip()
            tok = str(row['token']).strip()
            if exch not in exchange_tokens_map:
                exchange_tokens_map[exch] = []
            if tok not in exchange_tokens_map[exch]:
                exchange_tokens_map[exch].append(tok)

        all_fetched_quotes = []
        for exch, tokens_list in exchange_tokens_map.items():
            for i in range(0, len(tokens_list), 40):
                batch_tokens = tokens_list[i:i+40]
                if not batch_tokens:
                    continue
                try:
                    res = smart_api.getMarketData("FULL", {exch: batch_tokens})
                    if res and res.get('status') and res.get('data'):
                        all_fetched_quotes.extend(res['data'].get('fetched', []))
                except Exception as batch_err:
                    print(f"⚠ Batch fetch error for {exch}: {batch_err}")

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
            quotes_dict[tok] = {
                'ltp': ltp, 'volume': volume, 'oi': oi, 'oi_chg': oi - base_oi, 'net_change': net_change
            }

        try:
            exp_dt = datetime.strptime(str(current_expiry).title(), "%d%b%Y")
            exp_date = exp_dt.replace(hour=15, minute=30, second=0)
        except:
            exp_date = datetime.now() + timedelta(days=5)

        t = max((exp_date - datetime.now()).total_seconds() / (365.0 * 24 * 3600), 0.0001)
        r = 0.07

        output_rows = [[
            "CE Vega", "CE Theta", "CE Gamma", "CE Delta", "CE IV", "CE OI Chg", "CE OI", "CE Vol", "CE Change", "CE LTP",
            "Strike Price",
            "PE LTP", "PE Change", "PE Vol", "PE OI", "PE OI Chg", "PE IV", "PE Delta", "PE Gamma", "PE Theta", "PE Vega"
        ]]

        for st in strikes:
            ce_sub = current_expiry_options[
                (current_expiry_options['strike'] == st) & 
                (current_expiry_options['symbol'].astype(str).str.upper().str.endswith('CE'))
            ]
            pe_sub = current_expiry_options[
                (current_expiry_options['strike'] == st) & 
                (current_expiry_options['symbol'].astype(str).str.upper().str.endswith('PE'))
            ]

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
            data_sheet.batch_clear([f"A{last_row_num + 1}:U100"])
            print(f"🎯 Option Chain Table Updated Successfully for {clean_symbol} with {len(strikes)} strikes!")
        else:
            print("⚠ Warning: output_rows has only header, no strike rows generated!")

    except Exception as ex:
        print(f"❌ Exception in fetch loop: {str(ex)}")

# ==========================================
# MAIN EXECUTION THREADS
# ==========================================
def run_bot_loop():
    print("🚀 Starting Angel One Bot Loop Thread...")
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
                print("✅ Angel One Session Generated Successfully!")
                initialize_scrip_master(smart_api)
                update_watchlist_once()
                break
            else:
                print(f"⚠ Session Failed Response: {data}")
                time.sleep(10)
        except Exception as e:
            print(f"❌ Session Generation Exception: {e}")
            time.sleep(10)

    while True:
        try:
            fetch_and_update_option_chain(smart_api)
        except Exception as e:
            print(f"❌ Loop Error: {e}")
        time.sleep(5)

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    run_bot_loop()
