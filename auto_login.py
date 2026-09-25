import os
import time
import pyotp
import gspread
from google.oauth2.service_account import Credentials
from playwright.async_api import async_playwright
import asyncio

async def run():
    user_id = os.environ.get("UPSTOX_USER_ID")
    pin = os.environ.get("UPSTOX_PIN")
    totp_secret = os.environ.get("TOTP_SECRET")
    
    # Clean secret key from any spaces/newlines
    if totp_secret:
        totp_secret = totp_secret.strip()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        print("Navigating to Upstox Login Page...")
        # Login page URL
        login_url = f"https://api.upstox.com/v2/login/authorization/dialog?response_type=code&client_id={os.environ.get('UPSTOX_API_KEY')}&redirect_uri={os.environ.get('UPSTOX_REDIRECT_URI')}"
        await page.goto(login_url)

        print("Waiting for Mobile/User ID input field...")
        # Increase timeout to 60 seconds and support multiple selectors
        user_input = await page.wait_for_selector('input[type="mobileNum"], input[name="userId"], #mobileNum', timeout=60000)
        await user_input.fill(user_id)
        
        # Click Get OTP / Continue button
        get_otp_btn = await page.wait_for_selector('button[type="submit"], #getOtp', timeout=30000)
        await get_otp_btn.click()

        print("Entering OTP / TOTP...")
        totp = pyotp.TOTP(totp_secret)
        current_otp = totp.now()

        otp_input = await page.wait_for_selector('input[type="otpNum"], input[name="otp"]', timeout=30000)
        await otp_input.fill(current_otp)

        # Submit OTP
        continue_btn = await page.wait_for_selector('button[type="submit"]', timeout=30000)
        await continue_btn.click()

        print("Entering PIN...")
        pin_input = await page.wait_for_selector('input[type="password"], input[name="pin"]', timeout=30000)
        await pin_input.fill(pin)

        pin_btn = await page.wait_for_selector('button[type="submit"]', timeout=30000)
        await pin_btn.click()

        # Wait for redirect and extract auth code
        print("Waiting for redirect...")
        await page.wait_for_url("https://*", timeout=60000)
        final_url = page.url

        print("Successfully logged in!")
        await browser.close()

asyncio.run(run())
