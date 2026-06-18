#!/usr/bin/env python3
"""Samsung Food (Whisk) AI Food Manager Explorer.

Uses Playwright with saved Samsung session cookies to authenticate into
https://app.samsungfood.com, intercept the Whisk/Samsung Food authorization token,
query the Food List API (https://api.whisk.com/foodlist/v2), and save your fridge's
AI inventory (including item names like 'Pesto', expiry dates, and thumbnail crop URLs)
to /tmp/samsung_food_inventory_dump.json.
"""

import os
import sys
import json
import time
import logging
import argparse
import requests
from playwright.sync_api import sync_playwright

log_format = "%(asctime)s [%(levelname)s] %(message)s"
log_handlers = [
    logging.StreamHandler(sys.stdout),
    logging.FileHandler("/tmp/samsung_food_inspector.log", mode="w", encoding="utf-8"),
]
logging.basicConfig(level=logging.INFO, format=log_format, handlers=log_handlers)
_LOGGER = logging.getLogger("samsung_food_inspector")


def inspect_samsung_food(
    email: str = "",
    password: str = "",
    session_file: str = "smartthings_session.json",
    output_file: str = "/tmp/samsung_food_inventory_dump.json",
    headless: bool = True,
):
    session_path = os.path.expanduser(session_file)

    captured_tokens = set()
    captured_requests = []

    _LOGGER.info("Starting Samsung Food (Whisk) Inspector...")
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
            _LOGGER.info("Loading saved Samsung session cookies from %s", session_path)
            context_kwargs["storage_state"] = session_path

        context = browser.new_context(**context_kwargs)

        # Remove webdriver flag
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)

        page = context.new_page()

        # Intercept network requests to capture Whisk / Samsung Food API tokens and responses
        def on_request(request):
            url = request.url
            if any(domain in url for domain in ["api.whisk.com", "api.samsungfood.com", "whisk.com", "samsungfood.com"]):
                headers = request.headers
                auth = headers.get("authorization") or headers.get("x-whisk-token")
                if auth and not auth.startswith("Bearer anon_") and not auth.startswith("Token anon_"):
                    captured_tokens.add(auth)
                    _LOGGER.info("[Token Intercepted] Captured Whisk/Samsung Food Auth Header: %s...", auth[:35])

        def on_response(response):
            url = response.url
            if any(k in url.lower() for k in ["foodlist", "inventory", "fridge", "pantry", "shopping-list", "item"]):
                try:
                    if response.ok and "application/json" in response.headers.get("content-type", ""):
                        data = response.json()
                        captured_requests.append({
                            "url": url,
                            "status": response.status,
                            "data": data,
                        })
                        _LOGGER.info("[Payload Captured] Intercepted endpoint: %s (Status: %s)", url, response.status)
                except Exception:
                    pass

        page.on("request", on_request)
        page.on("response", on_response)

        try:
            _LOGGER.info("Navigating to https://app.samsungfood.com ...")
            page.goto("https://app.samsungfood.com", timeout=60000)
            time.sleep(5)
            page.screenshot(path="/tmp/samsung_food_step1.png")

            # Click "Log in" button in the top right header
            header_login_btn = page.locator("button:has-text('Log in'), a:has-text('Log in'), header button:has-text('Log in')").first
            if header_login_btn.is_visible():
                _LOGGER.info("Clicking header 'Log in' button...")
                header_login_btn.click()
                time.sleep(3)

            page.screenshot(path="/tmp/samsung_food_step2.png")

            # Click "Continue with Samsung" and expect OAuth popup
            _LOGGER.info("Clicking 'Continue with Samsung' to open OAuth popup...")
            popup_obj = None
            try:
                with page.expect_popup(timeout=15000) as popup_info:
                    page.get_by_text("Continue with Samsung", exact=False).first.click()
                popup_obj = popup_info.value
                _LOGGER.info("OAuth popup successfully opened: %s", popup_obj.url)
            except Exception as e:
                _LOGGER.warning("expect_popup notice: %s. Trying fallback...", e)
                # Fallback click
                try:
                    page.locator("button:has-text('Samsung')").first.click(force=True)
                except Exception:
                    pass
                time.sleep(4)

            auth_target = popup_obj if popup_obj else page

            # Wait for login inputs or Agree button to appear
            try:
                auth_target.wait_for_selector(
                    "input#account, input#iptLgnPlnID, input#password, button:has-text('Agree'), button#signInButton",
                    timeout=25000,
                )
            except Exception:
                pass

            # If redirected to Samsung sign-in portal on either popup or main page, complete credentials
            if "account.samsung.com" in auth_target.url or "signInGate" in auth_target.url or "whisk.com" in auth_target.url:
                _LOGGER.info("Completing Samsung Single Sign-On (SSO)...")
                
                # 1. Email Field
                email_selectors = ["input#account", "input#iptLgnPlnID", "input[type='email']", "input[name='iptLgnPlnID']"]
                for sel in email_selectors:
                    loc = auth_target.locator(sel).first
                    if loc.is_visible() and email:
                        loc.fill(email)
                        time.sleep(1)
                        next_btn = auth_target.locator("button#signInButton, button:has-text('Next')").first
                        if next_btn.is_visible():
                            next_btn.click()
                        else:
                            auth_target.keyboard.press("Enter")
                        break

                time.sleep(3)

                # Check for reCAPTCHA challenge
                recaptcha_el = auth_target.locator("iframe[src*='recaptcha'], div.g-recaptcha, iframe[title*='reCAPTCHA'], div#rc-anchor-container").first
                if recaptcha_el.is_visible():
                    if headless:
                        _LOGGER.error("==========================================================================")
                        _LOGGER.error(" Google reCAPTCHA ('I'm not a robot') detected on Samsung Account sign-in.")
                        _LOGGER.error(" Headless automation cannot solve visual CAPTCHA challenges.")
                        _LOGGER.error(" Please run with --no-headless in a GUI environment to complete login,")
                        _LOGGER.error(" or wait for Samsung's security rate-limiter cooldown to expire.")
                        _LOGGER.error("==========================================================================")
                        auth_target.screenshot(path="/tmp/popup_recaptcha.png")
                        return
                    else:
                        _LOGGER.info("Google reCAPTCHA detected. Please solve the CAPTCHA challenge in the browser window to continue...")

                # 2. Wait for Password Field to appear
                try:
                    auth_target.wait_for_selector(
                        "input#password, input#iptLgnPD, input[type='password']",
                        timeout=20000,
                    )
                except Exception:
                    pass

                # Check again if reCAPTCHA blocked the password field
                if recaptcha_el.is_visible() and not auth_target.locator("input#password, input#iptLgnPD, input[type='password']").first.is_visible():
                    if headless:
                        _LOGGER.error("reCAPTCHA challenge blocked password entry. Exiting headless session.")
                        return

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

                # 3. Check for "Agree" / "Allow" consent screen in popup
                for _ in range(12):
                    if popup_obj and popup_obj.is_closed():
                        break
                    clicked_agree = False
                    for sel in ["button:has-text('Agree')", "button:has-text('Allow')", "button:has-text('Accept')", "button#agree"]:
                        try:
                            loc = auth_target.locator(sel).first
                            if loc.is_visible():
                                _LOGGER.info("Clicking OAuth consent button: %s ...", sel)
                                loc.click()
                                clicked_agree = True
                                time.sleep(3)
                                break
                        except Exception:
                            pass
                    if clicked_agree:
                        break
                    time.sleep(1)

                # 4. Interstitial "Not now" check in popup
                for sel in ["button:has-text('Not now')", "button[data-log-id='not-now']", "a:has-text('Not now')", "button:has-text('Later')"]:
                    try:
                        loc = auth_target.locator(sel).first
                        if loc.is_visible():
                            loc.click()
                            time.sleep(2)
                            break
                    except Exception:
                        pass

            # If popup opened, wait for it to finish and close
            if popup_obj:
                try:
                    popup_obj.wait_for_event("close", timeout=20000)
                except Exception:
                    pass

            # Wait for main page to update with logged-in user profile
            time.sleep(8)

            # Navigate to Food List / Pantry / Fridge views
            for path in ["/pantry", "/food-list", "/lists", "/saved"]:
                target_url = f"https://app.samsungfood.com{path}"
                try:
                    page.goto(target_url, timeout=20000)
                    time.sleep(4)
                except Exception:
                    pass

            # Extract any tokens from localStorage and cookies
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

            # Extract from cookies and save session state
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
            _LOGGER.warning("Browser notice: %s", err)
        finally:
            browser.close()

    # Phase 2: Direct API querying with candidate tokens
    inventory_results = {
        "captured_tokens_count": len(captured_tokens),
        "captured_browser_endpoints": captured_requests,
        "all_items": [],
        "present_items": [],
        "consumed_items": [],
        "api_endpoints": {},
    }

    # Find the active token that returns items
    valid_token = None
    all_food_items = []

    for candidate in captured_tokens:
        clean_auth = candidate if candidate.startswith("Bearer ") or candidate.startswith("Token ") else f"Bearer {candidate}"
        raw_token = candidate.replace("Bearer ", "").replace("Token ", "").strip()
        test_headers = {
            "Authorization": clean_auth,
            "x-whisk-token": raw_token,
            "Accept": "application/json",
        }

        try:
            r = requests.get("https://api.whisk.com/foodlist/v2", headers=test_headers, params={"page_size": 200}, timeout=10)
            if r.ok:
                data = r.json()
                total = int(data.get("paging", {}).get("total", 0) or 0)
                if total > 0 or len(data.get("items", [])) > 0:
                    valid_token = candidate
                    _LOGGER.info("Authenticated successfully with valid Samsung Food user token! (Total account items: %d)", total)
                    break
        except Exception:
            pass

    if not valid_token and captured_tokens:
        valid_token = list(captured_tokens)[0]

    if valid_token:
        clean_auth = valid_token if valid_token.startswith("Bearer ") or valid_token.startswith("Token ") else f"Bearer {valid_token}"
        raw_token = valid_token.replace("Bearer ", "").replace("Token ", "").strip()
        
        # Save token for direct REST polling (zero browser logins / zero emails)
        script_dir = os.path.dirname(os.path.abspath(__file__))
        token_out_path = os.path.join(script_dir, "samsung_food_token.txt")
        try:
            with open(token_out_path, "w") as f:
                f.write(f"{raw_token}\n")
            _LOGGER.info("Saved reusable Whisk/Samsung Food API token to %s", token_out_path)
        except Exception as e:
            _LOGGER.debug("Could not write token file: %s", e)

        req_headers = {
            "Authorization": clean_auth,
            "x-whisk-token": raw_token,
            "Accept": "application/json",
        }

        # Paginate through all items
        _LOGGER.info("Fetching complete food inventory across all pages...")
        all_food_items = []
        after_cursor = ""
        page_num = 1

        while True:
            params = {}
            if after_cursor:
                params["paging.cursors.after"] = after_cursor

            try:
                r = requests.get("https://api.whisk.com/foodlist/v2", headers=req_headers, params=params, timeout=12)
                if not r.ok:
                    _LOGGER.warning("Page %d fetch failed: HTTP %s", page_num, r.status_code)
                    break
                
                data = r.json()
                page_items = data.get("items", [])
                all_food_items.extend(page_items)
                _LOGGER.info("Page %d: loaded %d items (total so far: %d)", page_num, len(page_items), len(all_food_items))

                paging = data.get("paging", {})
                next_cursor = paging.get("cursors", {}).get("after")
                if next_cursor and next_cursor != after_cursor and len(page_items) > 0:
                    after_cursor = next_cursor
                    page_num += 1
                else:
                    break
            except Exception as err:
                _LOGGER.error("Error during pagination: %s", err)
                break

        inventory_results["all_items"] = all_food_items

        # Classify into Present vs Consumed
        for itm in all_food_items:
            content = itm.get("content", {})
            status = content.get("presence_status", "")
            is_consumed = bool(content.get("consumed_at")) or bool(content.get("deleted_at")) or status == "PRESENCE_STATUS_CONSUMED"

            parsed = {
                "id": itm.get("id"),
                "name": content.get("name"),
                "presence_status": status,
                "location": content.get("location"),
                "added_at": content.get("added_at"),
                "expiration_date": content.get("expiration_date") or content.get("days_to_expire"),
                "image_url": content.get("image_url") or content.get("photo_url"),
                "ai_generated": content.get("ai_generated", False),
                "ai_suggested_names": content.get("ai_suggested_names", []),
            }

            if not is_consumed or status in ["PRESENCE_STATUS_EXISTING", "PRESENCE_STATUS_PRESENT"]:
                inventory_results["present_items"].append(parsed)
            else:
                inventory_results["consumed_items"].append(parsed)

    # Save final JSON dump
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(inventory_results, f, indent=2)

    _LOGGER.info("==========================================================")
    _LOGGER.info(" Complete Samsung Food dump saved to: %s", output_file)
    _LOGGER.info(" Total items: %d | Currently Present: %d | Consumed: %d", len(inventory_results["all_items"]), len(inventory_results["present_items"]), len(inventory_results["consumed_items"]))
    _LOGGER.info("==========================================================")

    # Scan and print detected present food items
    print()
    print("=" * 60)
    print(f"  CURRENTLY ACTIVE FOOD ITEMS IN FRIDGE ({len(inventory_results['present_items'])})")
    print("=" * 60)

    for item in inventory_results["present_items"]:
        name = item.get("name")
        location = item.get("location")
        expiry = item.get("expiration_date")
        image = item.get("image_url")
        ai_names = item.get("ai_suggested_names", [])
        print(f"\n[ACTIVE] Item: {name}")
        print(f"  * Location: {location}")
        if expiry:
            print(f"  * Expiration: {expiry}")
        if image:
            print(f"  * Thumbnail: {image}")
        if ai_names:
            print(f"  * AI Suggestions: {ai_names}")

    if not inventory_results["present_items"]:
        print("\nNo currently active items found (all items marked consumed). Sample of latest items:")
        for itm in inventory_results["all_items"][-10:]:
            c = itm.get("content", {})
            print(f"  - {c.get('name')} (Status: {c.get('presence_status')}, Added: {c.get('added_at')})")

    print("\n" + "=" * 60)
    print(f"Full output saved to: {output_file}")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Samsung Food (Whisk) AI Food Manager Inspector")
    parser.add_argument("--email", default="", help="Samsung Account Email")
    parser.add_argument("--password", default="", help="Samsung Account Password")
    parser.add_argument(
        "--config",
        default="config.json",
        help="Path to JSON config file (default: config.json)",
    )
    parser.add_argument(
        "--session",
        default="smartthings_session.json",
        help="Path to saved session state file (default: smartthings_session.json)",
    )
    parser.add_argument(
        "--output",
        default="/tmp/samsung_food_inventory_dump.json",
        help="Path to save full JSON dump (default: /tmp/samsung_food_inventory_dump.json)",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Run browser with visible UI for debugging",
    )
    args = parser.parse_args()

    email = args.email
    password = args.password

    script_dir = os.path.dirname(os.path.abspath(__file__))

    config_path = os.path.expanduser(args.config)
    if not os.path.exists(config_path):
        fallback_cfg = os.path.join(script_dir, "config.json")
        if os.path.exists(fallback_cfg):
            config_path = fallback_cfg
        elif os.path.exists(os.path.join(script_dir, "../tools/pat_rotator/config.json")):
            config_path = os.path.join(script_dir, "../tools/pat_rotator/config.json")
        elif os.path.exists(os.path.expanduser("~/smartthings_pat_rotator/config.json")):
            config_path = os.path.expanduser("~/smartthings_pat_rotator/config.json")

    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                cfg = json.load(f)
            email = email or cfg.get("samsung_email")
            password = password or cfg.get("samsung_password")
        except Exception as err:
            _LOGGER.warning("Could not read config file %s: %s", config_path, err)

    session_file = args.session
    if not os.path.isabs(os.path.expanduser(session_file)) and not os.path.exists(session_file):
        candidates = [
            os.path.join(script_dir, session_file),
            os.path.join(script_dir, "../tools/pat_rotator/smartthings_session.json"),
            os.path.expanduser("~/smartthings_pat_rotator/smartthings_session.json"),
        ]
        for c in candidates:
            if os.path.exists(c):
                session_file = c
                break

    inspect_samsung_food(
        email=email or "",
        password=password or "",
        session_file=session_file,
        output_file=args.output,
        headless=not args.no_headless,
    )


if __name__ == "__main__":
    main()
