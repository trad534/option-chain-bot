import os
import requests
import pyotp
import urllib.parse
import gspread
from google.oauth2.service_account import Credentials

def get_access_token():
    user_id = os.environ.get("UPSTOX_USER_ID")
    pin = os.environ.get("UPSTOX_PIN")
    totp_secret = os.environ.get("TOTP_SECRET").strip()
    api_key = os.environ.get("UPSTOX_API_KEY")
    api_secret = os.environ.get("UPSTOX_SECRET_KEY")
    redirect_uri = os.environ.get("UPSTOX_REDIRECT_URI")

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json"
    })

    print("Step 1: Sending Mobile/User ID...")
    # Step 1: Send OTP request
    otp_res = session.post(
        "https://api.upstox.com/v2/login/open/v4/auth/1fa/otp/generate",
        json={"mobileNumber": user_id}
    )
    
    if otp_res.status_code != 200:
        print("Error sending OTP request:", otp_res.text)
        raise Exception("OTP generation failed")

    print("Step 2: Validating TOTP...")
    totp = pyotp.TOTP(totp_secret)
    current_otp = totp.now()

    # Step 2: Validate TOTP
    val_res = session.post(
        "https://api.upstox.com/v2/login/open/v4/auth/1fa/otp/validate",
        json={"otp": current_otp}
    )

    if val_res.status_code != 200:
        print("Error validating TOTP:", val_res.text)
        raise Exception("TOTP validation failed")

    print("Step 3: Validating PIN...")
    # Step 3: Validate PIN
    pin_res = session.post(
        "https://api.upstox.com/v2/login/open/v4/auth/2fa/pin/validate",
        json={"pin": pin}
    )

    if pin_res.status_code != 200:
        print("Error validating PIN:", pin_res.text)
        raise Exception("PIN validation failed")

    print("Step 4: Authorizing API Token...")
    # Step 4: OAuth Authorization
    auth_url = f"https://api.upstox.com/v2/login/authorization/dialog?response_type=code&client_id={api_key}&redirect_uri={redirect_uri}"
    auth_res = session.get(auth_url, allow_redirects=False)

    location = auth_res.headers.get("Location")
    if not location:
        raise Exception("Failed to get authorization code redirect")

    parsed_url = urllib.parse.urlparse(location)
    code = urllib.parse.parse_qs(parsed_url.query).get("code")[0]

    print("Step 5: Fetching Access Token...")
    # Step 5: Get Access Token
    token_url = "https://api.upstox.com/v2/login/authorization/token"
    token_payload = {
        "code": code,
        "client_id": api_key,
        "client_secret": api_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code"
    }

    token_res = session.post(token_url, data=token_payload)
    token_data = token_res.json()

    access_token = token_data.get("access_token")
    print("Access Token Generated Successfully!")
    return access_token

if __name__ == "__main__":
    token = get_access_token()
    print("TOKEN_LENGTH:", len(token))
