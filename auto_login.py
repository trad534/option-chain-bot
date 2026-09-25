import os
import time
import pyotp
import gspread
import requests
from playwright.sync_api import sync_playwright

USER_ID = os.getenv("UPSTOX_USER_ID")
PIN = os.getenv("UPSTOX_PIN")
TOTP_SECRET = os.getenv("TOTP_SECRET")

API_KEY = "399fd5d0-cb80-4983-bdd8-bcfc05091459"
API_SECRET = "3ll8j7cxaf"
REDIRECT_URI = "https://127.0.0"

GOOGLE_SHEET_NAME = "6 LTP CALCULATOR"
MAIN_SHEET_NAME = "upstox get data"

def run_auto_login():
    print("🚀 Starting Headless Browser Auto-Login...")
    
    gc = gspread.service_account(filename="Credentials.json")
    spreadsheet = gc.open(GOOGLE_SHEET_NAME)
    data_sheet = spreadsheet.worksheet(MAIN_SHEET_NAME)

    totp = pyotp.TOTP(TOTP_SECRET.replace(" ", ""))
    otp_code = totp.now()

    auth_url = f"https://api.upstox.com/v2/login/authorization/dialog?response_type=code&client_id={API_KEY}&redirect_uri={REDIRECT_URI}"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(auth_url)

        page.wait_for_selector('input[type="mobileNum"]', timeout=20000)
        page.fill('input[type="mobileNum"]', USER_ID)
        page.click('button[type="submit"]')

        page.wait_for_selector('input[type="text"]', timeout=15000)
        page.fill('input[type="text"]', otp_code)
        page.click('button[type="submit"]')

        page.wait_for_selector('input[type="password"]', timeout=15000)
        page.fill('input[type="password"]', PIN)
        page.click('button[type="submit"]')

        page.wait_for_url("https://127.0.0*", timeout=20000)
        final_url = page.url
        code = final_url.split("code=")[1].split("&")[0]

        token_url = "https://api.upstox.com/v2/login/auth/token"
        headers = {'accept': 'application/json', 'Content-Type': 'application/x-www-form-urlencoded'}
        payload = {
            'code': code,
            'client_id': API_KEY,
            'client_secret': API_SECRET,
            'redirect_uri': REDIRECT_URI,
            'grant_type': 'authorization_code'
        }
        res = requests.post(token_url, headers=headers, data=payload).json()
        
        access_token = res.get('access_token')
        if access_token:
            data_sheet.update_acell('A1', access_token)
            print("🎉 SUCCESS: Fresh Access Token Google Sheet Cell A1 me update ho gaya hai!")
        else:
            print(f"❌ Token error: {res}")

        browser.close()

if __name__ == "__main__":
    run_auto_login()
