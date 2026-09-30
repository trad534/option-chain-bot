import time
import os
import json
import urllib.request
import pandas as pd
import numpy as np
import pyotp
import gspread
from scipy.stats import norm
from datetime import datetime
from SmartApi import SmartConnect
from flask import Flask
import threading

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
# CONFIGURATION & GOOGLE SHEET
# ==========================================
CLIENT_ID = os.getenv("ANGEL_CLIENT_ID")
PIN = os.getenv("ANGEL_PIN")
API_KEY = os.getenv("ANGEL_API_KEY")
TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET")

GOOGLE_SHEET_NAME = "6 LTP CALCULATOR"
MAIN_SHEET_NAME = "upstox get data"

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
    print("✅ SUCCESS: Google Sheet Connected Successfully!")
except Exception as e:
    print(f"❌ Google Sheet Connection Error: {e}")
    exit()

# ==========================================
# BLACK-SCHOLES GREEKS & IV CALCULATOR
# ==========================================
def calculate_iv_and_greeks(flag, S, K, t, r, ltp):
    if t <= 0 or S <= 0 or K <= 0 or ltp <= 0:
        return {'iv': 0, 'delta': 0, 'gamma': 0, 'theta': 0, 'vega': 0}

    low_sigma, high_sigma = 0.01, 3.0
    sigma = 0.20
    for _ in range(15):
        mid_sigma = (low_sigma + high_sigma) / 2
        d1 = (np.log(S / K) + (r + 0.5 * mid_sigma ** 2) * t) / (mid_sigma * np.sqrt(t))
        d2 = d1 - mid_sigma * np.sqrt(t)
        
        price = S * norm.cdf(d1) - K * np.exp(-r * t) * norm.cdf(d2) if flag == 'CE' else K * np.exp(-r * t) * norm.cdf(-d2) - S * norm.cdf(-d1)
        
        if abs(price - ltp) < 0.1:
            sigma = mid_sigma
            break
        if price > ltp:
            high_sigma = mid_sigma
        else:
            low_sigma = mid_sigma
    else:
        sigma = mid_sigma

    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * t) / (sigma * np.sqrt(t))
    d2 = d1 - sigma * np.sqrt(t)
    
    gamma = norm.pdf(d1) / (S * sigma * np.sqrt(t))
    vega = S * norm.pdf(d1) * np.sqrt(t) / 100

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
        'vega': round(vega, 2)
    }

# ==========================================
# SCRIP MASTER LOADER
# ==========================================
cached_options_df = pd.DataFrame()
earliest_expiry = ""
nifty_spot_token = "26000"

def initialize_scrip_master():
    global cached_options_df, earliest_expiry
    try:
        print("📥 Downloading Nifty Scrip Master...")
        url = "https://margincalculator.angelbroking.com/open-api/tokenMaster/OpenAPIScripMaster.json"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=30) as response:
            scrip_data = json.loads(response.read().decode())
        
        df = pd.DataFrame(scrip_data)
        if df.empty:
            return

        nifty_opts = df[(df['exch_seg'] == 'NFO') & (df['name'].str.upper() == 'NIFTY') & (df['instrumenttype'] == 'OPTIDX')].copy()
        if not nifty_opts.empty:
            nifty_opts['strike'] = nifty_opts['strike'].astype(float) / 100
            nifty_opts['parsed_expiry'] = pd.to_datetime(nifty_opts['expiry'], format='%d%b%Y', errors='coerce')
            valid_opts = nifty_opts.dropna(subset=['parsed_expiry'])
            
            if not valid_opts.empty:
                earliest_expiry = valid_opts['parsed_expiry'].min().strftime('%d%b%Y').upper()
                cached_options_df = nifty_opts[nifty_opts['expiry'] == earliest_expiry].copy()

        print(f"✅ Scrip Master Loaded! Expiry: {earliest_expiry}, Total Options: {len(cached_options_df)}")
    except Exception as e:
        print(f"⚠️ Scrip Master Error: {e}")

# ==========================================
# FETCH & UPDATE LOOP
# ==========================================
def fetch_and_update_option_chain(smart_api):
    global cached_options_df
    try:
        current_time_str = datetime.now().strftime('%H:%M:%S')
        print(f"\n🔄 Loop executed at: {current_time_str}")
        data_sheet.update(range_name="A2", values=[[f"Render is working - {current_time_str}"]])

        if cached_options_df is None or cached_options_df.empty:
            initialize_scrip_master()

        # 1. Fetch Spot Price
        spot_quote = smart_api.getMarketData("FULL", {"NSE": [nifty_spot_token]})
        spot_price = 0.0
        
        if spot_quote and spot_quote.get('status') and spot_quote.get('data') and spot_quote['data'].get('fetched'):
            item = spot_quote['data']['fetched'][0]
            spot_price = float(item.get('ltp', item.get('LTP', item.get('close', 0))))
            if spot_price <= 0:
                spot_price = float(item.get('close', 22620.45))

        print(f"🎯 Spot Price: {spot_price}")

        atm_strike = round(spot_price / 50) * 50

        if cached_options_df is not None and not cached_options_df.empty:
            filtered_opts = cached_options_df[(cached_options_df['strike'] >= atm_strike - 200) & (cached_options_df['strike'] <= atm_strike + 200)].copy()
            tokens_nfo = [str(t) for t in filtered_opts['token'].unique().tolist()]
        else:
            tokens_nfo = []
            filtered_opts = pd.DataFrame()

        # 2. Fetch NFO Option Quotes from Angel API
        all_fetched_quotes = []
        for i in range(0, len(tokens_nfo), 50):
            batch_tokens = tokens_nfo[i:i+50]
            quotes = smart_api.getMarketData("FULL", {"NFO": batch_tokens})
            if quotes and quotes.get('status') and quotes.get('data') and quotes['data'].get('fetched'):
                all_fetched_quotes.extend(quotes['data']['fetched'])

        quotes_data = []
        for item in all_fetched_quotes:
            tok = str(item.get('symbolToken', item.get('symboltoken', item.get('token', ''))))
            print("API Item Keys & Values:", item)
            
            val = float(item.get('ltp', item.get('LTP', item.get('close', item.get('netPrice', item.get('fiveDayChange', 0))))))
            quotes_data.append({'token': tok, 'ltp': val})

        quotes_df = pd.DataFrame(quotes_data)
        if not quotes_df.empty and not filtered_opts.empty:
            filtered_opts['token'] = filtered_opts['token'].astype(str)
            quotes_df['token'] = quotes_df['token'].astype(str)
            merged = pd.merge(filtered_opts[['token', 'symbol', 'strike']], quotes_df[['token', 'ltp']], on='token', how='inner')
        else:
            merged = pd.DataFrame()

        try:
            exp_date = datetime.strptime(str(earliest_expiry).title(), "%d%b%Y")
        except:
            exp_date = datetime.now()

        t = max((exp_date - datetime.now()).days / 365.0, 0.001)
        r = 0.07

        strikes = sorted(merged['strike'].unique()) if not merged.empty else range(int(atm_strike - 200), int(atm_strike + 250), 50)
        output_rows = [
            ["CE Delta", "CE Gamma", "CE Theta", "CE Vega", "CE IV", "CE LTP", "Strike Price", "PE LTP", "PE IV", "PE Delta", "PE Gamma", "PE Theta", "PE Vega"]
        ]

        for st in strikes:
            ce_row = merged[(merged['strike'] == st) & (merged['symbol'].str.endswith('CE'))] if not merged.empty else pd.DataFrame()
            pe_row = merged[(merged['strike'] == st) & (merged['symbol'].str.endswith('PE'))] if not merged.empty else pd.DataFrame()

            ce_ltp = float(ce_row['ltp'].values[0]) if not ce_row.empty and 'ltp' in ce_row.columns else 0.0
            pe_ltp = float(pe_row['ltp'].values[0]) if not pe_row.empty and 'ltp' in pe_row.columns else 0.0

            ce_gks = calculate_iv_and_greeks('CE', spot_price, st, t, r, ce_ltp)
            pe_gks = calculate_iv_and_greeks('PE', spot_price, st, t, r, pe_ltp)

            output_rows.append([
                ce_gks['delta'], ce_gks['gamma'], ce_gks['theta'], ce_gks['vega'], f"{ce_gks['iv']}%", ce_ltp,
                st,
                pe_ltp, f"{pe_gks['iv']}%", pe_gks['delta'], pe_gks['gamma'], pe_gks['theta'], pe_gks['vega']
            ])

        if len(output_rows) > 1:
            data_sheet.batch_clear(["A10:M100"])
            data_sheet.update(range_name=f"A10:M{10 + len(output_rows) - 1}", values=output_rows)
            print(f"🎯 Google Sheet Successfully Updated!")

    except Exception as ex:
        print(f"❌ Exception in fetch loop: {str(ex)}")

# ==========================================
# MAIN EXECUTION THREADS
# ==========================================
def run_bot_loop():
    print("🚀 Starting Bot Loop Thread...")
    smart_api = SmartConnect(api_key=API_KEY)
    
    while True:
        try:
            totp = pyotp.TOTP(TOTP_SECRET.replace(" ", "")).now()
            data = smart_api.generateSession(CLIENT_ID, PIN, totp)
            if data and data.get('status'):
                print("✅ Angel One Session Generated Successfully!")
                initialize_scrip_master()
                break
            else:
                print(f"⚠️ Session Failed Response: {data}")
                time.sleep(10)
        except Exception as e:
            print(f"❌ Session Generation Exception: {e}")
            time.sleep(10)

    while True:
        try:
            fetch_and_update_option_chain(smart_api)
        except Exception as e:
            print(f"❌ Loop Error: {e}")
        time.sleep(15)

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    run_bot_loop()
