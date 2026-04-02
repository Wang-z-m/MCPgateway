"""
Dashboard verification script using Playwright.
Run with: python scripts/verify_dashboard.py

Requires: pip install playwright && playwright install chromium
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

try:
    from playwright.async_api import async_playwright
except ImportError:
    print("Install Playwright first: pip install playwright && playwright install chromium")
    sys.exit(1)

BASE_URL = "http://127.0.0.1:8000"
SCREENSHOT_DIR = Path(__file__).resolve().parents[1] / "verification_screenshots"


async def main() -> None:
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    print(f"Screenshots will be saved to: {SCREENSHOT_DIR}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await context.new_page()

        try:
            # 1. Navigate to dashboard and capture initial load
            print("\n1. Loading dashboard...")
            await page.goto(f"{BASE_URL}/dashboard", wait_until="networkidle")
            await page.wait_for_timeout(1500)  # Allow dynamic content to load
            await page.screenshot(path=SCREENSHOT_DIR / "01_dashboard_initial.png")
            print("   Screenshot: 01_dashboard_initial.png")

            # 2. Click "MCP 调试" tab
            print("\n2. Clicking MCP 调试 tab...")
            await page.click('button[data-tab="playground"]')
            await page.wait_for_timeout(1500)  # Load tools
            await page.screenshot(path=SCREENSHOT_DIR / "02_mcp_playground.png")
            print("   Screenshot: 02_mcp_playground.png")

            # 3. Select tools/call, get_user, set args, send request
            print("\n3. Sending tools/call for get_user with {user_id: 1}...")
            await page.select_option("#pgMethod", "tools/call")
            await page.wait_for_timeout(500)

            # Ensure get_user is selected
            try:
                await page.select_option("#pgToolName", "get_user")
            except Exception:
                # May need to wait for options to load
                await page.wait_for_timeout(1000)
                await page.select_option("#pgToolName", "get_user")

            await page.fill("#pgArgs", '{"user_id": 1}')
            await page.click("#pgSendBtn")
            await page.wait_for_timeout(2000)  # Wait for response
            await page.screenshot(path=SCREENSHOT_DIR / "03_get_user_response.png")
            print("   Screenshot: 03_get_user_response.png")

        except Exception as e:
            print(f"\nError: {e}")
            await page.screenshot(path=SCREENSHOT_DIR / "error_state.png")
            print("   Error screenshot saved: error_state.png")
        finally:
            await browser.close()

    print("\nVerification complete.")


if __name__ == "__main__":
    asyncio.run(main())
