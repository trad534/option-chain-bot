import os
import json
import urllib.request
import pandas as pd
import pyotp
from SmartApi import SmartConnect

CLIENT_ID = os.getenv("ANGEL_CLIENT_ID")
PIN = os.getenv("ANGEL_PIN")
API_KEY = os.getenv("ANGEL_API_KEY")
TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET")

def get_option_chain():
    try:
        # 1. Initialize & Login
        smart_api = SmartConnect(api_key=API_KEY)
        totp = pyotp.TOTP(TOTP_SECRET).now()
        data = smart_api.generateSession(CLIENT_ID, PIN, totp)
        
        if not data.get('status'):
            print(f"❌ Login Failed: {data.get('message')}")
            return
            
        print("✅ Login Successful!")

        # 2. Fetch Master Scrip List from Angel One
        url = "https://margincalculator.angelbroking.com/open-api/tokenMaster/OpenAPIScripMaster.json"
        print("📥 Downloading Scrip Master Data...")
        response = urllib.request.urlopen(url)
        scrip_data = json.loads(response.read().decode())
        df = pd.DataFrame(scrip_data)

        # 3. Filter for NIFTY Options
        nifty_options = df[
            (df['name'] == 'NIFTY') & 
            (df['instrumenttype'].isin(['OPTIDX'])) &
            (df['exch_seg'] == 'NFO')
        ].copy()

        print(f"📊 Total Active NIFTY Option Contracts Found: {len(nifty_options)}")

        # 4. Save to CSV
        output_file = "nifty_option_chain.csv"
        nifty_options[['token', 'symbol', 'name', 'expiry', 'strike', 'lotsize', 'instrumenttype']].to_csv(output_file, index=False)
        print(f"🎉 Option Chain master dataset saved successfully to '{output_file}'!")

    except Exception as e:
        print(f"❌ Error during processing: {str(e)}")

if __name__ == "__main__":
    get_option_chain()
