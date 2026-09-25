import os
import time
import pyotp
import asyncio
import urllib.parse
import gspread
from google.oauth2.service_account import Credentials
from playwright.async_api import async_playwright

async def run():
    user_id = os.environ.get("UPSTOX_USER_ID")
    pin = os.environ.get("UPSTOX_PIN")
    totp_secret = os.environ.get("TOTP_SECRET")
    client_id = os.environ.get("UPSTOX_API_KEY")
    redirect_uri = os.environ.get("UPSTOX_REDIRECT_URI")
    
    if totp_secret:
        totp_secret = totp_secret.strip()

    async with async_playwright() as p:
        # Launch browser with anti-bot evasion settings
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-web-security"
            ]
        )
        
        # Emulate Real Mobile Browser (Upstox serves simple DOM for mobile)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1",
            viewport={"width": 390, "height": 844},
            is_mobile=True,
            has_touch=True
        )
        page = await context.new_page()

        print("Navigating to Upstox Login Page...")
        login_url = f"https://api.upstox.com/v2/login/authorization/dialog?response_type=code&client_id={client_id}&redirect_uri={redirect_uri}"
        
        await page.goto(login_url, wait_until="commit", timeout=60000)
        await asyncio.sleep(5)

        print("Filling Mobile / User ID...")
        # Mobile view input selector
        mobile_input = page.locator('input').first
        await mobile_input.wait_for(state="visible", timeout=30000)
        await mobile_input.fill(user_id)

        # Click Get OTP
        print("Clicking Get OTP...")
        btn = page.locator('button').first
        await btn.click()
        await asyncio.sleep(3)

        print("Entering TOTP...")
        totp = pyotp.TOTP(totp_secret)
        current_otp = totp.now()

        otp_input = page.locator('input').first
        await otp_input.wait_for(state="visible", timeout=30000)
        await otp_input.fill(current_otp)

        btn = page.locator('button').first
        await btn.click()
        await asyncio.sleep(3)

        print("Entering PIN...")
        pin_input = page.locator('input').first
        await pin_input.wait_for(state="visible", timeout=30000)
        await pin_input.fill(pin)

        btn = page.locator('button').first
        await btn.click()

        print("Waiting for final redirect...")
        await page.wait_for_url("https://*", timeout=60000)
        final_url = page.url
        print("Successfully Logged In! Final URL:", final_url)

        await browser.close()

asyncio.run(run())
