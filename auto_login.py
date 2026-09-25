import os
import time
import pyotp
import asyncio
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
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        print("Navigating to Upstox API Auth URL...")
        login_url = f"https://api.upstox.com/v2/login/authorization/dialog?response_type=code&client_id={client_id}&redirect_uri={redirect_uri}"
        
        # Load page with domcontentloaded instead of networkidle
        await page.goto(login_url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)

        print("Waiting for Mobile/User ID input field...")
        # Broad selectors for input field
        user_input = await page.wait_for_selector('input[type="tel"], input[type="text"], input[name="userId"], input[type="mobileNum"]', timeout=30000)
        await user_input.fill(user_id)
        
        # Click Get OTP / Continue
        get_otp_btn = await page.wait_for_selector('button[type="submit"], button:has-text("Get OTP"), button:has-text("Continue")', timeout=30000)
        await get_otp_btn.click()
        await page.wait_for_timeout(2000)

        print("Generating and Entering TOTP...")
        totp = pyotp.TOTP(totp_secret)
        current_otp = totp.now()

        otp_input = await page.wait_for_selector('input[type="tel"], input[type="text"], input[name="otp"], input[type="otpNum"]', timeout=30000)
        await otp_input.fill(current_otp)

        continue_btn = await page.wait_for_selector('button[type="submit"], button:has-text("Continue")', timeout=30000)
        await continue_btn.click()
        await page.wait_for_timeout(2000)

        print("Entering PIN...")
        pin_input = await page.wait_for_selector('input[type="password"]', timeout=30000)
        await pin_input.fill(pin)

        pin_btn = await page.wait_for_selector('button[type="submit"], button:has-text("Continue")', timeout=30000)
        await pin_btn.click()

        print("Waiting for final redirect...")
        await page.wait_for_url("https://*", timeout=60000)
        print("Login completed successfully! Final URL:", page.url)

        await browser.close()

asyncio.run(run())
