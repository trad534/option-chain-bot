import os
import pyotp
from SmartApi import SmartConnect

# Fetch credentials from GitHub Secrets
CLIENT_ID = os.getenv("ANGEL_CLIENT_ID")
PIN = os.getenv("ANGEL_PIN")
API_KEY = os.getenv("ANGEL_API_KEY")
TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET")

def run_login():
    try:
        # Initialize SmartConnect API
        smart_api = SmartConnect(api_key=API_KEY)
        
        # Generate TOTP dynamically
        totp = pyotp.TOTP(TOTP_SECRET).now()
        
        # Authenticate Session
        data = smart_api.generateSession(CLIENT_ID, PIN, totp)
        
        if data.get('status'):
            print("✅ Angel One Login Successful!")
            print(f"JWT Token Generated: {data['data']['jwtToken'][:15]}...")
            return smart_api
        else:
            print(f"❌ Login Failed: {data.get('message')}")
            return None

    except Exception as e:
        print(f"❌ Exception during authentication: {str(e)}")
        return None

if __name__ == "__main__":
    run_login()
