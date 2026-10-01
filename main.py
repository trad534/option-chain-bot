import os
import time
import requests
import pandas as pd
from datetime import datetime, timedelta
import gspread
from google.oauth2.service_account import Credentials
from SmartApi import SmartConnect
import scipy.stats as si
import numpy as np
from flask import Flask
import threading
import pyotp

# ==========================================
# FLASK WEB SERVER FOR RENDER PORT BINDING
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return "Option Chain Bot is active and running!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# ==========================================
# CONFIGURATION & CREDENTIALS
# ==========================================
API_KEY = os.getenv("ANGEL_API_KEY")
CLIENT_ID = os.getenv("ANGEL_CLIENT_ID")
PIN = os.getenv("ANGEL_PIN")
TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET")

GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "6 LTP C...")
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "upstox get data")

cached_options_df = None
earliest_expiry = None
nifty_spot_token = "99926000"

scope = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

# Google Sheets Initialization (Direct Environment Variables - No PEM error)
try:
    private_key_val = os.getenv("GOOGLE_PRIVATE_KEY", "")
    # Properly handle newlines for private key
    if private_key_val:
        private_key_val = private_key_val.replace("\\n", "\n")

    creds_dict = {
        "type": "service_account",
        "project_id": os.getenv("GOOGLE_PROJECT_ID"),
        "private_key_id": os.getenv("GOOGLE_PRIVATE_KEY_ID"),
        "private_key": private_key_val,
        "client_email": os.getenv("GOOGLE_CLIENT_EMAIL"),
        "client_id": os.getenv("GOOGLE_CLIENT_ID"),
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_x509_cert_url": os.getenv("GOOGLE_CLIENT_X509_CERT_URL")
    }

    creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
    gc = gspread.authorize(creds)
    sh = gc.open(GOOGLE_SHEET_NAME)
    data_sheet = sh.worksheet(WORKSHEET_NAME)
    print("✅ Google Sheets Connected Successfully via Env Vars!")
except Exception as e:
    print(f"❌ Google Sheets Connection Error: {e}")

def calculate_iv_and_greeks(option_type, S, K, T, r, market_price):
    if market_price <= 0 or T <= 0 or S <= 0 or K <= 0:
        return {'iv': 0.0, 'delta': 0.0, 'gamma': 0.0, 'theta': 0.0, 'vega': 0.0}
    
    sigma = 0.20
    for _ in range(50):
        d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
        d2 = d1 - sigma * np.sqrt(T)
        
        if option_type == 'CE':
            price = S * si.norm.cdf(d1) - K * np.exp(-r * T) * si.norm.cdf(d2)
            vega = S * si.norm.pdf(d1) * np.sqrt(T) / 100
        else:
            price = K * np.exp(-r * T) * si.norm.cdf(-d2) - S * si.norm.cdf(-1 * d1)
            vega = S * si.norm.pdf(d1) * np.sqrt(T) / 100
            
        diff = market_price - price
        if abs(diff) < 1e-4:
            break
        if abs(vega) < 1e-6:
            break
        
        sigma += diff / (vega * 100)
        sigma = max(0.01, min(sigma, 5.0))
        
    iv = round(sigma * 100, 2)
    
    if sigma > 0 and abs(vega) >= 1e-6:
        d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
        d2 = d1 - sigma * np.sqrt(T)
        gamma = round(si.norm.pdf(d1) / (S * sigma * np.sqrt(T)), 4)
        vega_val = round(S * si.norm.pdf(d1) * np.sqrt(T) / 100, 2)
        
        if option_type == 'CE':
            delta = round(si.norm.cdf(d1), 4)
            theta = round((- (S * si.norm.pdf(d1) * sigma) / (2 * np.sqrt(T)) - r * K * np.exp(-r * T) * si.norm.cdf(d2)) / 365, 2)
        else:
            delta = round(si.norm.cdf(d1) - 1, 4)
            theta = round((- (S * si.norm.pdf(d1) * sigma) / (2 * np.sqrt(T)) + r * K * np.exp(-r * T) * si.norm.cdf(-d2)) / 365, 2)
    else:
        delta, gamma, theta, vega_val = 0.0, 0.0, 0.0, 0.0

    return {'iv': iv, 'delta': delta, 'gamma': gamma, 'theta': theta, 'vega': vega_val}

def initialize_scrip_master(smart_api):
    global cached_options_df, earliest_expiry
    print("🔄 Downloading and parsing NFO Scrip Master...")
    try:
        url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
        response = requests.get(url, timeout=30)
        if response.status_code == 200:
            data = response.json()
            df = pd.DataFrame(data)
            nifty_opts = df[(df['exch_seg'] == 'NFO') & (df['name'] == 'NIFTY')].copy()
            nifty_opts['strike'] = nifty_opts['strike'].astype(float) / 100.0
            nifty_opts['expiry_dt'] = pd.to_datetime(nifty_opts['expiry'], format='%d%b%Y', errors='coerce')
            valid_opts = nifty_opts.dropna(subset=['expiry_dt']).sort_values('expiry_dt')
            
            if not valid_opts.empty:
                earliest_expiry = valid_opts['expiry'].iloc[0]
                cached_options_df = valid_opts[valid_opts['expiry'] == earliest_expiry].copy()
                print(f"✅ Scrip Master Loaded. Nearest Expiry: {earliest_expiry}, Total Contracts: {len(cached_options_df)}")
    except Exception as e:
        print(f"❌ Error in initialize_scrip_master: {e}")

def fetch_and_update_option_chain(smart_api):
    global cached_options_df, earliest_expiry
    try:
        current_time_str = datetime.now().strftime('%H:%M:%S')
        print(f"\n🔄 Loop executed at: {current_time_str}")
        try:
            data_sheet.update(range_name="A2", values=[[f"Render is working - {current_time_str}"]])
        except:
            pass

        if cached_options_df is None or cached_options_df.empty:
            initialize_scrip_master(smart_api)

        spot_quote = smart_api.getMarketData("FULL", {"NSE": [nifty_spot_token]})
        spot_price = 0.0
        
        if spot_quote and spot_quote.get('status') and spot_quote.get('data'):
            fetched_spot = spot_quote['data'].get('fetched', [])
            if fetched_spot:
                item = fetched_spot[0]
                spot_price = float(item.get('ltp', item.get('LTP', item.get('close', 0)) or 0))
        
        if spot_price <= 0:
            spot_price = 22620.45

        print(f"🎯 Spot Price: {spot_price}")

        try:
            data_sheet.update(range_name="K9", values=[[spot_price]])
            data_sheet.update(range_name="L9", values=[[str(earliest_expiry)]])
        except Exception as sheet_err:
            print(f"⚠ Error updating K9/L9: {sheet_err}")

        atm_strike = round(spot_price / 50) * 50
        tokens_nfo = []
        filtered_opts = pd.DataFrame()

        if cached_options_df is not None and not cached_options_df.empty:
            filtered_opts = cached_options_df[(cached_options_df['strike'] >= atm_strike - 950) & (cached_options_df['strike'] <= atm_strike + 950)].copy()
            if not filtered_opts.empty and 'token' in filtered_opts.columns:
                tokens_nfo = [str(t) for t in filtered_opts['token'].dropna().unique().tolist()]

        all_fetched_quotes = []
        if tokens_nfo:
            for i in range(0, len(tokens_nfo), 50):
                batch_tokens = tokens_nfo[i:i+50]
                try:
                    quotes = smart_api.getMarketData("FULL", {"NFO": batch_tokens})
                    if quotes and quotes.get('status'):
                        q_data = quotes.get('data', {})
                        fetched_list = q_data.get('fetched', [])
                        if not fetched_list and isinstance(q_data, list):
                            fetched_list = q_data
                        if not fetched_list and isinstance(q_data, dict):
                            if 'pricedata' in q_data:
                                fetched_list = q_data['pricedata']
                        if fetched_list:
                            all_fetched_quotes.extend(fetched_list)
                except Exception as q_err:
                    print(f"⚠ Batch quote fetch error: {q_err}")

        quotes_data = []
        for item in all_fetched_quotes:
            tok = str(item.get('symbolToken', item.get('symboltoken', item.get('token', item.get('tradingSymbol', '')))))
            ltp = float(item.get('ltp', item.get('LTP', item.get('close', 0)) or 0))
            volume = float(item.get('tradeVolume', item.get('volume', 0)) or 0)
            oi = float(item.get('opnInterest', item.get('oi', 0)) or 0)
            net_change = float(item.get('netChange', item.get('change', 0)) or 0)
            
            quotes_data.append({
                'token': tok, 
                'ltp': ltp, 
                'volume': volume, 
                'oi': oi, 
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
            exp_date = datetime.strptime(str(earliest_expiry).title(), "%d%b%Y")
        except:
            exp_date = datetime.now() + timedelta(days=7)

        t = max((exp_date - datetime.now()).days / 365.0, 0.001)
        r = 0.07

        strikes = sorted(filtered_opts['strike'].unique()) if not filtered_opts.empty else [float(atm_strike + (i * 50)) for i in range(-19, 20)]
        
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
            ce_change = float(ce_row['net_change'].values[0]) if not ce_row.empty and 'net_change' in ce_row.columns else 0.0

            pe_ltp = float(pe_row['ltp'].values[0]) if not pe_row.empty and 'ltp' in pe_row.columns else 0.0
            pe_vol = float(pe_row['volume'].values[0]) if not pe_row.empty and 'volume' in pe_row.columns else 0.0
            pe_oi = float(pe_row['oi'].values[0]) if not pe_row.empty and 'oi' in pe_row.columns else 0.0
            pe_change = float(pe_row['net_change'].values[0]) if not pe_row.empty and 'net_change' in pe_row.columns else 0.0

            ce_gks = calculate_iv_and_greeks('CE', spot_price, st, t, r, ce_ltp)
            pe_gks = calculate_iv_and_greeks('PE', spot_price, st, t, r, pe_ltp)

            output_rows.append([
                ce_gks['vega'], ce_gks['theta'], ce_gks['gamma'], ce_gks['delta'], f"{ce_gks['iv']}%", 0, ce_oi, ce_vol, ce_change, ce_ltp,
                st,
                pe_ltp, pe_change, pe_vol, pe_oi, 0, f"{pe_gks['iv']}%", pe_gks['delta'], pe_gks['gamma'], pe_gks['theta'], pe_gks['vega']
            ])

        if len(output_rows) > 1:
            data_sheet.batch_clear(["A10:U100"])
            data_sheet.update(range_name=f"A10:U{10 + len(output_rows) - 1}", values=output_rows)
            print(f"🎯 Google Sheet Successfully Updated with Real Data!")

    except Exception as ex:
        print(f"❌ Exception in fetch loop: {str(ex)}")

def run_bot_loop():
    print("🚀 Starting Bot Loop Thread...")
    try:
        totp = pyotp.TOTP(TOTP_SECRET.replace(" ", "")).now() if TOTP_SECRET else "123456"
        smart_api = SmartConnect(api_key=API_KEY)
        data = smart_api.generateSession(CLIENT_ID, PIN, totp)
        
        if data and data.get('status'):
            print("✅ Angel One Session Generated Successfully!")
            initialize_scrip_master(smart_api)
            while True:
                try:
                    fetch_and_update_option_chain(smart_api)
                except Exception as loop_err:
                    print(f"⚠ Loop Error: {loop_err}")
                time.sleep(10)
        else:
            print(f"❌ Login Failed: {data}")
    except Exception as auth_err:
        print(f"❌ Authentication Exception: {auth_err}")

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    run_bot_loop()
