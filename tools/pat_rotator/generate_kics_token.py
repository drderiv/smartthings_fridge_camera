#!/usr/bin/env python3
"""Automated Samsung Family Hub Food Circles (KICS EPA) Token Manager.

Maintains, refreshes, and validates the active 26-character Samsung Account Bearer token
for Client ID `qumapq783u` to fetch live food circle crops from
https://kics-epa.samsungepa.com/api/foodlist.

Supports:
1. Autonomous zero-browser renewal via Samsung Account OAuth2 refresh_token grant.
2. Initial browser-based SSO acquisition using Samsung Account IAM authorization gateway
   (https://account.samsung.com/iam/oauth2/authorize) and saved session cookies.
"""

import argparse
import json
import logging
import os
import sys
import time
import urllib.parse
import requests
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_LOGGER = logging.getLogger("kics_token_generator")

CLIENT_ID = "qumapq783u"
AUTH_BASE = "https://us-auth2.samsungosp.com"
IAM_AUTHORIZE_URL = "https://account.samsung.com/iam/oauth2/authorize"
REDIRECT_URI = "https://kics-epa.samsungepa.com/api/oauth/callback"
API_ENDPOINT = "https://kics-epa.samsungepa.com/api/foodlist"
DEFAULT_LOCATION_ID = ""


def _resolve_location_id(location_id: Optional[str] = None, config_file: str = "config.json") -> str:
    """Resolve location ID from parameter, config.json, or environment."""
    if location_id and location_id.strip() and not location_id.startswith("YOUR_"):
        return location_id.strip()
    if os.path.exists(config_file):
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                loc = cfg.get("location_id")
                if loc and not str(loc).startswith("YOUR_"):
                    return str(loc).strip()
        except Exception:
            pass
    return ""


def validate_kics_token(token: str, location_id: str = "") -> bool:
    """Verify if a KICS Bearer token is currently active and authorized."""
    if not token or not token.strip():
        return False

    loc = _resolve_location_id(location_id)
    auth_val = token.strip() if token.startswith("Bearer ") else f"Bearer {token.strip()}"
    headers = {
        "authorization": auth_val,
        "kics-sa-auth-url": "us-auth2.samsungosp.com",
        "kics-sa-app-id": CLIENT_ID,
        "kics-location": loc,
        "kics-client-platform": "android",
        "kics-client-version": "KS.ST.2.11-20",
        "accept": "application/json, text/plain, */*",
        "user-agent": "Mozilla/5.0 (Linux; Android 17; sdk_gphone16k_x86_64) AppleWebKit/537.36",
    }

    try:
        r = requests.get(API_ENDPOINT, headers=headers, timeout=10)
        if r.status_code == 200:
            return True
        _LOGGER.debug("Token validation check returned HTTP %d: %s", r.status_code, r.text[:120])
        return False
    except Exception as err:
        _LOGGER.warning("Could not validate KICS token against endpoint: %s", err)
        return False


def get_stored_kics_token(candidate_paths: list[str]) -> Optional[str]:
    """Check candidate file paths for an existing non-empty token."""
    for p in candidate_paths:
        expanded = os.path.expanduser(p)
        if os.path.exists(expanded):
            try:
                with open(expanded, "r", encoding="utf-8") as f:
                    tok = f.read().strip()
                if tok:
                    _LOGGER.info("Found token in %s (%s...)", expanded, tok[:8])
                    return tok
            except Exception as err:
                _LOGGER.debug("Error reading %s: %s", expanded, err)
    return None


def refresh_kics_token(
    refresh_token: str,
    client_id: str = CLIENT_ID,
    auth_base: str = AUTH_BASE,
) -> dict:
    """Exchange a rolling refresh token for a fresh KICS access token.

    Returns a dict with:
        access_token (str)
        refresh_token (str) - newly minted rolling refresh token (valid for 90 days)
        expires_in (int) - access token lifetime (~24 hours)
        refresh_token_expires_in (int) - refresh token lifetime (~90 days)
    """
    if not refresh_token or not refresh_token.strip():
        raise ValueError("Cannot refresh KICS token with empty refresh token")

    url = f"{auth_base}/auth/oauth2/token"
    data = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token.strip(),
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Mozilla/5.0 (Linux; Android 17; sdk_gphone16k_x86_64) AppleWebKit/537.36",
    }
    r = requests.post(url, data=data, headers=headers, timeout=15)
    if not r.ok:
        raise RuntimeError(f"Samsung Account OAuth refresh failed ({r.status_code}): {r.text[:200]}")

    payload = r.json()
    acc_tok = payload.get("access_token")
    if not acc_tok:
        raise RuntimeError(f"No access_token returned by Samsung Account: {payload}")
    return payload


def rotate_kics_token(
    refresh_token_file: str = "kics_refresh_token.txt",
    output_file: str = "kics_food_token.txt",
    config_file: str = "config.json",
    client_id: str = CLIENT_ID,
    explicit_refresh_token: Optional[str] = None,
) -> str:
    """Use the saved rolling refresh token to obtain a fresh access token."""
    base_dir = os.path.dirname(os.path.abspath(config_file))
    refr_path = os.path.expanduser(refresh_token_file)
    output_path = os.path.expanduser(output_file)

    curr_refresh_tok = explicit_refresh_token
    if not curr_refresh_tok:
        candidate_refresh_paths = [
            refr_path,
            os.path.join(base_dir, "kics_refresh_token.txt"),
        ]
        curr_refresh_tok = get_stored_kics_token(candidate_refresh_paths)

    if not curr_refresh_tok and os.path.exists(config_file):
        try:
            with open(config_file, "r", encoding="utf-8") as cf:
                cfg = json.load(cf)
            curr_refresh_tok = cfg.get("kics_refresh_token")
        except Exception:
            pass

    if not curr_refresh_tok:
        existing_acc = get_stored_kics_token([output_path, os.path.join(base_dir, "kics_food_token.txt")])
        if existing_acc and validate_kics_token(existing_acc):
            _LOGGER.info("No refresh token found, but existing KICS access token is still valid.")
            return existing_acc
        raise RuntimeError(
            "No KICS refresh token found in kics_refresh_token.txt or config.json. "
            "Please provide a refresh token to enable automated renewal."
        )

    _LOGGER.info("Refreshing KICS access token via Samsung Account OAuth...")
    res = refresh_kics_token(curr_refresh_tok, client_id=client_id)
    new_access_token = res["access_token"]
    new_refresh_token = res.get("refresh_token")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(f"{new_access_token}\n")
    _LOGGER.info("Saved fresh KICS access token to %s (valid for %.1f hours)", output_path, res.get("expires_in", 86400) / 3600.0)

    if new_refresh_token:
        with open(refr_path, "w", encoding="utf-8") as f:
            f.write(f"{new_refresh_token}\n")
        _LOGGER.info("Updated rolling KICS refresh token in %s (valid for %.1f days)", refr_path, res.get("refresh_token_expires_in", 7776000) / 86400.0)

    return new_access_token


def acquire_initial_kics_token(
    session_file: str = "smartthings_session.json",
    config_file: str = "config.json",
    output_file: str = "kics_food_token.txt",
    refresh_file: str = "kics_refresh_token.txt",
    headless: bool = True,
    email: str = "",
    password: str = "",
) -> str:
    """Acquire initial KICS access and refresh tokens using browser-based OAuth with saved Samsung session.

    Loads Samsung's web IAM authorization gateway for Client ID `qumapq783u`.
    If smartthings_session.json exists, reuses authenticated session cookies.
    Intercepts the redirect callback and exchanges the authorization code for
    the initial access_token and 90-day rolling refresh_token.
    """
    base_dir = os.path.dirname(os.path.abspath(config_file))
    session_path = os.path.expanduser(session_file)
    output_path = os.path.expanduser(output_file)
    refresh_path = os.path.expanduser(refresh_file)

    if not os.path.isabs(session_path) and not os.path.exists(session_path):
        candidate_sess = os.path.join(base_dir, session_file)
        if os.path.exists(candidate_sess):
            session_path = candidate_sess

    oauth_authorize_url = (
        f"{IAM_AUTHORIZE_URL}?"
        f"client_id={CLIENT_ID}&"
        f"response_type=code&"
        f"redirect_uri={urllib.parse.quote(REDIRECT_URI, safe='')}"
    )

    _LOGGER.info("Starting browser-based initial KICS token acquisition...")
    from playwright.sync_api import sync_playwright

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
        }

        if os.path.exists(session_path):
            _LOGGER.info("Loading saved session cookies from %s", session_path)
            context_kwargs["storage_state"] = session_path

        context = browser.new_context(**context_kwargs)
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")

        page = context.new_page()
        captured_code = None

        def check_url(url: str):
            nonlocal captured_code
            if "kics-epa.samsungepa.com" in url and "code=" in url and not captured_code:
                parsed = urllib.parse.urlparse(url)
                params = urllib.parse.parse_qs(parsed.query)
                if "code" in params:
                    captured_code = params["code"][0]
                    _LOGGER.info("Captured OAuth authorization code from redirect callback!")

        page.on("request", lambda req: check_url(req.url))
        page.on("response", lambda resp: check_url(resp.url))

        try:
            page.goto(oauth_authorize_url, timeout=60000)
            time.sleep(3)

            for _ in range(20):
                if captured_code:
                    break
                check_url(page.url)
                if captured_code:
                    break

                # Auto-click consent / Allow / Agree / Accept buttons if displayed
                for sel in [
                    "button:has-text('Agree')",
                    "button:has-text('Allow')",
                    "button:has-text('Accept')",
                    "button:has-text('Confirm')",
                    "button:has-text('Continue')",
                    "button#agreeBtn",
                    "button.btn-primary",
                ]:
                    try:
                        btn = page.locator(sel).first
                        if btn.is_visible():
                            _LOGGER.info("Clicking consent button: %s", sel)
                            btn.click()
                            time.sleep(2)
                            break
                    except Exception:
                        pass

                # If signin form visible and email/password provided
                if not captured_code:
                    try:
                        email_input = page.locator("input#account, input#iptLgnPlnID, input#signInId, input[type='email']").first
                        if email_input.is_visible() and email and password:
                            _LOGGER.info("Entering email into Samsung login...")
                            email_input.fill(email)
                            page.keyboard.press("Enter")
                            time.sleep(2)
                            pass_input = page.locator("input#password, input[type='password']").first
                            if pass_input.is_visible():
                                _LOGGER.info("Entering password into Samsung login...")
                                pass_input.fill(password)
                                page.keyboard.press("Enter")
                                time.sleep(5)
                    except Exception:
                        pass

                time.sleep(1)

            # Save updated storage state back to session_path
            if session_path and os.path.exists(os.path.dirname(os.path.abspath(session_path))):
                try:
                    context.storage_state(path=session_path)
                except Exception:
                    pass

        finally:
            browser.close()

    if not captured_code:
        raise RuntimeError(
            "Could not capture OAuth authorization code from Samsung IAM redirect. "
            "Please run with '--no-headless' or ensure smartthings_session.json contains an active login."
        )

    # Step 2: Exchange authorization code for access_token + refresh_token
    token_url = f"{AUTH_BASE}/auth/oauth2/token"
    token_payload = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": captured_code,
        "redirect_uri": REDIRECT_URI,
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Mozilla/5.0 (Linux; Android 17; sdk_gphone16k_x86_64) AppleWebKit/537.36",
    }

    _LOGGER.info("Exchanging authorization code for KICS access and refresh tokens...")
    res = requests.post(token_url, data=token_payload, headers=headers, timeout=15)
    if not res.ok:
        raise RuntimeError(f"OAuth code exchange failed ({res.status_code}): {res.text}")

    data = res.json()
    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token")

    if not access_token:
        raise RuntimeError(f"No access_token returned by Samsung Account: {data}")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(f"{access_token}\n")
    _LOGGER.info("Saved initial KICS access token to %s", output_path)

    if refresh_token:
        with open(refresh_path, "w", encoding="utf-8") as f:
            f.write(f"{refresh_token}\n")
        _LOGGER.info("Saved initial rolling KICS refresh token to %s", refresh_path)

    return access_token


def generate_or_validate_kics_token(
    config_file: str = "config.json",
    output_file: str = "kics_food_token.txt",
    refresh_token_file: str = "kics_refresh_token.txt",
    session_file: str = "smartthings_session.json",
    explicit_token: Optional[str] = None,
    explicit_refresh_token: Optional[str] = None,
    allow_browser_acquisition: bool = True,
    headless: bool = True,
) -> str:
    """Ensure an active KICS token exists, validate it, refresh if needed, or acquire via web SSO."""
    base_dir = os.path.dirname(os.path.abspath(config_file))
    output_path = os.path.expanduser(output_file)

    # 1. If explicit access token passed, save and validate
    if explicit_token and explicit_token.strip():
        tok = explicit_token.strip()
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(f"{tok}\n")
        _LOGGER.info("Saved explicit KICS token to %s", output_path)
        return tok

    # 2. If explicit refresh token passed, rotate immediately
    if explicit_refresh_token and explicit_refresh_token.strip():
        return rotate_kics_token(
            refresh_token_file=refresh_token_file,
            output_file=output_file,
            config_file=config_file,
            explicit_refresh_token=explicit_refresh_token.strip(),
        )

    # 3. Check candidate access token files
    candidates = [
        output_path,
        os.path.join(base_dir, "kics_food_token.txt"),
        os.path.join(base_dir, "active_bearer_token.txt"),
        "kics_food_token.txt",
        "active_bearer_token.txt",
    ]
    stored_token = get_stored_kics_token(candidates)
    if stored_token:
        _LOGGER.info("Validating stored KICS token against %s...", API_ENDPOINT)
        if validate_kics_token(stored_token):
            _LOGGER.info("Stored KICS token is VALID and active.")
            if output_path != candidates[0] or not os.path.exists(output_path):
                with open(output_path, "w", encoding="utf-8") as f:
                    f.write(f"{stored_token}\n")
            return stored_token
        else:
            _LOGGER.warning("Stored KICS token has expired. Attempting automatic refresh...")

    # 4. Attempt automatic refresh using stored refresh token
    try:
        return rotate_kics_token(
            refresh_token_file=refresh_token_file,
            output_file=output_file,
            config_file=config_file,
        )
    except Exception as err:
        _LOGGER.debug("Automatic KICS token refresh could not proceed: %s", err)

    # 5. Check if token is in config.json
    if os.path.exists(config_file):
        try:
            with open(config_file, "r", encoding="utf-8") as cf:
                cfg = json.load(cf)
            cfg_tok = cfg.get("kics_food_token") or cfg.get("food_circles_token")
            if cfg_tok and validate_kics_token(cfg_tok):
                _LOGGER.info("Validated KICS token from config.json.")
                with open(output_path, "w", encoding="utf-8") as f:
                    f.write(f"{cfg_tok}\n")
                return cfg_tok
        except Exception as err:
            _LOGGER.warning("Error reading token from config.json: %s", err)

    # 6. If browser acquisition is enabled and session exists, acquire initial tokens via web SSO
    if allow_browser_acquisition:
        candidate_sessions = [
            session_file,
            os.path.join(base_dir, "smartthings_session.json"),
            "smartthings_session.json",
        ]
        active_sess = next((s for s in candidate_sessions if os.path.exists(os.path.expanduser(s))), None)
        if active_sess:
            try:
                _LOGGER.info("Attempting initial KICS web SSO acquisition using %s...", active_sess)
                return acquire_initial_kics_token(
                    session_file=active_sess,
                    config_file=config_file,
                    output_file=output_file,
                    refresh_file=refresh_token_file,
                    headless=headless,
                )
            except Exception as acq_err:
                _LOGGER.warning("Initial KICS web SSO acquisition failed: %s", acq_err)

    raise RuntimeError(
        "No valid KICS Food Circles token found. "
        "Please provide an active token via --token '<TOKEN>', run with '--login' to acquire via browser, "
        "or place refresh token in kics_refresh_token.txt."
    )


def main():
    parser = argparse.ArgumentParser(description="Samsung KICS Food Circles Token Manager")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    parser.add_argument("--output", default="kics_food_token.txt", help="Path to write active token")
    parser.add_argument("--refresh-file", default="kics_refresh_token.txt", help="Path to rolling refresh token")
    parser.add_argument("--session", default="smartthings_session.json", help="Path to smartthings_session.json")
    parser.add_argument("--token", default=None, help="Explicit active Bearer token to save and validate")
    parser.add_argument("--refresh-token", default=None, help="Explicit refresh token to exchange for fresh access token")
    parser.add_argument("--refresh-now", action="store_true", help="Force an immediate token refresh via refresh_token")
    parser.add_argument("--login", action="store_true", help="Acquire initial tokens via browser web SSO")
    parser.add_argument("--no-headless", action="store_true", help="Run browser visibly for interactive approval")
    parser.add_argument("--validate-only", action="store_true", help="Only validate existing token without erroring")
    args = parser.parse_args()

    try:
        if args.refresh_now:
            tok = rotate_kics_token(
                refresh_token_file=args.refresh_file,
                output_file=args.output,
                config_file=args.config,
                explicit_refresh_token=args.refresh_token,
            )
        elif args.login:
            tok = acquire_initial_kics_token(
                session_file=args.session,
                config_file=args.config,
                output_file=args.output,
                refresh_file=args.refresh_file,
                headless=not args.no_headless,
            )
        else:
            tok = generate_or_validate_kics_token(
                config_file=args.config,
                output_file=args.output,
                refresh_token_file=args.refresh_file,
                session_file=args.session,
                explicit_token=args.token,
                explicit_refresh_token=args.refresh_token,
                allow_browser_acquisition=True,
                headless=not args.no_headless,
            )
        print(f"SUCCESS: Active KICS token is ready ({tok[:8]}...) in {args.output}")
    except Exception as e:
        if args.validate_only:
            _LOGGER.info("Validation result: False (%s)", e)
            sys.exit(1)
        _LOGGER.error("%s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
