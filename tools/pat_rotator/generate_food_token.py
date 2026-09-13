#!/usr/bin/env python3
"""Automated Samsung Food (Whisk) Token Generator.

Uses Playwright with saved Samsung session cookies to authenticate into
https://app.samsungfood.com, extract the active Whisk/Samsung Food authorization
token, verify it against the Food List API, and save it to disk.
"""

import os
import sys
import json
import time
import logging
import requests
from playwright.sync_api import sync_playwright

_LOGGER = logging.getLogger("food_token_generator")


def generate_food_token(
    email: str = "",
    password: str = "",
    output_file: str = "samsung_food_token.txt",
    session_file: str = "smartthings_session.json",
    headless: bool = True,
) -> str:
    """Automate retrieval of the Whisk/Samsung Food token using Playwright."""
    session_path = os.path.expanduser(session_file)
    output_path = os.path.expanduser(output_file)

    captured_tokens = set()

    _LOGGER.info("Starting Samsung Food token generation...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
                "--disable-popup-blocking",
            ],
        )

        context_kwargs = {
            "user_agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            "viewport": {"width": 1920, "height": 1080},
            "locale": "en-US",
            "timezone_id": "America/New_York",
        }

        if os.path.exists(session_path):
            _LOGGER.info("Loading saved Samsung session from %s", session_path)
            context_kwargs["storage_state"] = session_path

        context = browser.new_context(**context_kwargs)

        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)

        page = context.new_page()

        def on_request(request):
            url = request.url
            if any(domain in url for domain in ["api.whisk.com", "api.samsungfood.com", "whisk.com", "samsungfood.com"]):
                headers = request.headers
                auth = headers.get("authorization") or headers.get("x-whisk-token")
                if auth and not auth.startswith("Bearer anon_") and not auth.startswith("Token anon_"):
                    captured_tokens.add(auth)
                    _LOGGER.info("[Token Intercepted] Captured Whisk/Samsung Food Auth Header: %s...", auth[:35])

        page.on("request", on_request)

        try:
            _LOGGER.info("Navigating to https://app.samsungfood.com ...")
            page.goto("https://app.samsungfood.com", timeout=60000)
            time.sleep(5)

            # Check if login button is visible
            header_login_btn = page.locator("button:has-text('Log in'), a:has-text('Log in'), header button:has-text('Log in')").first
            if header_login_btn.is_visible():
                _LOGGER.info("Clicking header 'Log in' button...")
                header_login_btn.click()
                time.sleep(3)

            # Check for "Continue with Samsung"
            popup_obj = None
            try:
                with page.expect_popup(timeout=12000) as popup_info:
                    page.get_by_text("Continue with Samsung", exact=False).first.click()
                popup_obj = popup_info.value
                _LOGGER.info("OAuth popup opened: %s", popup_obj.url)
            except Exception:
                try:
                    page.locator("button:has-text('Samsung')").first.click(force=True)
                except Exception:
                    pass
                time.sleep(3)

            auth_target = popup_obj if popup_obj else page

            try:
                auth_target.wait_for_selector(
                    "input#account, input#iptLgnPlnID, input#password, button:has-text('Agree'), button#signInButton",
                    timeout=20000,
                )
            except Exception:
                pass

            # Fill username/email if prompted
            user_selectors = ["input#account", "input#iptLgnPlnID", "input[type='email']", "input[name='iptLgnPlnID']"]
            for sel in user_selectors:
                loc = auth_target.locator(sel).first
                if loc.is_visible() and email:
                    loc.fill(email)
                    time.sleep(1)
                    nxt = auth_target.locator("button#signInButton, button:has-text('Next'), button[type='submit']").first
                    if nxt.is_visible():
                        nxt.click()
                    else:
                        auth_target.keyboard.press("Enter")
                    time.sleep(3)
                    break

            # Fill password if prompted
            pwd_selectors = ["input#password", "input#iptLgnPD", "input[type='password']", "input[name='iptLgnPD']"]
            for sel in pwd_selectors:
                loc = auth_target.locator(sel).first
                if loc.is_visible() and password:
                    loc.fill(password)
                    time.sleep(1)
                    sign_in_btn = auth_target.locator("button#signInButton, button:has-text('Sign in'), button[type='submit']").first
                    if sign_in_btn.is_visible():
                        sign_in_btn.click()
                    else:
                        auth_target.keyboard.press("Enter")
                    time.sleep(4)
                    break

            # Check for consent / Agree buttons
            for _ in range(8):
                if popup_obj and popup_obj.is_closed():
                    break
                clicked_agree = False
                for sel in ["button:has-text('Agree')", "button:has-text('Allow')", "button:has-text('Accept')", "button#agree"]:
                    try:
                        loc = auth_target.locator(sel).first
                        if loc.is_visible():
                            _LOGGER.info("Clicking OAuth consent button: %s", sel)
                            loc.click()
                            clicked_agree = True
                            time.sleep(3)
                            break
                    except Exception:
                        pass
                if clicked_agree:
                    break
                time.sleep(1)

            if popup_obj:
                try:
                    popup_obj.wait_for_event("close", timeout=15000)
                except Exception:
                    pass

            time.sleep(5)

            # Navigate to pantry / food list to force token generation
            for path in ["/pantry", "/food-list"]:
                try:
                    page.goto(f"https://app.samsungfood.com{path}", timeout=20000)
                    time.sleep(3)
                except Exception:
                    pass

            # Inspect localStorage
            try:
                local_storage = page.evaluate("() => JSON.stringify(window.localStorage)")
                ls_data = json.loads(local_storage)
                for k, v in ls_data.items():
                    if any(t in k.lower() for t in ["token", "auth", "whisk", "jwt", "user"]):
                        val_str = str(v)
                        if "bearer " in val_str.lower() or len(val_str) > 30:
                            captured_tokens.add(val_str)
            except Exception:
                pass

            # Inspect cookies
            try:
                cookies = context.cookies()
                for c in cookies:
                    if any(k in c.get("name", "").lower() for k in ["auth", "token", "whisk", "jwt"]):
                        captured_tokens.add(c.get("value"))
                if session_path:
                    context.storage_state(path=session_path)
                    _LOGGER.info("Saved updated browser session to %s", session_path)
            except Exception as e:
                _LOGGER.debug("Could not save session state: %s", e)

        except Exception as err:
            _LOGGER.warning("Browser notice during food token fetch: %s", err)
        finally:
            browser.close()

    # Validate candidate tokens against Whisk Food List API
    valid_token = None
    for candidate in captured_tokens:
        clean_auth = candidate if candidate.startswith("Bearer ") or candidate.startswith("Token ") else f"Bearer {candidate}"
        raw_token = candidate.replace("Bearer ", "").replace("Token ", "").strip()
        test_headers = {
            "Authorization": clean_auth,
            "x-whisk-token": raw_token,
            "Accept": "application/json",
        }
        try:
            r = requests.get("https://api.whisk.com/foodlist/v2", headers=test_headers, params={"page_size": 10}, timeout=10)
            if r.ok:
                data = r.json()
                total = int(data.get("paging", {}).get("total", 0) or 0)
                valid_token = raw_token
                _LOGGER.info("Successfully validated Samsung Food token! (Total account items: %d)", total)
                break
        except Exception:
            pass

    if not valid_token and captured_tokens:
        # Fallback to first non-empty token
        valid_token = list(captured_tokens)[0].replace("Bearer ", "").replace("Token ", "").strip()

    if not valid_token:
        raise RuntimeError("Failed to capture a valid Samsung Food (Whisk) token.")

    # Save token to file
    try:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(f"{valid_token}\n")
        _LOGGER.info("Saved Samsung Food token to %s", output_path)
    except Exception as e:
        _LOGGER.warning("Could not write token file %s: %s", output_path, e)

    return valid_token


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Generate Samsung Food Token")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    parser.add_argument("--output", default="samsung_food_token.txt", help="Output token file")
    parser.add_argument("--session", default="smartthings_session.json", help="Session file")
    parser.add_argument("--headless", action="store_true", default=True, help="Run headless")
    args = parser.parse_args()

    email = ""
    password = ""
    if os.path.exists(args.config):
        with open(args.config) as f:
            cfg = json.load(f)
            email = cfg.get("samsung_email", "")
            password = cfg.get("samsung_password", "")

    token = generate_food_token(
        email=email,
        password=password,
        output_file=args.output,
        session_file=args.session,
        headless=args.headless,
    )
    print(f"\nCaptured Food Token: {token[:10]}...{token[-10:]}")
