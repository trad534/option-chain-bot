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
# FETCH & UPDATE LOOP (FAST MODE)
# ==========================================
def fetch_and_update_angel_option_chain(smart_api):
    try:
        current_time_str = datetime.now().strftime('%H:%M:%S')
        data_sheet.update(range_name="A2", values=[[f"Render is working - {current_time_str}"]])

        # 1. Fetch Nifty Spot Price directly using Nifty Index token (99926000)
        spot_quote = smart_api.getMarketData("FULL", {"NSE": ["99926000"]})
        if not spot_quote or not spot_quote.get('status') or not spot_quote['data']['fetched']:
            print(f"❌ Failed to fetch Spot Quote: {spot_quote}")
            return
            
        spot_price = float(spot_quote['data']['fetched'][0]['ltp'])
        print(f"🎯 Nifty Spot Price: {spot_price}")

        atm_strike = round(spot_price / 50) * 50

        # 2. Generate strikes around ATM (Step of 50, from -250 to +250)
        strikes = [atm_strike + i for i in range(-250, 300, 50)]
        
        # Build dynamic symbols for nearest expiry (e.g., 01OCT2026)
        # Angel One symbol format: NIFTY01OCT2625000CE
        # Since exact symbol requires exact expiry date string, let's fetch matching symbols via SmartAPI search/holding if needed, 
        # OR better: let's query Angel One's searchScrip API directly for current expiry strikes to avoid large master file download!
        
        print(f"🎯 ATM Strike calculated: {atm_strike}. Updating Sheet...")

        # For testing speed, let's write a confirmation row in sheet to verify fast execution
        output_rows = [
            ["CE Delta", "CE Gamma", "CE Theta", "CE Vega", "CE IV", "CE LTP", "Strike Price", "PE LTP", "PE IV", "PE Delta", "PE Gamma", "PE Theta", "PE Vega"],
            [0, 0, 0, 0, "0%", 0, atm_strike, 0, "0%", 0, 0, 0, 0]
        ]
        
        data_sheet.batch_clear(["A10:M20"])
        data_sheet.update(range_name="A10:M11", values=output_rows)
        print(f"✅ Sheet updated successfully with ATM {atm_strike} at {current_time_str}")

    except Exception as ex:
        print(f"❌ Exception in Process: {str(ex)}")

# ==========================================
# MAIN EXECUTION THREADS
# ==========================================
def run_bot_loop():
    print("🚀 Starting Fast Option Chain Loop...")
    smart_api = SmartConnect(api_key=API_KEY)
    
    while True:
        try:
            totp = pyotp.TOTP(TOTP_SECRET.replace(" ", "")).now()
            data = smart_api.generateSession(CLIENT_ID, PIN, totp)
            if data.get('status'):
                print("✅ Angel One Session Generated Successfully")
                break
            else:
                print(f"❌ Login failed: {data.get('message')}, retrying in 10s...")
                time.sleep(10)
        except Exception as e:
            print(f"❌ Login Error: {e}, retrying in 10s...")
            time.sleep(10)

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
