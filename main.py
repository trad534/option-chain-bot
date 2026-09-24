import time
import os
import requests
import pyotp
import gspread
import upstox_client
from upstox_client.rest import ApiException
from datetime import datetime

# ==========================================
# CONFIGURATION & LOGIN PARAMETERS
# ==========================================
API_KEY = "399fd5d0-cb80-4983-bdd8-bcfc05091459"
API_SECRET = "3ll8j7cxaf"
REDIRECT_URI = "https://127.0.0"

USER_ID = "3CAGY5"
PIN = "011996"

TOTP_SECRET = os.getenv("TOTP_SECRET", "YOUR_TOTP_SECRET_KEY_HERE")

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
    print(f"Google Sheet Connection Error: {e}")
    exit()

# ==========================================
# AUTOMATIC DAILY LOGIN FUNCTION (TOTP)
# ==========================================
def generate_automatic_access_token():
    if "YOUR_TOTP_SECRET" in TOTP_SECRET or not TOTP_SECRET:
        print("🔴 Cannot Auto-Login: TOTP Secret Key abhi dali nahi gayi hai!")
        return None
        
    print("🔄 Initiating Auto-Login via Upstox API & TOTP...")
    
    try:
        totp = pyotp.TOTP(TOTP_SECRET.replace(" ", ""))
        otp_code = totp.now()
        
        session_url = "https://api.upstox.com/v2/login/auth/token"
        headers = {
            'accept': 'application/json',
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        data = {
            'code': otp_code,
            'client_id': API_KEY,
            'client_secret': API_SECRET,
            'redirect_uri': REDIRECT_URI,
            'grant_type': 'authorization_code'
        }
        
        response = requests.post(session_url, headers=headers, data=data)
        res_data = response.json()
        
        if 'access_token' in res_data:
            fresh_token = res_data['access_token']
            data_sheet.update_acell('A1', fresh_token)
            print("🟩 Access Token automatically generated and updated in Cell A1!")
            return fresh_token
        else:
            print(f"❌ Auto-Login API Response: {res_data}")
            return None

    except Exception as e:
        print(f"❌ Auto-Login Error: {e}")
        return None

# ==========================================
# LIVE OPTION CHAIN DATA PROCESS ENGINE
# ==========================================
def fetch_and_update_option_chain():
    # A2 cell me har 10 second me time aur confirmation message update hoga
    current_time = datetime.now().strftime("%H:%M:%S")
    try:
        data_sheet.update_acell('A2', f"Render is working - {current_time}")
    except Exception as status_err:
        print(f"⚠️ A2 Update Error: {status_err}")

    try:
        access_token = data_sheet.acell('A1').value
        
        if not access_token or "Test Active" in str(access_token) or len(str(access_token).strip()) < 10:
            print("⚠️ Cell A1 me Access Token nahi hai! Auto-Login triggering...")
            access_token = generate_automatic_access_token()
            if not access_token:
                return

        configuration = upstox_client.Configuration()
        configuration.access_token = str(access_token).strip()
        api_instance = upstox_client.OptionChainApi(upstox_client.ApiClient(configuration))

        instrument_key = "NSE_INDEX|Nifty 50" 
        expiry_date = "" 

        print(f"📊 Fetching Live Option Chain (Nifty 50)...")
        api_response = api_instance.get_option_chain(instrument_key, expiry_date)
        raw_data = api_response.data
        
        output_rows = [
            ["CE Delta", "CE Gamma", "CE Theta", "CE Vega", "CE IV", "CE LTP", "Strike Price", "PE LTP", "PE IV", "PE Delta", "PE Gamma", "PE Theta", "PE Vega"]
        ]
        
        for item in raw_data:
            strike = getattr(item, 'strike_price', 0)
            ce = getattr(item, 'call_options', None)
            pe = getattr(item, 'put_options', None)
            
            ce_ltp, ce_iv, ce_delta, ce_gamma, ce_theta, ce_vega = 0, 0, 0, 0, 0, 0
            pe_ltp, pe_iv, pe_delta, pe_gamma, pe_theta, pe_vega = 0, 0, 0, 0, 0, 0
            
            if ce:
                m_data = getattr(ce, 'market_data', None)
                gks = getattr(ce, 'greeks', None)
                if m_data: ce_ltp, ce_iv = getattr(m_data, 'ltp', 0), getattr(m_data, 'iv', 0)
                if gks: ce_delta, ce_gamma, ce_theta, ce_vega = getattr(gks, 'delta', 0), getattr(gks, 'gamma', 0), getattr(gks, 'theta', 0), getattr(gks, 'vega', 0)
            
            if pe:
                m_data = getattr(pe, 'market_data', None)
                gks = getattr(pe, 'greeks', None)
                if m_data: pe_ltp, pe_iv = getattr(m_data, 'ltp', 0), getattr(m_data, 'iv', 0)
                if gks: pe_delta, pe_gamma, pe_theta, pe_vega = getattr(gks, 'delta', 0), getattr(gks, 'gamma', 0), getattr(gks, 'theta', 0), getattr(gks, 'vega', 0)

            output_rows.append([
                round(ce_delta, 3), round(ce_gamma, 4), round(ce_theta, 2), round(ce_vega, 2), f"{round(ce_iv, 2)}%", ce_ltp,
                strike,
                pe_ltp, f"{round(pe_iv, 2)}%", round(pe_delta, 3), round(pe_gamma, 4), round(pe_theta, 2), round(pe_vega, 2)
            ])
            
        data_sheet.batch_clear(["A10:M1000"])
        data_sheet.update(range_name=f"A10:M{10 + len(output_rows) - 1}", values=output_rows)
        print(f"🎯 Google Sheet updated at A10! (Last Sync: {current_time})")
        
    except ApiException as e:
        if "Unauthorized" in str(e) or e.status == 401:
            print("🔄 Access Token Expired! Auto-generating new Token...")
            generate_automatic_access_token()
        else:
            print(f"❌ Upstox API Exception: {e}")
    except Exception as ex:
        print(f"❌ General error: {ex}")

# ==========================================
# RUN LOOPER
# ==========================================
print("Upstox Live Engine Starting...")

while True:
    fetch_and_update_option_chain()
    print("Waiting for 10 seconds before next sync...\n")
    time.sleep(10)
