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
    
    if totp_secret:
        totp_secret = totp_secret.strip()

    async with async_playwright() as p:
        # Launch browser with user-agent to avoid bot blocking
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        print("Navigating to Upstox Login Page...")
        # Direct Upstox Mobile/User Login URL
        await page.goto("https://login.upstox.com/", wait_until="networkidle")

        print("Waiting for Mobile/User ID input field...")
        # Broad selector matching Upstox current interface
        user_input = await page.wait_for_selector('input[type="tel"], input[name="userId"], input[type="text"]', timeout=30000)
        await user_input.fill(user_id)
        
        # Click Get OTP or Continue button
        get_otp_btn = await page.wait_for_selector('button:has-text("Get OTP"), button:has-text("Continue"), button[type="submit"]', timeout=30000)
        await get_otp_btn.click()

        print("Generating and Entering TOTP...")
        await asyncio.sleep(2)
        totp = pyotp.TOTP(totp_secret)
        current_otp = totp.now()

        otp_input = await page.wait_for_selector('input[type="tel"], input[type="text"], input[name="otp"]', timeout=30000)
        await otp_input.fill(current_otp)

        continue_btn = await page.wait_for_selector('button:has-text("Continue"), button[type="submit"]', timeout=30000)
        await continue_btn.click()

        print("Entering PIN...")
        await asyncio.sleep(2)
        pin_input = await page.wait_for_selector('input[type="password"]', timeout=30000)
        await pin_input.fill(pin)

        pin_btn = await page.wait_for_selector('button:has-text("Continue"), button[type="submit"]', timeout=30000)
        await pin_btn.click()

        print("Login completed successfully!")
        await browser.close()

asyncio.run(run())
