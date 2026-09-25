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
        # Launch browser without headless detection
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720}
        )
        page = await context.new_page()

        print("Navigating to Upstox API Auth URL...")
        login_url = f"https://api.upstox.com/v2/login/authorization/dialog?response_type=code&client_id={client_id}&redirect_uri={redirect_uri}"
        
        await page.goto(login_url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(5)

        # Save screenshot to debug exact UI
        await page.screenshot(path="login_page_debug.png")
        print("Saved debug screenshot: login_page_debug.png")

        # Generic locator strategy: find any visible input element
        print("Searching for any visible input field...")
        inputs = page.locator("input")
        count = await inputs.count()
        print(f"Total inputs found: {count}")

        if count > 0:
            first_input = inputs.first
            await first_input.fill(user_id)
            print("Filled User ID successfully.")
        else:
            raise Exception("No input elements found on page.")

        # Click Get OTP / Submit
        button = page.locator("button").first
        await button.click()
        await asyncio.sleep(3)

        # Enter TOTP
        print("Entering TOTP...")
        totp = pyotp.TOTP(totp_secret)
        current_otp = totp.now()

        otp_input = page.locator("input").first
        await otp_input.fill(current_otp)
        await page.locator("button").first.click()
        await asyncio.sleep(3)

        # Enter PIN
        print("Entering PIN...")
        pin_input = page.locator("input").first
        await pin_input.fill(pin)
        await page.locator("button").first.click()

        print("Waiting for final redirect...")
        await page.wait_for_url("https://*", timeout=60000)
        print("Login completed successfully! Final URL:", page.url)

        await browser.close()

asyncio.run(run())
