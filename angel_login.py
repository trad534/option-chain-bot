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

# ==========================================
# CONFIGURATION & GOOGLE SHEET
# ==========================================
CLIENT_ID = os.getenv("ANGEL_CLIENT_ID")
PIN = os.getenv("ANGEL_PIN")
API_KEY = os.getenv("ANGEL_API_KEY")
TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET")

GOOGLE_SHEET_NAME = "6 LTP CALCULATOR"
MAIN_SHEET_NAME = "upstox get data"

try:
    gc = gspread.service_account(filename="Credentials.json")
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

    # Implied Volatility (IV) Calculation via Bisection Method
    low_sigma, high_sigma = 0.01, 3.0
    sigma = 0.20
    for _ in range(20):
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

    # Greeks Calculation
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
# ANGEL ONE DATA FETCH & GOOGLE SHEET SYNC
# ==========================================
def fetch_and_update_angel_option_chain():
    try:
        # 1. Login to Angel One
        smart_api = SmartConnect(api_key=API_KEY)
        totp = pyotp.TOTP(TOTP_SECRET.replace(" ", "")).now()
        data = smart_api.generateSession(CLIENT_ID, PIN, totp)
        
        if not data.get('status'):
            print(f"❌ Angel One Login Failed: {data.get('message')}")
            return

        print("✅ Angel One Login Successful!")

        # 2. Fetch Master Scrip File
        url = "https://margincalculator.angelbroking.com/open-api/tokenMaster/OpenAPIScripMaster.json"
        response = urllib.request.urlopen(url)
        scrip_data = json.loads(response.read().decode())
        df = pd.DataFrame(scrip_data)

        # 3. Get NIFTY Live Spot Price
        nifty_spot = df[(df['name'] == 'NIFTY') & (df['instrumenttype'] == 'AMXIDX') & (df['exch_seg'] == 'NSE')].iloc[0]
        spot_quote = smart_api.getMarketData("FULL", {"NSE": [str(nifty_spot['token'])]})
        spot_price = float(spot_quote['data']['fetched'][0]['ltp'])
        print(f"📌 NIFTY Live Spot Price: {spot_price}")

        # 4. Filter Options (Current Expiry & ATM +- 500)
        nifty_opts = df[(df['name'] == 'NIFTY') & (df['instrumenttype'] == 'OPTIDX') & (df['exch_seg'] == 'NFO')].copy()
        nifty_opts['strike'] = nifty_opts['strike'].astype(float) / 100
        
        earliest_expiry = nifty_opts['expiry'].min()
        expiry_opts = nifty_opts[nifty_opts['expiry'] == earliest_expiry].copy()

        atm_strike = round(spot_price / 50) * 50
        filtered_opts = expiry_opts[(expiry_opts['strike'] >= atm_strike - 500) & (expiry_opts['strike'] <= atm_strike + 500)].copy()

        # 5. Fetch Live Quotes
        tokens_nfo = list(filtered_opts['token'].unique())
        quotes = smart_api.getMarketData("FULL", {"NFO": tokens_nfo})
        
        if not quotes or not quotes.get('status') or not quotes['data']['fetched']:
            print("❌ Failed to fetch Market Quotes")
            return

        quotes_df = pd.DataFrame(quotes['data']['fetched'])
        merged = pd.merge(filtered_opts[['token', 'symbol', 'strike', 'symbolname']], 
                          quotes_df[['tradingSymbol', 'ltp']], 
                          left_on='symbol', right_on='tradingSymbol', how='inner')

        # 6. Separate Call & Put Data by Strike
        exp_date = datetime.strptime(earliest_expiry, "%d%b%Y")
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

        # 7. Update Google Sheet at A10
        data_sheet.batch_clear(["A10:M1000"])
        data_sheet.update(range_name=f"A10:M{10 + len(output_rows) - 1}", values=output_rows)
        print("🎯 Google Sheet Updated Successfully at Cell A10!")

    except Exception as ex:
        print(f"❌ Exception in Process: {ex}")

if __name__ == "__main__":
    fetch_and_update_angel_option_chain()
