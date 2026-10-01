import time
import os
import json
import requests
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
    return "Universal Option Chain Bot is active and running!"

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
DATA_SHEET_NAME = "upstox get data"

EXACT_LOT_SIZES = {
    "NIFTY": 65,
    "BANKNIFTY": 30,
    "FINNIFTY": 25,
    "MIDCPNIFTY": 50,
    "SENSEX": 10,
    "BANKEX": 15,
    "GOLD": 100,
    "SILVER": 30,
    "CRUDEOIL": 100,
    "NATURALGAS": 1250,
    "COPPER": 2500,
    "AARTIIND": 1000,
    "AARTITRADE": 1000
}

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
    data_sheet = spreadsheet.worksheet(DATA_SHEET_NAME)
    print("✅ SUCCESS: Google Sheets Connected Successfully!")
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
# FETCH & UPDATE LOOP
# ==========================================
def fetch_and_update_option_chain(smart_api):
    try:
        current_time_str = datetime.now().strftime('%H:%M:%S')
        print(f"\n🔄 Loop executed at: {current_time_str}")
        try:
            data_sheet.update(range_name="A2", values=[[f"Render is working - {current_time_str}"]])
        except:
            pass

        # 1. Read Selected Symbol from J4 dynamically across worksheets
        selected_symbol = "NIFTY"
        try:
            target_ws = None
            for ws in spreadsheet.worksheets():
                if "index" in ws.title.lower() or "chain" in ws.title.lower():
                    target_ws = ws
                    break
            if not target_ws:
                target_ws = spreadsheet.worksheet("Option chain index new")
            
            j4_val = target_ws.acell('J4').value
            if j4_val and str(j4_val).strip():
                selected_symbol = str(j4_val).strip().upper()
        except:
            try:
                j4_fallback = data_sheet.acell('J4').value
                if j4_fallback and str(j4_fallback).strip():
                    selected_symbol = str(j4_fallback).strip().upper()
            except:
                pass

        print(f"📌 Active Selected Symbol: {selected_symbol}")

        is_index = selected_symbol in ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"]
        is_mcx = any(comm in selected_symbol for comm in ["GOLD", "SILVER", "CRUDEOIL", "NATURALGAS", "COPPER", "ZINC", "LEAD", "NICKEL", "ALUMINIUM"])
        
        exchange = "MCX" if is_mcx else "NFO"
        spot_exchange = "MCX" if is_mcx else "NSE"

        # 2. Spot Price First
        spot_token_map = {
            "NIFTY": "26000", 
            "BANKNIFTY": "26009", 
            "FINNIFTY": "26037",
            "MIDCPNIFTY": "26074",
            "SENSEX": "1",
            "BANKEX": "13"
        }
        
        spot_token = spot_token_map.get(selected_symbol, "")
        if not spot_token and not is_mcx:
            try:
                st_res = smart_api.searchScrip("NSE", selected_symbol)
                if st_res and st_res.get('status') and st_res.get('data'):
                    for item in st_res['data']:
                        if item.get('tradingsymbol') == selected_symbol or item.get('symbol') == selected_symbol:
                            spot_token = str(item.get('symboltoken', item.get('token', '')))
                            break
            except:
                pass

        spot_price = 0.0
        if spot_token:
            try:
                spot_quote = smart_api.getMarketData("FULL", {spot_exchange: [spot_token]})
                if spot_quote and spot_quote.get('status') and spot_quote.get('data') and spot_quote['data'].get('fetched'):
                    item = spot_quote['data']['fetched'][0]
                    spot_price = float(item.get('ltp', item.get('LTP', item.get('close', 0))))
            except:
                pass

        if spot_price <= 0:
            if selected_symbol == "NIFTY":
                spot_price = 24600.0
            elif selected_symbol == "BANKNIFTY":
                spot_price = 50000.0
            else:
                spot_price = 500.0

        print(f"🎯 Spot Price: {spot_price}")

        try:
            data_sheet.update(range_name="K9", values=[[spot_price]])
        except:
            pass

        # 3. Scrip Master Fetch
        all_options_df = pd.DataFrame()
        available_expiries = []
        
        scrip_data = []
        try:
            search_ex = "MCX" if is_mcx else "NFO"
            res = smart_api.searchScrip(search_ex, selected_symbol)
            if res and res.get('status') and res.get('data'):
                scrip_data = res['data']
        except Exception:
            pass

        if scrip_data:
            df = pd.DataFrame(scrip_data)
            if not df.empty:
                if 'tradingsymbol' in df.columns:
                    if is_index:
                        exact_match = df[df['tradingsymbol'].str.startswith(selected_symbol, na=False)].copy()
                    else:
                        exact_match = df[df['tradingsymbol'].str.contains(selected_symbol, case=False, na=False)].copy()
                    if not exact_match.empty:
                        df = exact_match

                opt_df = df[df['instrumenttype'].isin(['OPTIDX', 'OPTSTK', 'OPTOM'])].copy() if 'instrumenttype' in df.columns else df.copy()
                if opt_df.empty:
                    opt_df = df.copy()

                if not opt_df.empty:
                    if 'strike' in opt_df.columns:
                        opt_df['strike'] = opt_df['strike'].astype(float)
                        if opt_df['strike'].max() > 500000:
                            opt_df['strike'] = opt_df['strike'] / 100
                    
                    all_options_df = opt_df.copy()
                    if 'symboltoken' in all_options_df.columns:
                        all_options_df['token'] = all_options_df['symboltoken']
                    elif 'token' not in all_options_df.columns and 'symbolToken' in all_options_df.columns:
                        all_options_df['token'] = all_options_df['symbolToken']

                    if 'tradingsymbol' in all_options_df.columns:
                        all_options_df['symbol'] = all_options_df['tradingsymbol']
                    elif 'symbol' not in all_options_df.columns and 'tradingSymbol' in all_options_df.columns:
                        all_options_df['symbol'] = all_options_df['tradingSymbol']
                    
                    if 'expiry' in all_options_df.columns:
                        raw_expiries = all_options_df['expiry'].dropna().unique()
                        parsed_exps = []
                        for exp in raw_expiries:
                            cleaned_exp = str(exp).strip().upper()
                            try:
                                dt = datetime.strptime(cleaned_exp, "%d%b%Y")
                                if dt.date() >= datetime.now().date() - timedelta(days=1):
                                    parsed_exps.append((dt, cleaned_exp))
                            except:
                                pass
                        parsed_exps.sort(key=lambda x: x[0])
                        seen = set()
                        for dt, exp_str in parsed_exps:
                            if exp_str not in seen:
                                seen.add(exp_str)
                                available_expiries.append([exp_str])
                            if len(available_expiries) >= 4:
                                break

        # Lot Size
        lot_size = EXACT_LOT_SIZES.get(selected_symbol, 50 if is_index else 1000)

        try:
            data_sheet.update(range_name="A7", values=[[lot_size]])
        except:
            pass

        if not available_expiries:
            available_expiries = [["08OCT2026"], ["15OCT2026"], ["22OCT2026"], ["29OCT2026"]]

        try:
            data_sheet.update(range_name="A3:A6", values=available_expiries)
        except:
            pass

        flat_expiries = [item[0] for item in available_expiries]
        selected_expiry = "N/A"
        try:
            l9_val = data_sheet.acell('L9').value
            if l9_val and str(l9_val).strip().upper() in flat_expiries:
                selected_expiry = str(l9_val).strip().upper()
        except:
            pass

        if selected_expiry == "N/A" or selected_expiry not in flat_expiries:
            selected_expiry = flat_expiries[0]
            try:
                data_sheet.update(range_name="L9", values=[[selected_expiry]])
            except:
                pass

        print(f"📅 Active Expiry: {selected_expiry}")

        # 4. Strike Gap (Step Size)
        if selected_symbol == "BANKNIFTY":
            strike_gap = 100
        elif selected_symbol in ["NIFTY", "FINNIFTY", "MIDCPNIFTY"]:
            strike_gap = 50
        elif selected_symbol in ["SENSEX", "BANKEX"]:
            strike_gap = 100
        elif is_mcx:
            strike_gap = 100 if spot_price > 10000 else 10
        else:
            strike_gap = 5 if spot_price < 500 else (10 if spot_price < 1000 else 20)

        atm_strike = round(spot_price / strike_gap) * strike_gap

        # 5. Filter options for selected expiry safely
        tokens_nfo = []
        filtered_opts = pd.DataFrame()
        
        if all_options_df is not None and not all_options_df.empty:
            if 'expiry' in all_options_df.columns:
                expiry_filtered = all_options_df[all_options_df['expiry'].str.upper() == selected_expiry].copy()
                if expiry_filtered.empty:
                    expiry_filtered = all_options_df.copy()
            else:
                expiry_filtered = all_options_df.copy()

            range_span = strike_gap * 15
            filtered_opts = expiry_filtered[(expiry_filtered['strike'] >= atm_strike - range_span) & (expiry_filtered['strike'] <= atm_strike + range_span)].copy()
            
            if not filtered_opts.empty:
                if 'token' in filtered_opts.columns:
                    tokens_nfo = [str(t) for t in filtered_opts['token'].dropna().unique().tolist()]

        # 6. Fetch Option Quotes securely
        all_fetched_quotes = []
        if tokens_nfo:
            for i in range(0, len(tokens_nfo), 50):
                batch_tokens = tokens_nfo[i:i+50]
                try:
                    quotes = smart_api.getMarketData("FULL", {exchange: batch_tokens})
                    if quotes and quotes.get('status') and quotes.get('data'):
                        fetched_list = quotes['data'].get('fetched', [])
                        if fetched_list:
                            all_fetched_quotes.extend(fetched_list)
                except Exception as q_err:
                    print(f"⚠️️ Quote fetch error: {q_err}")

        quotes_data = []
        for item in all_fetched_quotes:
            tok = str(item.get('symbolToken', item.get('symboltoken', item.get('token', ''))))
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
            filtered_opts['token'] = filtered_opts['token'].astype(str)
            quotes_df['token'] = quotes_df['token'].astype(str)
            merged = pd.merge(filtered_opts[['token', 'symbol', 'strike']], quotes_df, on='token', how='inner')
        else:
            merged = pd.DataFrame()

        try:
            exp_date = datetime.strptime(str(selected_expiry).title(), "%d%b%Y")
        except:
            exp_date = datetime.now() + timedelta(days=7)

        t = max((exp_date - datetime.now()).days / 365.0, 0.001)
        r = 0.07

        strikes = sorted(merged['strike'].unique()) if not merged.empty else [float(atm_strike + (i * strike_gap)) for i in range(-10, 11)]
        
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
            print(f"🎯 Google Sheet Successfully Updated for {selected_symbol} ({exchange})!")

    except Exception as ex:
        print(f"❌ Exception in fetch loop: {str(ex)}")

# ==========================================
# MAIN EXECUTION THREADS
# ==========================================
def run_bot_loop():
    print("🚀 Starting Universal Bot Loop Thread...")
    
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
        time.sleep(15)

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    run_bot_loop()
