import time
import os
import json
import urllib.request
import pandas as pd
import numpy as np
import pyotp
import gspread
from scipy.stats import norm
from datetime import datetime, time as dtime
from SmartApi import SmartConnect
from flask import Flask
import threading

# ==========================================
# FLASK WEB SERVER FOR RENDER PORT REQUIREMENT
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
    print("==================================================")
    print("SUCCESS: Google Sheet Connected Successfully!!!")
    print("==================================================")
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
# GLOBAL CACHE FOR SCRIP MASTER
# ==========================================
cached_nifty_spot_token = "99926000"
cached_options_df = None
earliest_expiry = None

def initialize_scrip_master():
    global cached_nifty_spot_token, cached_options_df, earliest_expiry
    try:
        print("📥 Downloading Scrip Master (One-time setup)...")
        url = "https://margincalculator.angelbroking.com/open-api/tokenMaster/OpenAPIScripMaster.json"
        response = urllib.request.urlopen(url)
        scrip_data = json.loads(response.read().decode())
        df = pd.DataFrame(scrip_data)

        nifty_spot_row = df[(df['exch_seg'] == 'NSE') & ((df['symbol'] == 'NIFTY') | (df['name'] == 'NIFTY'))]
        if not nifty_spot_row.empty:
            cached_nifty_spot_token = str(nifty_spot_row.iloc[0]['token'])
        
        print(f"✅ Nifty Spot Token Set: {cached_nifty_spot_token}")

        nifty_opts = df[(df['exch_seg'] == 'NFO') & (df['name'] == 'NIFTY') & (df['instrumenttype'] == 'OPTIDX')].copy()
        nifty_opts['strike'] = nifty_opts['strike'].astype(float) / 100
        
        earliest_expiry = nifty_opts['expiry'].min()
        cached_options_df = nifty_opts[nifty_opts['expiry'] == earliest_expiry].copy()
        print(f"✅ Scrip Master Loaded Successfully! Expiry: {earliest_expiry}, Total Options: {len(cached_options_df)}")
    except Exception as e:
        print(f"❌ Error loading Scrip Master: {e}")
        cached_nifty_spot_token = "99926000"

# ==========================================
# FETCH & UPDATE LOOP
# ==========================================
def fetch_and_update_angel_option_chain(smart_api):
    global cached_options_df, cached_nifty_spot_token, earliest_expiry
    try:
        current_time_str = datetime.now().strftime('%H:%M:%S')
        data_sheet.update(range_name="A2", values=[[f"Render is working - {current_time_str}"]])

        now_time = datetime.now().time()
        start_market_time = dtime(9, 10)
        end_market_time = dtime(15, 35)

        if not (start_market_time <= now_time <= end_market_time):
            print(f"⏳ Outside market hours ({now_time}). Waiting...")
            return

        # Fetch Spot Price using cached token
        spot_quote = smart_api.getMarketData("FULL", {"NSE": [str(cached_nifty_spot_token)]})
        if not spot_quote or not spot_quote.get('status') or not spot_quote['data']['fetched']:
            print(f"❌ Failed to fetch Spot Quote: {spot_quote}")
            return
            
        spot_price = float(spot_quote['data']['fetched'][0]['ltp'])

        atm_strike = round(spot_price / 50) * 50
        filtered_opts = cached_options_df[(cached_options_df['strike'] >= atm_strike - 250) & (cached_options_df['strike'] <= atm_strike + 250)].copy()

        tokens_nfo = [str(t) for t in filtered_opts['token'].unique().tolist()]
        if not tokens_nfo:
            print("❌ No NFO tokens found for current strike range!")
            return

        quotes = smart_api.getMarketData("FULL", {"NFO": tokens_nfo})
        
        if not quotes or not quotes.get('status') or not quotes['data']['fetched']:
            print(f"❌ Failed to fetch Market Quotes: {quotes}")
            return

        quotes_df = pd.DataFrame(quotes['data']['fetched'])
        merged = pd.merge(filtered_opts[['token', 'symbol', 'strike', 'symbolname']], 
                          quotes_df[['tradingSymbol', 'ltp']], 
                          left_on='symbol', right_on='tradingSymbol', how='inner')

        # Fixed expiry date parsing (.title() handles uppercase month strings like 'OCT')
        exp_date = datetime.strptime(str(earliest_expiry).title(), "%d%b%Y")
        t = max((exp_date - datetime.now()).days / 365.0, 0.001)
        r = 0.07

        strikes = sorted(merged['strike'].unique())
        output_rows = [
            ["CE Delta", "CE Gamma", "CE Theta", "CE Vega", "CE IV", "CE LTP", "Strike Price", "PE LTP", "PE IV", "PE Delta", "PE Gamma", "PE Theta", "PE Vega"]
        ]

        for st in strikes:
            ce_row = merged[(merged['strike'] == st) & (merged['symbol'].str.endswith('CE'))]
            pe_row = merged[(merged['strike'] == st) & (merged['symbol'].str.endswith('PE'))]

            ce_ltp = float(ce_row['ltp'].values[0]) if not ce_row.empty else 0.0
            pe_ltp = float(pe_row['ltp'].values[0]) if not pe_row.empty else 0.0

            ce_gks = calculate_iv_and_greeks('CE', spot_price, st, t, r, ce_ltp)
            pe_gks = calculate_iv_and_greeks('PE', spot_price, st, t, r, pe_ltp)

            output_rows.append([
                ce_gks['delta'], ce_gks['gamma'], ce_gks['theta'], ce_gks['vega'], f"{ce_gks['iv']}%", ce_ltp,
                st,
                pe_ltp, f"{pe_gks['iv']}%", pe_gks['delta'], pe_gks['gamma'], pe_gks['theta'], pe_gks['vega']
            ])

        data_sheet.batch_clear(["A10:M100"])
        data_sheet.update(range_name=f"A10:M{10 + len(output_rows) - 1}", values=output_rows)
        print(f"🎯 Option Chain Updated in Google Sheet at {datetime.now().strftime('%H:%M:%S')}!")

    except Exception as ex:
        print(f"❌ Exception in Process: {ex}")

# ==========================================
# MAIN EXECUTION THREADS
# ==========================================
def run_bot_loop():
    print("🚀 Starting Option Chain Loop...")
    smart_api = SmartConnect(api_key=API_KEY)
    
    # Login & Initial Setup
    while True:
        try:
            totp = pyotp.TOTP(TOTP_SECRET.replace(" ", "")).now()
            data = smart_api.generateSession(CLIENT_ID, PIN, totp)
            if data.get('status'):
                print("✅ Angel One Session Generated Successfully")
                initialize_scrip_master()
                break
            else:
                print(f"❌ Login failed: {data.get('message')}, retrying in 10s...")
                time.sleep(10)
        except Exception as e:
            print(f"❌ Login Error: {e}, retrying in 10s...")
            time.sleep(10)

    # Continuous Loop
    while True:
        try:
            fetch_and_update_angel_option_chain(smart_api)
        except Exception as e:
            print(f"❌ Loop Error: {e}")
        time.sleep(15)

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    run_bot_loop()
