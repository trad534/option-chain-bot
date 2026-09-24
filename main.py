import os
import time
import requests
import pyotp
import gspread
from google.oauth2.service_account import Credentials
import google.auth.transport.requests
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

# Dummy Web Server for Render Free Tier
class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Option Chain Bot is Running Live!")

def run_dummy_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), SimpleHTTPRequestHandler)
    server.serve_forever()

threading.Thread(target=run_dummy_server, daemon=True).start()

# CONFIGURATION & LOGIN PARAMETERS
API_KEY = "399fd5d0-cb80-4983-bdd8-bcfc0509180b"
API_SECRET = "3ll8j7cxaf"
REDIRECT_URI = "https://127.0.0.1"

USER_ID = "3CAGY5"
PIN = "011996"
TOTP_SECRET = "YOUR_TOTP_SECRET_KEY_HERE"

GOOGLE_SHEET_NAME = "6 LTP CALCULATOR"
MAIN_SHEET_NAME = "upstox get data"

try:
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    
    # Authenticate credentials with Google Auth
    creds = Credentials.from_service_account_file('Credentials.json', scopes=scopes)
    
    # Refresh token explicitly to fix JWT signature error
    request = google.auth.transport.requests.Request()
    creds.refresh(request)
    
    gc = gspread.authorize(creds)

    spreadsheet = gc.open(GOOGLE_SHEET_NAME)
    data_sheet = spreadsheet.worksheet(MAIN_SHEET_NAME)
    print("==================================================")
    print("SUCCESS: Google Sheet Connected Successfully!")
    print("==================================================")
except Exception as e:
    print(f"Google Sheet Connection Error: {e}")
    exit()

def generate_automatic_access_token():
    if "YOUR_TOTP_SECRET" in TOTP_SECRET or not TOTP_SECRET:
        print("🔴 Cannot Auto-Login: TOTP Secret Key missing")
        return None
    try:
        totp = pyotp.TOTP(TOTP_SECRET)
        otp = totp.now()
        auth_url = f"https://api.upstox.com/v2/login/authorization/dialog?response_type=code&client_id={API_KEY}&redirect_uri={REDIRECT_URI}"
        headers = {'accept': 'application/json', 'Content-Type': 'application/x-www-form-urlencoded'}
        
        session = requests.Session()
        session.get(auth_url)
        
        login_data = {"property2": USER_ID, "property3": PIN, "otp": otp}
        res = session.post("https://api.upstox.com/v2/login/auth/token", data=login_data, headers=headers)
        
        if res.status_code == 200:
            code = res.json().get('data', {}).get('code')
            token_url = "https://api.upstox.com/v2/login/authorization/token"
            token_data = {
                'code': code,
                'client_id': API_KEY,
                'client_secret': API_SECRET,
                'redirect_uri': REDIRECT_URI,
                'grant_type': 'authorization_code'
            }
            token_res = requests.post(token_url, data=token_data, headers=headers)
            if token_res.status_code == 200:
                return token_res.json().get('access_token')
    except Exception as e:
        print(f"Auto Login Error: {e}")
    return None

access_token = generate_automatic_access_token()

if access_token:
    print("SUCCESS: Upstox Access Token Generated!")
else:
    print("WARNING: Running without active Upstox token. Check TOTP configuration.")

print("Bot deployed and active...")
while True:
    time.sleep(10)
