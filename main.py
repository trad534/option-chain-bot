import time
import os
import json
import pandas as pd
import pyotp
import gspread
from datetime import datetime
from SmartApi import SmartConnect
from flask import Flask
import threading
import builtins

# ==========================================
# FORCE FLUSH PRINTS
# ==========================================
def print(*args, **kwargs):
    kwargs['flush'] = True
    builtins.print(*args, **kwargs)

# ==========================================
# FLASK SERVER
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return "Option Chain Bot is active!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# ==========================================
# GOOGLE SHEET CONFIG
# ==========================================
gc = gspread.service_account(filename="Credentials.json")
spreadsheet = gc.open("6 LTP CALCULATOR")
data_sheet = spreadsheet.worksheet("upstox get data")
option_chain_sheet = spreadsheet.worksheet("Option chain index new")

# ==========================================
# GLOBAL VARIABLES
# ==========================================
cached_options_df = pd.DataFrame()
earliest_expiry = "N/A"
nifty_spot_token = None

# ==========================================
# SCRIP MASTER LOADER
# ==========================================
def initialize_scrip_master(smart_api):
    global cached_options_df, earliest_expiry, nifty_spot_token
    try:
        res = smart_api.searchScrip("NFO", "NIFTY")
        if res and res.get('status') and res.get('data'):
            df = pd.DataFrame(res['data'])

            if 'lotsize' in df.columns:
                df['lotsize'] = df['lotsize'].astype(int)

            df['parsed_expiry'] = pd.to_datetime(df['expiry'], errors='coerce')
            valid_opts = df.dropna(subset=['parsed_expiry'])
            if not valid_opts.empty:
                min_exp_dt = valid_opts['parsed_expiry'].min()
                earliest_expiry = min_exp_dt.strftime('%d%b%Y').upper()
                cached_options_df = df[df['parsed_expiry'] == min_exp_dt].copy()

            spot_row = df[df['tradingsymbol'].str.upper() == "NIFTY"]
            if not spot_row.empty:
                nifty_spot_token = str(spot_row['symboltoken'].iloc[0])

        print(f"✅ Loaded Expiry: {earliest_expiry}, Spot Token: {nifty_spot_token}")
    except Exception as e:
        print(f"⚠️ Error in scrip master: {e}")

# ==========================================
# LOT SIZE & EXPIRY UPDATE
# ==========================================
def update_lot_and_expiry():
    global cached_options_df, earliest_expiry
    try:
        selected_symbol = option_chain_sheet.acell("J4").value.strip()
        lot_size = "NA"
        if not cached_options_df.empty and 'lotsize' in cached_options_df.columns:
            match = cached_options_df[cached_options_df['symbol'].str.contains(selected_symbol, case=False, na=False)]
            if not match.empty:
                lot_size = int(match['lotsize'].values[0])

        # ✅ gspread update fix
        data_sheet.update([[lot_size]], "A7")
        data_sheet.update([[str(earliest_expiry)]], "L9")
        print(f"✅ Updated Lot Size {lot_size}, Expiry {earliest_expiry}")
    except Exception as e:
        print(f"⚠️ Error updating lot/expiry: {e}")

# ==========================================
# FETCH & UPDATE LOOP
# ==========================================
def fetch_and_update_option_chain(smart_api):
    global cached_options_df, earliest_expiry, nifty_spot_token
    try:
        current_time_str = datetime.now().strftime('%H:%M:%S')
        data_sheet.update([[f"Render is working - {current_time_str}"]], "A2")

        if cached_options_df.empty or not nifty_spot_token:
            initialize_scrip_master(smart_api)

        spot_price = 0.0
        if nifty_spot_token:
            spot_quote = smart_api.getMarketData("FULL", {"NSE": [nifty_spot_token]})
            if spot_quote and spot_quote.get('status') and spot_quote.get('data') and spot_quote['data'].get('fetched'):
                item = spot_quote['data']['fetched'][0]
                spot_price = float(item.get('ltp', item.get('close', 0)))
        print(f"🎯 Spot Price: {spot_price}")

        atm_strike = round(spot_price / 50) * 50
        print(f"🎯 ATM Strike: {atm_strike}")

        update_lot_and_expiry()

    except Exception as ex:
        print(f"❌ Exception in fetch loop: {str(ex)}")

# ==========================================
# MAIN LOOP
# ==========================================
def run_bot_loop():
    smart_api = SmartConnect(api_key=os.getenv("ANGEL_API_KEY"))
    while True:
        try:
            print("🔄 Attempting to generate session...")
            totp = pyotp.TOTP(os.getenv("ANGEL_TOTP_SECRET").replace(" ", "")).now()
            session = smart_api.generateSession(os.getenv("ANGEL_CLIENT_ID"), os.getenv("ANGEL_PIN"), totp)
            if session and session.get('status'):
                print("✅ Angel One Session Generated Successfully!")
                # ✅ Token set fix
                smart_api.setToken(session['data']['jwtToken'])
                initialize_scrip_master(smart_api)
                break
            else:
                print(f"⚠ Session Failed Response: {session}")
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
