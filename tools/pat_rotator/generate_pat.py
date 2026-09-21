#!/usr/bin/env python3
"""Automated SmartThings Personal Access Token (PAT) Generator.

Uses Playwright to log into account.smartthings.com/tokens,
handles optional 2FA verification from the terminal, saves browser session
cookies for persistent one-time login, generates a fresh 24-hour PAT with
device scopes, writes it to disk, and closes the browser.
"""

import os
import re
import sys
import time
import json
import logging
import argparse
from playwright.sync_api import sync_playwright

_LOGGER = logging.getLogger("pat_generator")

UUID_REGEX = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)


class InteractiveAuthRequired(RuntimeError):
    """Raised when human intervention (2FA, CAPTCHA, etc.) is required to authenticate."""
    pass


def generate_pat(
    email: str = "",
    password: str = "",
    output_file: str = "smartthings_pat.txt",
    session_file: str = "smartthings_session.json",
    token_name: str = "ha-fridge-camera",
    two_factor_method: str = "device",
    headless: bool = True,
) -> str:
    output_path = os.path.expanduser(output_file)
    session_path = os.path.expanduser(session_file)

    _LOGGER.info("Starting SmartThings PAT generation...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
            ],
            ignore_default_args=["--enable-automation"],
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
            _LOGGER.info("Loading saved session cookies from %s", session_path)
            context_kwargs["storage_state"] = session_path

        context = browser.new_context(**context_kwargs)

        # Remove navigator.webdriver flag to prevent bot-detection
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)

        page = context.new_page()

        # Log browser console output & API responses
        page.on("console", lambda msg: _LOGGER.info("[Browser Console %s] %s", msg.type, msg.text))
        page.on("pageerror", lambda err: _LOGGER.error("[Browser JS Error] %s", err))

        try:
            _LOGGER.info("Navigating to https://account.smartthings.com/tokens ...")
            page.goto("https://account.smartthings.com/tokens", timeout=60000)

            # Wait for either tokens page or signin page to load
            _LOGGER.info("Waiting for initial page load...")
            page.wait_for_selector(
                "input#account, input#iptLgnPlnID, input#signInId, input[type='email'], "
                "button:has-text('Generate new token'), a:has-text('Generate new token')",
                timeout=45000,
            )

            # Check if we are already authenticated via saved session cookies
            gen_button = page.locator("button:has-text('Generate new token'), a:has-text('Generate new token')").first
            if gen_button.is_visible():
                _LOGGER.info("Authenticated successfully using saved session cookies.")
            else:
                _LOGGER.info("No active session; proceeding with Samsung login.")
                if not email or not password:
                    raise RuntimeError("Email and password are required for login when no saved session exists.")

                # 1. Fill Email
                email_selectors = [
                    "input#account",
                    "input#iptLgnPlnID",
                    "input#signInId",
                    "input[name='iptLgnPlnID']",
                    "input[type='email']",
                    "input[name='email']",
                ]
                email_field = None
                for sel in email_selectors:
                    loc = page.locator(sel).first
                    if loc.is_visible():
                        email_field = loc
                        break

                if email_field is None:
                    raise RuntimeError("Could not find Samsung Account email input field.")

                _LOGGER.info("Entering Samsung login email...")
                email_field.fill(email)
                time.sleep(1)

                # Either click next button or press Enter (not both)
                next_btn = page.locator("button#signInButton, button:has-text('Next'), button:has-text('Sign in')").first
                if next_btn.is_visible():
                    next_btn.click()
                else:
                    page.keyboard.press("Enter")

                # 2. Fill Password
                password_selectors = [
                    "input#password",
                    "input#iptLgnPD",
                    "input[name='iptLgnPD']",
                    "input[type='password']",
                ]
                page.wait_for_selector(
                    "input#password, input#iptLgnPD, input[type='password']",
                    timeout=30000,
                )
                pwd_field = None
                for sel in password_selectors:
                    loc = page.locator(sel).first
                    if loc.is_visible():
                        pwd_field = loc
                        break

                if pwd_field is None:
                    raise RuntimeError("Could not find Samsung Account password input field.")

                _LOGGER.info("Entering password...")
                pwd_field.fill(password)
                time.sleep(1)

                # Either click sign in button or press Enter (not both)
                sign_in_btn = page.locator(
                    "button#signInButton, button[type='submit'], button:has-text('Sign in'), button:has-text('Log in')"
                ).first
                if sign_in_btn.is_visible():
                    _LOGGER.info("Clicking sign in button...")
                    sign_in_btn.click()
                else:
                    _LOGGER.info("Submitting login via Enter...")
                    page.keyboard.press("Enter")

                # 3. Check for onboarding / Privacy Notice / Consent screens (e.g. SmartThings "Control your devices ... Continue")
                _LOGGER.info("Checking for onboarding / Privacy Notice / consent screens...")
                time.sleep(5)

                for _ in range(3):
                    clicked_continue = False
                    for target in [page] + page.frames:
                        for sel in [
                            "button:has-text('Continue')",
                            "a:has-text('Continue')",
                            "button:has-text('Agree and continue')",
                            "button:has-text('Agree to all and continue')",
                            "button:has-text('I Agree')",
                            "button:has-text('Agree')",
                            "button:has-text('Accept')",
                        ]:
                            try:
                                loc = target.locator(sel).first
                                if loc.is_visible():
                                    _LOGGER.info("Detected consent / onboarding button '%s'; clicking to proceed...", sel)
                                    loc.click()
                                    clicked_continue = True
                                    time.sleep(4)
                                    break
                            except Exception:
                                pass
                        if clicked_continue:
                            break
                    if not clicked_continue:
                        break

                # 4. Check if tokens page is already visible after clicking Continue
                gen_button = page.locator("button:has-text('Generate new token'), a:has-text('Generate new token')").first
                is_2fa = False
                active_frame = page
                if gen_button.is_visible():
                    _LOGGER.info("Tokens page loaded directly! No 2FA verification required.")
                else:
                    # Check for genuine 2FA / Verification step
                    _LOGGER.info("Checking for post-login verification...")
                    page_text_lower = page.content().lower()
                    if any(kw in page_text_lower for kw in [
                        "two-step", "two-factor", "2-step", "verification code", "security code",
                        "galaxy device notification", "check your galaxy", "check your phone to verify",
                    ]):
                        is_2fa = True

                    if not is_2fa:
                        verify_indicators = [
                            "text=Two-step",
                            "text=two-step",
                            "text=two-factor",
                            "text=security code",
                            "text=Enter code",
                            "text=Enter the code",
                            "text=Check your Galaxy",
                            "text=Galaxy device notification",
                            "input[name*='code' i]",
                            "input[placeholder*='code' i]",
                            "input#code",
                        ]
                        for target in [page] + page.frames:
                            for sel in verify_indicators:
                                try:
                                    if target.locator(sel).first.is_visible():
                                        is_2fa = True
                                        active_frame = target
                                        break
                                except Exception:
                                    pass
                            if is_2fa:
                                break

                if is_2fa:
                    if not sys.stdin.isatty():
                        _LOGGER.error(
                            "Samsung account requires 2FA or device approval, but rotator is running non-interactively in background mode. "
                            "Please run '.venv/bin/python generate_pat.py' interactively in a terminal to authenticate once and refresh %s.",
                            session_path,
                        )
                        raise InteractiveAuthRequired("Interactive 2FA verification required in non-interactive/background mode.")

                    # Save diagnostic screenshot of 2FA screen
                    try:
                        page.screenshot(path="/tmp/samsung_2fa_prompt.png")
                    except Exception:
                        pass

                    # Step 3a: Handle 2FA according to configured two_factor_method ("device" vs "sms")
                    sms_routed = False
                    device_approved = False

                    if two_factor_method.lower() in ("sms", "text"):
                        _LOGGER.info("2FA method is 'sms'. Attempting to re-route 2FA to mobile phone text message...")
                        # 1. Direct "Verify with text message" buttons/links
                        direct_sms_selectors = [
                            "button:has-text('Verify with text message')",
                            "a:has-text('Verify with text message')",
                            "button:has-text('Send text message')",
                            "a:has-text('Send text message')",
                            "button:has-text('Use text message')",
                            "a:has-text('Use text message')",
                            "button:has-text('Text message')",
                            "a:has-text('Text message')",
                        ]
                        for target in [active_frame, page] + page.frames:
                            for sel in direct_sms_selectors:
                                try:
                                    loc = target.locator(sel).first
                                    if loc.is_visible():
                                        _LOGGER.info("Found direct SMS option '%s'; clicking to re-route 2FA to phone...", sel)
                                        loc.click()
                                        time.sleep(3)
                                        sms_routed = True
                                        active_frame = target
                                        break
                                except Exception:
                                    pass
                            if sms_routed:
                                break

                        # 2. If no direct button, look for "Verify another way" / "Didn't get a notification?"
                        if not sms_routed:
                            another_way_selectors = [
                                "button:has-text('Verify another way')",
                                "a:has-text('Verify another way')",
                                "button:has-text('Try another way')",
                                "a:has-text('Try another way')",
                                "button:has-text('Didn\'t get a notification?')",
                                "a:has-text('Didn\'t get a notification?')",
                                "button:has-text('Didn’t get a notification?')",
                                "a:has-text('Didn’t get a notification?')",
                                "button:has-text('Need help?')",
                                "a:has-text('Need help?')",
                            ]
                            for target in [active_frame, page] + page.frames:
                                for sel in another_way_selectors:
                                    try:
                                        loc = target.locator(sel).first
                                        if loc.is_visible():
                                            _LOGGER.info("Found '%s'; clicking to open alternative verification methods...", sel)
                                            loc.click()
                                            time.sleep(2)

                                            # Look for Text message / SMS option in the list
                                            for opt_sel in [
                                                "label:has-text('Text message')",
                                                "div:has-text('Text message')",
                                                "button:has-text('Text message')",
                                                "a:has-text('Text message')",
                                                "input[type='radio'][value*='sms' i]",
                                                "input[type='radio'][value*='text' i]",
                                                "button:has-text('Send text')",
                                                "button:has-text('Send code')",
                                            ]:
                                                try:
                                                    opt = target.locator(opt_sel).first
                                                    if opt.is_visible():
                                                        _LOGGER.info("Selected '%s'...", opt_sel)
                                                        opt.click()
                                                        time.sleep(1)
                                                        break
                                                except Exception:
                                                    pass

                                            # Click Confirm / Next / Send if present
                                            for next_sel in [
                                                "button:has-text('Send')",
                                                "button:has-text('Send code')",
                                                "button:has-text('Next')",
                                                "button:has-text('Continue')",
                                                "button:has-text('Confirm')",
                                            ]:
                                                try:
                                                    btn = target.locator(next_sel).first
                                                    if btn.is_visible():
                                                        _LOGGER.info("Clicked confirmation '%s'...", next_sel)
                                                        btn.click()
                                                        time.sleep(3)
                                                        break
                                                except Exception:
                                                    pass

                                            sms_routed = True
                                            active_frame = target
                                            break
                                    except Exception:
                                        pass
                                if sms_routed:
                                    break
                    else:
                        _LOGGER.info("2FA method is '%s'. Retaining Galaxy device / tablet push notification.", two_factor_method)

                    # Auto-check "Trust this device" / "Don't ask again on this device" if present
                    trust_selectors = [
                        "input#trustDevice",
                        "input[name*='trust' i]",
                        "input[name*='remember' i]",
                        "label:has-text('Trust this device')",
                        "label:has-text('Don\'t ask again')",
                        "label:has-text('Don’t ask again')",
                    ]
                    for target in [active_frame, page] + page.frames:
                        for t_sel in trust_selectors:
                            try:
                                t_loc = target.locator(t_sel).first
                                if t_loc.is_visible():
                                    t_loc.click()
                                    _LOGGER.info("Checked '%s' to remember browser session.", t_sel)
                                    break
                            except Exception:
                                pass

                    if sms_routed:
                        print()
                        print("=" * 60)
                        print("  SAMSUNG TWO-FACTOR AUTHENTICATION (2FA) VIA SMS")
                        print("  [SUCCESS] Re-routed 2FA to your mobile phone via SMS text message!")
                        print("  Please check your mobile phone for the 6-digit verification code.")
                        print("=" * 60)
                        two_fa_code = input("Enter the 2FA verification code: ").strip()
                    elif two_factor_method.lower() in ("device", "push"):
                        print()
                        print("=" * 60)
                        print("  SAMSUNG TWO-FACTOR AUTHENTICATION (2FA) VIA GALAXY DEVICE")
                        print("  Push notification sent to your Galaxy device / tablet.")
                        print("  Please tap 'Yes' (or approve sign-in) on your device.")
                        print("=" * 60)
                        _LOGGER.info("Waiting for Galaxy device approval...")

                        for _ in range(30):
                            time.sleep(2)
                            gen_loc = page.locator("button:has-text('Generate new token'), a:has-text('Generate new token')").first
                            try:
                                if gen_loc.is_visible() or ("account.smartthings.com" in page.url and "/tokens" in page.url):
                                    device_approved = True
                                    _LOGGER.info("Device approval detected successfully via browser redirection!")
                                    break
                            except Exception:
                                pass
                            for c_sel in ["button:has-text('Continue')", "button:has-text('Agree')"]:
                                try:
                                    c_loc = page.locator(c_sel).first
                                    if c_loc.is_visible():
                                        c_loc.click()
                                        time.sleep(2)
                                except Exception:
                                    pass
                            if device_approved:
                                break

                        if not device_approved:
                            two_fa_code = input("Enter 2FA verification code (or press Enter if approved on device): ").strip()
                        else:
                            two_fa_code = ""
                    else:
                        print()
                        print("=" * 60)
                        print("  SAMSUNG TWO-FACTOR AUTHENTICATION (2FA) REQUIRED")
                        print("  A verification code was sent to your phone or authenticator app.")
                        print("  (If this went to your tablet, check /tmp/samsung_2fa_prompt.png)")
                        print("=" * 60)
                        two_fa_code = input("Enter the 2FA verification code: ").strip()

                    code_selectors = [
                        "input[name*='code' i]",
                        "input[placeholder*='code' i]",
                        "input#code",
                        "input#verificationCode",
                        "input[type='tel']",
                        "input[type='number']",
                        "input[type='text']",
                    ]
                    code_field = None
                    if two_fa_code:
                        # Wait up to 10 seconds for code field to appear
                        for _ in range(10):
                            for target in [active_frame, page] + page.frames:
                                for sel in code_selectors:
                                    try:
                                        loc = target.locator(sel).first
                                        if loc.is_visible():
                                            code_field = loc
                                            active_frame = target
                                            break
                                    except Exception:
                                        pass
                                if code_field:
                                    break
                            if code_field:
                                break
                            time.sleep(1)

                        if code_field:
                            code_field.fill(two_fa_code)
                            time.sleep(1)
                            verify_btn = active_frame.locator(
                                "button:has-text('Verify'), button:has-text('Submit'), button:has-text('Next'), button[type='submit']"
                            ).first
                            if verify_btn.is_visible():
                                verify_btn.click()
                            else:
                                active_frame.keyboard.press("Enter")
                        elif not device_approved:
                            raise RuntimeError("Could not find 2FA code input field on page.")

                # 4. Handle possible interstitial prompts (e.g. Terms of Service, Privacy Updates, password reminders)
                _LOGGER.info("Checking for interstitial prompts (e.g. Terms of Service, password change reminders)...")
                time.sleep(4)

                consent_selectors = [
                    "button:has-text('Agree and continue')",
                    "button:has-text('Agree to all and continue')",
                    "button:has-text('I Agree')",
                    "button:has-text('Agree')",
                    "button:has-text('Accept')",
                    "button:has-text('Continue')",
                    "button#agreeBtn",
                    "button#accept-btn",
                    "button[data-log-id='not-now']",
                    "button:has-text('Not now')",
                    "button[data-testid='Buttons5B67DE22']",
                    "a:has-text('Not now')",
                    "button:has-text('Later')",
                    "button:has-text('Remind me later')",
                    "button:has-text('Cancel')",
                ]
                agree_all_checkboxes = [
                    "input#agreeAll",
                    "input[name='agreeAll']",
                    "label:has-text('Agree to all')",
                    "label:has-text('I agree to all')",
                ]

                for _ in range(4):
                    clicked_interstitial = False
                    for target in [page] + page.frames:
                        for cb_sel in agree_all_checkboxes:
                            try:
                                cb = target.locator(cb_sel).first
                                if cb.is_visible():
                                    _LOGGER.info("Detected consent checkbox '%s'; checking...", cb_sel)
                                    cb.click()
                                    time.sleep(1)
                            except Exception:
                                pass

                        for sel in consent_selectors:
                            try:
                                loc = target.locator(sel).first
                                if loc.is_visible():
                                    _LOGGER.info("Detected interstitial screen; clicking '%s'...", sel)
                                    loc.click()
                                    clicked_interstitial = True
                                    time.sleep(3)
                                    break
                            except Exception:
                                pass
                        if clicked_interstitial:
                            break
                    if not clicked_interstitial:
                        break

                # Wait for tokens page
                _LOGGER.info("Waiting for tokens page to load...")
                page.wait_for_selector(
                    "button:has-text('Generate new token'), a:has-text('Generate new token')",
                    timeout=45000,
                )

                # Save authenticated session state (cookies + storage) for future 1-click runs
                _LOGGER.info("Saving session state to %s for future persistent logins...", session_path)
                os.makedirs(os.path.dirname(os.path.abspath(session_path)), exist_ok=True)
                context.storage_state(path=session_path)

            # 5. Generate the new token
            _LOGGER.info("Clicking 'Generate new token' button...")
            gen_btn = page.locator("button:has-text('Generate new token'), a:has-text('Generate new token')").first
            gen_btn.click()
            time.sleep(2)

            # Fill Token Name & Select Authorized Scopes
            page.wait_for_selector("input[type='text'], input[name='inputTokenName'], input#token-name", timeout=15000)

            name_selectors = [
                "input[name='inputTokenName']",
                "input#token-name",
                "input[name='tokenName']",
                "input[placeholder*='Token Name']",
                "input[type='text']",
            ]
            for sel in name_selectors:
                loc = page.locator(sel).first
                if loc.is_visible():
                    unique_token_name = f"{token_name}-{int(time.time()) % 10000}"
                    loc.fill(unique_token_name)
                    _LOGGER.info("Set token name to '%s'", unique_token_name)
                    break

            # Wait for scope checkboxes to finish loading asynchronously in React
            _LOGGER.info("Waiting for scope checkboxes to render in React...")
            try:
                page.wait_for_selector(
                    "input[type='checkbox'], .form-check-input, input#select-all",
                    timeout=15000,
                )
                time.sleep(1)
            except Exception as e:
                _LOGGER.warning("Scope checkbox wait notice: %s. Retrying...", e)

            # Retry loop to ensure checkboxes have mounted in the DOM
            checkboxes = []
            for _ in range(10):
                checkboxes = page.locator("input[type='checkbox']").all()
                if len(checkboxes) > 0:
                    break
                time.sleep(1)

            _LOGGER.info("Found %d scope checkbox(es). Selecting all scopes...", len(checkboxes))
            for cb in checkboxes:
                try:
                    if not cb.is_checked():
                        cb.set_checked(True, force=True)
                except Exception:
                    try:
                        cb.click(force=True)
                    except Exception:
                        pass

            time.sleep(1)

            # Submit token generation form
            submit_token_btn = page.locator(
                "button#submit, button:has-text('Generate Token'), button:has-text('Generate token'), button[type='submit']"
            ).first
            _LOGGER.info("Submitting token generation form...")
            submit_token_btn.click()
            time.sleep(3)

            # Extract the generated PAT string
            _LOGGER.info("Extracting newly generated token...")
            page_text = page.inner_text("body")
            matches = UUID_REGEX.findall(page_text)

            token_found = None
            for m in matches:
                token_found = m
                break

            if not token_found:
                for elem in page.locator("code, pre, input[readonly], .token-string, .token-value").all():
                    txt = elem.inner_text() or elem.get_attribute("value") or ""
                    match = UUID_REGEX.search(txt)
                    if match:
                        token_found = match.group(0)
                        break

            if not token_found:
                raise RuntimeError("Could not find the generated PAT token on the confirmation page.")

            _LOGGER.info("Successfully generated new SmartThings PAT: %s...", token_found[:8])

            # Write token to output file
            os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
            with open(output_path, "w") as f:
                f.write(f"{token_found}\n")

            # Update session state
            context.storage_state(path=session_path)

            _LOGGER.info("Saved fresh PAT to: %s", output_path)
            return token_found

        except Exception as e:
            try:
                current_url = page.url
                page_title = page.title()
                _LOGGER.error("PAT generation failed on page URL: %s | Page title: '%s'", current_url, page_title)

                content_lower = page.content().lower()
                if any(k in content_lower for k in ["recaptcha", "g-recaptcha", "challenge-running", "cf-turnstile", "captcha"]):
                    _LOGGER.error("Samsung presented a CAPTCHA challenge that cannot be solved automatically in headless mode.")
                    if headless:
                        e = InteractiveAuthRequired("Samsung CAPTCHA challenge requires manual/interactive resolution.")
                elif any(k in content_lower for k in ["verify", "two-step", "the code", "security code"]):
                    _LOGGER.error("Samsung is requesting two-step verification / device approval.")
                    if headless and not sys.stdin.isatty():
                        e = InteractiveAuthRequired("Interactive 2FA verification required in non-interactive/background mode.")

                script_dir = os.path.dirname(os.path.abspath(__file__))
                local_png = os.path.join(script_dir, "smartthings_pat_error.png")
                local_html = os.path.join(script_dir, "smartthings_pat_error.html")
                page.screenshot(path=local_png)
                page.screenshot(path="/tmp/smartthings_pat_error.png")
                with open(local_html, "w", encoding="utf-8") as f:
                    f.write(page.content())
                with open("/tmp/smartthings_pat_error.html", "w", encoding="utf-8") as f:
                    f.write(page.content())
                _LOGGER.info("Saved failure screenshot to %s and %s", local_png, "/tmp/smartthings_pat_error.png")
            except Exception as screenshot_err:
                _LOGGER.warning("Could not capture failure screenshot: %s", screenshot_err)
            raise e
        finally:
            browser.close()


def main():
    parser = argparse.ArgumentParser(description="SmartThings Automated PAT Generator")
    parser.add_argument("--email", required=False, help="Samsung Account Email")
    parser.add_argument("--password", required=False, help="Samsung Account Password")
    parser.add_argument(
        "--config",
        default=os.path.expanduser("config.json"),
        help="Path to JSON config file (default: config.json)",
    )
    parser.add_argument(
        "--session",
        default=os.path.expanduser("smartthings_session.json"),
        help="Path to saved session state file (default: smartthings_session.json)",
    )
    parser.add_argument(
        "--output",
        default=os.path.expanduser("smartthings_pat.txt"),
        help="Path to write the PAT to (default: smartthings_pat.txt)",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Run browser with visible UI for debugging",
    )
    parser.add_argument(
        "--2fa-method",
        dest="two_factor_method",
        choices=["device", "sms"],
        default=None,
        help="2FA delivery method: 'device' for Galaxy tablet/phone push, 'sms' for mobile text message",
    )
    args = parser.parse_args()

    email = args.email
    password = args.password
    two_factor_method = args.two_factor_method

    script_dir = os.path.dirname(os.path.abspath(__file__))

    config_path = os.path.expanduser(args.config)
    if not os.path.exists(config_path):
        fallback_cfg = os.path.join(script_dir, "config.json")
        if os.path.exists(fallback_cfg):
            config_path = fallback_cfg

    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                cfg = json.load(f)
            email = email or cfg.get("samsung_email")
            password = password or cfg.get("samsung_password")
            if not two_factor_method:
                two_factor_method = cfg.get("two_factor_method")
        except Exception as err:
            _LOGGER.warning("Could not read config file %s: %s", config_path, err)

    two_factor_method = two_factor_method or "device"

    output_file = args.output
    if not os.path.isabs(os.path.expanduser(output_file)) and not os.path.exists(output_file):
        output_file = os.path.join(script_dir, output_file)

    session_file = args.session
    if not os.path.isabs(os.path.expanduser(session_file)) and not os.path.exists(session_file):
        session_file = os.path.join(script_dir, session_file)

    try:
        token = generate_pat(
            email=email or "",
            password=password or "",
            output_file=output_file,
            session_file=session_file,
            two_factor_method=two_factor_method,
            headless=not args.no_headless,
        )
        print()
        print("=" * 60)
        print(f"SUCCESS! New PAT: {token}")
        print("=" * 60)
    except Exception as err:
        _LOGGER.error("Failed to generate PAT: %s", err)
        sys.exit(1)


if __name__ == "__main__":
    main()
