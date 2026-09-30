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
# LOT SIZE & EXPIRY AUTO UPDATE
# ==========================================
cached_options_df = pd.DataFrame()
earliest_expiry = "N/A"

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
        
        # ✅ Corrected f-string (no syntax error now)
        data_sheet.update(range_name="A2", values=[[f"Render is working - {current_time_str}"]])

        # यहां बाकी आपका option chain fetch/update logic रहेगा...
        update_lot_and_expiry()

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
