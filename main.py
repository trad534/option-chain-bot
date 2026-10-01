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
    return "Option Chain Bot is active and running!"

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
INDEX_SHEET_NAME = "Option chain index new"  # J4 symbol selection ke liye

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
    index_sheet = spreadsheet.worksheet(INDEX_SHEET_NAME)
    print("✅ SUCCESS: Google Sheets Connected Successfully!")
except Exception as e:
    print(f"❌ Google Sheet Connection Error: {e}")
    exit()

# ==========================================
# ROBUST IV & GREEKS (WITHOUT % SIGN)
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
# SCRIP MASTER LOADER VIA OFFICIAL JSON
# ==========================================
full_scrip_df = pd.DataFrame()
morning_oi_dict = {}
baseline_date = None

def initialize_scrip_master(smart_api):
    global full_scrip_df
    try:
        print("📥 Downloading Angel One Scrip Master JSON...")
        url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
        resp = requests.get(url, timeout=20)
        if resp.status_code == 200:
            scrip_data = resp.json()
            full_scrip_df = pd.DataFrame(scrip_data)
            
            if 'symboltoken' in full_scrip_df.columns and 'token' not in full_scrip_df.columns:
                full_scrip_df['token'] = full_scrip_df['symboltoken']
            if 'tradingsymbol' in full_scrip_df.columns and 'symbol' not in full_scrip_df.columns:
                full_scrip_df['symbol'] = full_scrip_df['tradingsymbol']
            if 'strike' in full_scrip_df.columns:
                full_scrip_df['strike'] = pd.to_numeric(full_scrip_df['strike'], errors='coerce').fillna(0)
                # Kuch segments me strike price 100 se multiplied hoti hai, use adjust karein agar zaroori ho
                # full_scrip_df.loc[full_scrip_df['strike'] > 500000, 'strike'] = full_scrip_df['strike'] / 100
                
            print(f"✅ Scrip Master Loaded Successfully! Total Records: {len(full_scrip_df)}")
    except Exception as e:
        print(f"⚠ Scrip Master Download Error: {e}")

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
        data_sheet.update(range_name="A2", values=[[f"Render is working - {current_time_str}"]])

        if baseline_date != current_date_str:
            morning_oi_dict = {}
            baseline_date = current_date_str

        if full_scrip_df is None or full_scrip_df.empty:
            initialize_scrip_master(smart_api)
            if full_scrip_df.empty:
                print("⚠ Scrip master empty, retrying next loop...")
                return

        # 1. Read Selected Symbol from J4 in 'Option chain index new'
        selected_symbol = str(index_sheet.acell('J4').value).strip().upper()
        if not selected_symbol or selected_symbol == "NONE":
            print("⚠ No valid symbol found in J4. Waiting...")
            return

        print(f"🎯 Selected Symbol from J4: {selected_symbol}")

        # 2. Filter Symbol data from Master (Index/Stock/MCX options)
        # Name match ya symbol match karenge
        sym_options = full_scrip_df[
            (full_scrip_df['exch_seg'].isin(['NFO', 'MCX', 'BFO'])) & 
            ((full_scrip_df['name'] == selected_symbol) | (full_scrip_df['symbol'].str.contains(selected_symbol, case=False, na=False))) &
            (full_scrip_df['instrumenttype'].isin(['OPTIDX', 'OPTSTK', 'OPTCUR']))
        ].copy()

        if sym_options.empty:
            print(f"⚠ No options found in master for symbol: {selected_symbol}")
            return

        # Parse expiry dates
        sym_options['parsed_expiry'] = pd.to_datetime(sym_options['expiry'], format='%d%b%Y', errors='coerce')
        sym_options = sym_options.dropna(subset=['parsed_expiry'])

        if sym_options.empty:
            print(f"⚠ No valid expiry dates found for {selected_symbol}")
            return

        # Sort unique expiries chronologically
        unique_expiries_dt = sorted(sym_options['parsed_expiry'].unique())
        expiry_strings_sorted = [pd.Timestamp(dt).strftime('%d%b%Y').upper() for dt in unique_expiries_dt]

        # Get top 4 expiries for A3 to A6
        active_expiries = expiry_strings_sorted[:4]
        while len(active_expiries) < 4:
            active_expiries.append(active_expiries[-1] if active_expiries else "N/A")

        current_expiry = active_expiries[0]

        # Filter options for the Current (Nearest) Expiry
        current_expiry_dt = unique_expiries_dt[0]
        current_expiry_options = sym_options[sym_options['parsed_expiry'] == current_expiry_dt].copy()

        # 3. Get Lot Size (A7)
        lot_size = 1
        if 'lotsize' in current_expiry_options.columns and not current_expiry_options['lotsize'].empty:
            try:
                lot_size = int(float(current_expiry_options['lotsize'].iloc[0]))
            except:
                lot_size = 1

        # 4. Find Spot / Underlying Token & Price
        spot_token = ""
        spot_exchange = "NSE"
        # Spot token dhundne ke liye index/equity segment check karein
        spot_match = full_scrip_df[
            (full_scrip_df['symbol'].str.contains(selected_symbol, case=False, na=False)) & 
            (full_scrip_df['instrumenttype'] == 'AMXIDX') | (full_scrip_df['exch_seg'] == 'NSE') & (full_scrip_df['symbol'] == selected_symbol + '-EQ')
        ]
        
        # Fallback agar specific spot na mile toh Nifty default ya symbol base token lenge
        if not spot_match.empty:
            spot_token = str(spot_match['token'].iloc[0])
            spot_exchange = str(spot_match['exch_seg'].iloc[0])
        else:
            # Common indices mapping fallback
            spot_map = {"NIFTY": ("26000", "NSE"), "BANKNIFTY": ("26009", "NSE"), "SENSEX": ("1", "BSE")}
            if selected_symbol in spot_map:
                spot_token, spot_exchange = spot_map[selected_symbol]
            else:
                spot_token = str(current_expiry_options['token'].iloc[0])
                spot_exchange = str(current_expiry_options['exch_seg'].iloc[0])

        # Fetch Spot Price
        spot_quote = smart_api.getMarketData("FULL", {spot_exchange: [spot_token]})
        spot_price = 0.0
        if spot_quote and spot_quote.get('status') and spot_quote.get('data') and spot_quote['data'].get('fetched'):
            item = spot_quote['data']['fetched'][0]
            spot_price = float(item.get('ltp', item.get('LTP', item.get('close', 0))))
        
        if spot_price <= 0:
            spot_price = float(current_expiry_options['strike'].median()) # Fallback median strike

        print(f"🎯 Symbol: {selected_symbol} | Spot: {spot_price} | Current Expiry: {current_expiry} | Lot Size: {lot_size}")

        # 5. Update Sheet Mappings (`upstox get data`)
        try:
            data_sheet.update(range_name="J9", values=[[selected_symbol]])
            data_sheet.update(range_name="K9", values=[[spot_price]])
            data_sheet.update(range_name="L9", values=[[str(current_expiry)]])
            data_sheet.update(range_name="A7", values=[[lot_size]])
            
            # Expiries A3 to A6 update
            expiry_cells = [
                ['A3', active_expiries[0]], 
                ['A4', active_expiries[1]], 
                ['A5', active_expiries[2]], 
                ['A6', active_expiries[3]]
            ]
            data_sheet.batch_update([{'range': cell, 'values': [[val]]} for cell, val in expiry_cells])
        except Exception as sheet_err:
            print(f"⚠ Error updating header cells (J9, K9, L9, A3-A7): {sheet_err}")

        # 6. Filter option chain strikes around Spot Price (e.g., ± 20 strikes)
        strike_step = 50
        if len(current_expiry_options['strike'].unique()) > 1:
            sorted_strikes = sorted(current_expiry_options['strike'].unique())
            if len(sorted_strikes) > 1:
                strike_step = abs(sorted_strikes[1] - sorted_strikes[0])

        base_strike = round(spot_price / strike_step) * strike_step
        range_span = strike_step * 20  # 20 strikes above and below

        filtered_opts = current_expiry_options[
            (current_expiry_options['strike'] >= base_strike - range_span) & 
            (current_expiry_options['strike'] <= base_strike + range_span)
        ].copy()

        tokens_nfo = [str(t).strip() for t in filtered_opts['token'].unique().tolist()]

        # Fetch Market Data for Options in Batches
        all_fetched_quotes = []
        for i in range(0, len(tokens_nfo), 40):
            batch_tokens = tokens_nfo[i:i+40]
            if not batch_tokens:
                continue
            try:
                res = smart_api.getMarketData("FULL", {"NFO": batch_tokens})
                if res and res.get('status') and res.get('data'):
                    fetched_list = res['data'].get('fetched', [])
                    all_fetched_quotes.extend(fetched_list)
            except Exception as batch_err:
                print(f"⚠ Batch fetch error: {batch_err}")

        market_open_time = datetime.strptime("09:15:00", "%H:%M:%S").time()
        is_market_open_passed = now_dt.time() >= market_open_time

        quotes_data = []
        for item in all_fetched_quotes:
            tok = str(item.get('symbolToken', item.get('symboltoken', item.get('token', '')))).strip()
            ltp = float(item.get('ltp', item.get('LTP', item.get('close', 0))))
            volume = float(item.get('tradeVolume', item.get('volume', 0)))
            oi = float(item.get('opnInterest', item.get('oi', 0)))
            net_change = float(item.get('netChange', item.get('change', 0)))
            
            if is_market_open_passed:
                if tok not in morning_oi_dict:
                    morning_oi_dict[tok] = oi

            base_oi = morning_oi_dict.get(tok, oi)
            oi_chg = oi - base_oi

            quotes_data.append({
                'token': tok, 
                'ltp': ltp, 
                'volume': volume, 
                'oi': oi, 
                'oi_chg': oi_chg,
                'net_change': net_change
            })

        quotes_df = pd.DataFrame(quotes_data)

        if not quotes_df.empty and not filtered_opts.empty:
            filtered_opts['token'] = filtered_opts['token'].astype(str).str.strip()
            quotes_df['token'] = quotes_df['token'].astype(str).str.strip()
            merged = pd.merge(filtered_opts[['token', 'symbol', 'strike']], quotes_df, on='token', how='inner')
        else:
            merged = pd.DataFrame()

        try:
            exp_dt = datetime.strptime(str(current_expiry).title(), "%d%b%Y")
            exp_date = exp_dt.replace(hour=15, minute=30, second=0)
        except:
            exp_date = datetime.now() + timedelta(days=5)

        t = max((exp_date - datetime.now()).total_seconds() / (365.0 * 24 * 3600), 0.0001)
        r = 0.07

        strikes = sorted(merged['strike'].unique()) if not merged.empty else [base_strike]
        
        output_rows = [
            [
                "CE Vega", "CE Theta", "CE Gamma", "CE Delta", "CE IV", "CE OI Chg", "CE OI", "CE Vol", "CE Change", "CE LTP",
                "Strike Price",
                "PE LTP", "PE Change", "PE Vol", "PE OI", "PE OI Chg", "PE IV", "PE Delta", "PE Gamma", "PE Theta", "PE Vega"
            ]
        ]

        for st in strikes:
            ce_row = merged[(merged['strike'] == st) & (merged['symbol'].str.endswith('CE'))] if not merged.empty else pd.DataFrame()     
            pe_row = merged[(merged['strike'] == st) & (merged['symbol'].str.endswith('PE'))] if not merged.empty else pd.DataFrame()     

            ce_ltp = float(ce_row['ltp'].values[0]) if not ce_row.empty and 'ltp' in ce_row.columns else 0.0
            ce_vol = float(ce_row['volume'].values[0]) if not ce_row.empty and 'volume' in ce_row.columns else 0.0
            ce_oi = float(ce_row['oi'].values[0]) if not ce_row.empty and 'oi' in ce_row.columns else 0.0
            ce_oi_chg = float(ce_row['oi_chg'].values[0]) if not ce_row.empty and 'oi_chg' in ce_row.columns else 0.0
            ce_change = float(ce_row['net_change'].values[0]) if not ce_row.empty and 'net_change' in ce_row.columns else 0.0

            pe_ltp = float(pe_row['ltp'].values[0]) if not pe_row.empty and 'ltp' in pe_row.columns else 0.0
            pe_vol = float(pe_row['volume'].values[0]) if not pe_row.empty and 'volume' in pe_row.columns else 0.0
            pe_oi = float(pe_row['oi'].values[0]) if not pe_row.empty and 'oi' in pe_row.columns else 0.0
            pe_oi_chg = float(pe_row['oi_chg'].values[0]) if not pe_row.empty and 'oi_chg' in pe_row.columns else 0.0
            pe_change = float(pe_row['net_change'].values[0]) if not pe_row.empty and 'net_change' in pe_row.columns else 0.0

            ce_gks = calculate_iv_and_greeks('CE', spot_price, st, t, r, ce_ltp)
            pe_gks = calculate_iv_and_greeks('PE', spot_price, st, t, r, pe_ltp)

            ce_iv_val = ce_gks['iv'] if ce_gks['iv'] > 1.0 else 0
            pe_iv_val = pe_gks['iv'] if pe_gks['iv'] > 1.0 else 0

            output_rows.append([
                ce_gks['vega'], ce_gks['theta'], ce_gks['gamma'], ce_gks['delta'], ce_iv_val, ce_oi_chg, ce_oi, ce_vol, ce_change, ce_ltp,
                st,
                pe_ltp, pe_change, pe_vol, pe_oi, pe_oi_chg, pe_iv_val, pe_gks['delta'], pe_gks['gamma'], pe_gks['theta'], pe_gks['vega']
            ])

        if len(output_rows) > 1:
            data_sheet.update(range_name=f"A10:U{10 + len(output_rows) - 1}", values=output_rows)
            print(f"🎯 Option Chain Table Updated Successfully for {selected_symbol}!")

    except Exception as ex:
        print(f"❌ Exception in fetch loop: {str(ex)}")

# ==========================================
# MAIN EXECUTION THREADS
# ==========================================
def run_bot_loop():
    print("🚀 Starting Bot Loop Thread...")
    
    try:
        smart_api = SmartConnect(api_key=API_KEY)
    except Exception as e:
        print(f"❌ SmartConnect Init Error: {e}")
        return
        
    while True:
        try:
            print("🔄 Attempting to generate session...")
            totp = pyotp.TOTP(TOTP_SECRET.replace(" ", "")).now() if TOTP_SECRET else "123456"
            data = smart_api.generateSession(CLIENT_ID, PIN, totp)
            if data and data.get('status'):
                print("✅ Angel One Session Generated Successfully!")
                initialize_scrip_master(smart_api)
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
