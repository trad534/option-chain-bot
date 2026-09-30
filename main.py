import time
import os
import json
import pandas as pd
import numpy as np
import pyotp
import gspread
from scipy.stats import norm
from datetime import datetime, timedelta
from SmartApi import SmartConnect
from flask import Flask
import threading
import builtins

# ==========================================
# FORCE FLUSH LOGS IN RENDER (BUFFERING FIX)
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
    option_chain_sheet = spreadsheet.worksheet("Option chain index new")
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
        if price > ltp:   # ✅ corrected condition
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
# SCRIP MASTER LOADER VIA SMARTAPI SEARCH
# ==========================================
cached_options_df = pd.DataFrame()
earliest_expiry = "N/A"
nifty_spot_token = "26000"

def get_upcoming_tuesday():
    today = datetime.now()
    days_ahead = (1 - today.weekday() + 7) % 7
    if days_ahead == 0 and today.hour >= 15:
        days_ahead = 7
    tuesday = today + timedelta(days=days_ahead)
    return tuesday.strftime('%d%b%Y').upper()

def initialize_scrip_master(smart_api):
    global cached_options_df, earliest_expiry
    try:
        print("📥 Fetching Nifty Options via SmartAPI Search...")
        res = smart_api.searchScrip("NFO", "NIFTY")
        if res and res.get('status') and res.get('data'):
            scrip_data = res['data']
            df = pd.DataFrame(scrip_data)
            
            nifty_opts = df[df['instrumenttype'] == 'OPTIDX'].copy()
            if not nifty_opts.empty:
                if 'strike' in nifty_opts.columns:
                    nifty_opts['strike'] = nifty_opts['strike'].astype(float)
                    if nifty_opts['strike'].max() > 50000:
                        nifty_opts['strike'] = nifty_opts['strike'] / 100
                
                if 'expiry' in nifty_opts.columns:
                    nifty_opts['parsed_expiry'] = pd.to_datetime(nifty_opts['expiry'], errors='coerce')
                    valid_opts = nifty_opts.dropna(subset=['parsed_expiry'])
                    
                    if not valid_opts.empty:
                        min_exp_dt = valid_opts['parsed_expiry'].min()
                        earliest_expiry = min_exp_dt.strftime('%d%b%Y').upper()
                        cached_options_df = nifty_opts[nifty_opts['parsed_expiry'] == min_exp_dt].copy()
                        
                if cached_options_df.empty and not nifty_opts.empty:
                    cached_options_df = nifty_opts.copy()

                if 'symboltoken' in cached_options_df.columns and 'token' not in cached_options_df.columns:
                    cached_options_df['token'] = cached_options_df['symboltoken']
                if 'tradingsymbol' in cached_options_df.columns and 'symbol' not in cached_options_df.columns:
                    cached_options_df['symbol'] = cached_options_df['tradingsymbol']

        if earliest_expiry == "N/A" or not earliest_expiry:
            earliest_expiry = get_upcoming_tuesday()

        print(f"✅ Scrip Master Loaded! Final Expiry: {earliest_expiry}, Total Options: {len(cached_options_df)}")
    except Exception as e:
        print(f"⚠️ Scrip Master API Error: {e}")
        if earliest_expiry == "N/A":
            earliest_expiry = get_upcoming_tuesday()

# ==========================================
# LOT SIZE & EXPIRY AUTO UPDATE
# ==========================================
def update_lot_and_expiry():
    global cached_options_df, earliest_expiry
    try:
        selected_symbol = option_chain_sheet.acell("J4").value.strip()
        if not selected_symbol:
            return

        lot_size = "NA"
        if not cached_options_df.empty and 'symbol' in cached_options_df.columns and 'lotsize' in cached_options_df.columns:
            match = cached_options_df[cached_options_df['symbol'].str.contains(selected_symbol, case=False, na=False)]
            if not match.empty:
                lot_size = int(match['lotsize'].values[0])

        data_sheet.update("A7", [[lot_size]])
        data_sheet.update("L9", [[str(earliest_expiry)]])
        print(f"✅ Lot Size & Expiry Updated → {selected_symbol}: Lot {lot_size}, Expiry {earliest_expiry}")
    except Exception as e:
        print(f"⚠️ Error updating Lot Size/Expiry: {e}")

# ==========================================
# FETCH & UPDATE LOOP
# ==========================================
def fetch_and_update_option_chain(smart_api):
    global cached_options_df, earliest_expiry
    try:
        current_time_str = datetime.now().strftime('%H:%M:%S')
        print(f"\n🔄 Loop executed at: {current_time_str}")
        data_sheet.update(range_name="A2", values=[[f"Render is
