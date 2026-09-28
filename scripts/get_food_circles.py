#!/usr/bin/env python3
"""Standalone Samsung Family Hub Food Circle Thumbnail Downloader.

Fetches the live circular crops of food items inside the Samsung Family Hub
refrigerator using the KICS EPA backend (https://kics-epa.samsungepa.com).

Authentication:
  Uses an active Samsung Account Bearer token (from `active_bearer_token.txt`,
  CLI `--token`, or re-minted via Playwright).

Location Discovery:
  Auto-detects the fridge's SmartThings Location ID via local PAT service
  (http://127.0.0.1:8765/pat), or from `config.json`, or via `--location-id`.
"""

import argparse
import json
import logging
import os
import re
import sys
import time
import urllib.parse
from typing import Dict, List, Optional
import requests
from playwright.sync_api import sync_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
_LOGGER = logging.getLogger("food_circles")

# Samsung KICS EPA Constants (from SmartThings Android Family Hub plugin)
DEFAULT_CLIENT_ID = "qumapq783u"
AUTH_BASE = "https://us-auth2.samsungosp.com"
API_ENDPOINT = "https://kics-epa.samsungepa.com/api/foodlist"
DEFAULT_LOCATION_ID = ""


def discover_location_id(pat: str = "", rotator_url: str = "http://127.0.0.1:8765/pat") -> Optional[str]:
    """Attempt to discover the SmartThings Location ID via the active PAT or rotator."""
    # 1. Try querying /location_id from the rotator server first
    try:
        base_rotator = rotator_url.rsplit('/', 1)[0]
        r = requests.get(f"{base_rotator}/location_id", timeout=2)
        if r.ok:
            loc = r.json().get("location_id")
            if loc and not loc.startswith("YOUR_"):
                _LOGGER.info("Discovered Location ID from rotator server (/location_id): %s", loc)
                return loc
    except Exception:
        pass

    token = pat
    if not token:
        try:
            r = requests.get(rotator_url, timeout=3)
            if r.ok:
                token = r.json().get("token", "")
        except Exception:
            pass

    if not token:
        pat_paths = [
            "smartthings_pat.txt",
            os.path.join(os.path.dirname(__file__), "../smartthings_pat.txt"),
            os.path.join(os.path.dirname(__file__), "../tools/pat_rotator/smartthings_pat.txt"),
            os.path.expanduser("~/smartthings_pat_rotator/smartthings_pat.txt"),
        ]
        for p in pat_paths:
            if os.path.exists(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        tok = f.read().strip()
                        if tok:
                            token = tok
                            break
                except Exception:
                    pass

    if not token:
        return None

    try:
        res = requests.get(
            "https://api.smartthings.com/v1/devices",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if res.ok:
            data = res.json()
            for dev in data.get("items", []):
                device_type = dev.get("deviceTypeName", "").lower()
                name = dev.get("name", "").lower()
                label = dev.get("label", "").lower()
                if any(k in device_type or k in name or k in label for k in ["fridge", "refrigerator", "familyhub", "hub"]):
                    loc = dev.get("locationId")
                    if loc:
                        _LOGGER.info("Auto-discovered Location ID from device '%s': %s", dev.get("label") or dev.get("name"), loc)
                        return loc
            items = data.get("items", [])
            if items and items[0].get("locationId"):
                loc = items[0].get("locationId")
                _LOGGER.info("Auto-discovered Location ID from primary device: %s", loc)
                return loc
    except Exception as err:
        _LOGGER.debug("Could not auto-discover location ID via SmartThings API: %s", err)

    return None


def obtain_kics_token(
    session_file: str = "smartthings_session.json",
    client_id: str = DEFAULT_CLIENT_ID,
    email: str = "",
    password: str = "",
    headless: bool = True,
) -> str:
    """Acquire an OAuth Bearer token for client ID using Playwright with saved session."""
    session_path = os.path.expanduser(session_file)

    oauth_url = (
        f"{AUTH_BASE}/auth/oauth2/authorize?"
        f"response_type=code&"
        f"client_id={client_id}&"
        f"redirect_uri=https://kics-epa.samsungepa.com/api/oauth/callback&"
        f"scope=user"
    )

    _LOGGER.info("Initiating OAuth code exchange for Client ID %s...", client_id)

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
                "Mozilla/5.0 (Linux; Android 17; sdk_gphone16k_x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
                "Chrome/149.0.7827.5 Mobile Safari/537.36"
            ),
            "locale": "en-US",
        }

        if os.path.exists(session_path):
            _LOGGER.info("Loading saved session from %s", session_path)
            context_kwargs["storage_state"] = session_path

        context = browser.new_context(**context_kwargs)
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        """)

        page = context.new_page()
        captured_code: Optional[str] = None
        last_response_text: str = ""

        def handle_response(response):
            nonlocal captured_code, last_response_text
            url = response.url
            if "code=" in url and not captured_code:
                parsed = urllib.parse.urlparse(url)
                params = urllib.parse.parse_qs(parsed.query)
                if "code" in params:
                    captured_code = params["code"][0]
                    _LOGGER.info("Intercepted OAuth Authorization Code.")
            elif "samsungosp.com" in url or "kics-epa" in url:
                try:
                    last_response_text = response.text()
                except Exception:
                    pass

        page.on("response", handle_response)

        try:
            page.goto(oauth_url, timeout=45000)
            time.sleep(3)

            # Auto-click consent / Allow / Agree buttons if presented
            for sel in [
                "button:has-text('Agree')",
                "button:has-text('Allow')",
                "button:has-text('Accept')",
                "button#agreeBtn",
            ]:
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible():
                        _LOGGER.info("Clicking OAuth consent button '%s'...", sel)
                        btn.click()
                        time.sleep(3)
                        break
                except Exception:
                    pass

            # If login required and credentials provided
            if "signin" in page.url.lower() or "login" in page.url.lower():
                if not email or not password:
                    raise RuntimeError("Active session expired; email/password required.")
                _LOGGER.info("Submitting login credentials...")
                page.locator("input[type='email'], input#account").first.fill(email)
                page.keyboard.press("Enter")
                time.sleep(2)
                page.locator("input[type='password'], input#password").first.fill(password)
                page.keyboard.press("Enter")
                time.sleep(5)

            # Check if redirection captured the code
            if not captured_code and "code=" in page.url:
                parsed = urllib.parse.urlparse(page.url)
                params = urllib.parse.parse_qs(parsed.query)
                captured_code = params.get("code", [None])[0]

            if not captured_code:
                # Log detailed error diagnostics
                _LOGGER.warning("Could not capture code. Final URL: %s", page.url)
                try:
                    body_text = page.inner_text("body")
                    _LOGGER.warning("Page body content: %s", body_text[:300])
                    page.screenshot(path="/tmp/food_circles_oauth_error.png")
                except Exception:
                    pass

            if session_path and os.path.exists(os.path.dirname(os.path.abspath(session_path))):
                try:
                    context.storage_state(path=session_path)
                except Exception:
                    pass

        finally:
            browser.close()

    if not captured_code:
        raise RuntimeError(
            "Failed to capture OAuth authorization code. "
            "If Samsung returned AUT_1005 (userauth_token required), provide an active token via --token."
        )

    # Exchange authorization code for token
    token_url = f"{AUTH_BASE}/auth/oauth2/token"
    token_payload = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": captured_code,
        "redirect_uri": "https://kics-epa.samsungepa.com/api/oauth/callback",
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    _LOGGER.info("Exchanging authorization code for Access Token...")
    res = requests.post(token_url, data=token_payload, headers=headers, timeout=15)
    if not res.ok:
        raise RuntimeError(f"Token exchange failed ({res.status_code}): {res.text}")

    token_data = res.json()
    access_token = token_data.get("access_token")
    if not access_token:
        raise RuntimeError("No access_token found in token response.")

    return access_token



def download_image_with_retry(url: str, dest_file: str, max_retries: int = 3, timeout: int = 25) -> bool:
    """Download an image with automatic retries and backoff to handle transient timeouts."""
    if not url:
        return False
    for attempt in range(1, max_retries + 1):
        try:
            res = requests.get(url, timeout=timeout)
            if res.ok and len(res.content) > 0:
                with open(dest_file, "wb") as f:
                    f.write(res.content)
                return True
            _LOGGER.warning("Download HTTP %d on attempt %d for %s", res.status_code, attempt, dest_file)
        except Exception as err:
            if attempt == max_retries:
                _LOGGER.warning("Download timed out/failed on final attempt (%d/%d) for %s: %s", attempt, max_retries, dest_file, err)
            else:
                _LOGGER.debug("Transient download error (attempt %d/%d) for %s: %s. Retrying...", attempt, max_retries, dest_file, err)
        if attempt < max_retries:
            time.sleep(1.0 * attempt)
    return False


def load_whisk_stock_map(rotator_url: str = "http://127.0.0.1:8765/food_token") -> Dict[str, str]:
    """Attempt to load Whisk inventory to map item IDs / names to stock food photography."""
    whisk_token = None
    try:
        r = requests.get(rotator_url, timeout=2)
        if r.ok:
            whisk_token = r.json().get("token")
    except Exception:
        pass

    if not whisk_token:
        for p in [
            "samsung_food_token.txt",
            os.path.join(os.path.dirname(__file__), "samsung_food_token.txt"),
            os.path.join(os.path.dirname(__file__), "../tools/pat_rotator/samsung_food_token.txt"),
            os.path.expanduser("~/smartthings_pat_rotator/samsung_food_token.txt"),
        ]:
            if os.path.exists(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        tok = f.read().strip()
                        if tok:
                            whisk_token = tok
                            break
                except Exception:
                    pass

    if not whisk_token:
        return {}

    clean_auth = whisk_token if whisk_token.startswith("Bearer ") or whisk_token.startswith("Token ") else f"Bearer {whisk_token}"
    raw_token = whisk_token.replace("Bearer ", "").replace("Token ", "").strip()
    headers = {
        "Authorization": clean_auth,
        "x-whisk-token": raw_token,
        "Accept": "application/json",
    }
    stock_map = {}
    try:
        r = requests.get("https://api.whisk.com/foodlist/v2", headers=headers, timeout=10)
        if r.ok:
            for itm in r.json().get("items", []):
                itm_id = itm.get("id")
                c = itm.get("content", {})
                photo = c.get("image_url") or c.get("photo_url")
                name = c.get("name", "").lower()
                if itm_id and photo:
                    stock_map[itm_id] = photo
                if name and photo:
                    stock_map[name] = photo
            _LOGGER.info("Loaded %d Whisk stock food photos for image fallback.", len(stock_map))
    except Exception as err:
        _LOGGER.debug("Could not fetch Whisk stock map: %s", err)

    return stock_map


def fetch_and_download_food_circles(
    token: str,
    location_id: str,
    client_id: str = DEFAULT_CLIENT_ID,
    sa_auth_url: str = SA_AUTH_URL,
    output_dir: str = "food_circles_live",
    token_cache_file: Optional[str] = "active_bearer_token.txt"
) -> int:
    """Fetch food list from kics-epa and download circular thumbnail crops with stock image fallback."""
    os.makedirs(output_dir, exist_ok=True)

    auth_val = token.strip() if token.startswith("Bearer ") else f"Bearer {token.strip()}"

    headers = {
        "authorization": auth_val,
        "kics-sa-auth-url": sa_auth_url,
        "kics-sa-app-id": client_id,
        "kics-location": location_id,
        "kics-client-platform": "android",
        "kics-client-version": "KS.ST.2.11-20",
        "accept": "application/json, text/plain, */*",
        "user-agent": "Mozilla/5.0 (Linux; Android 17; sdk_gphone16k_x86_64) AppleWebKit/537.36",
    }

    _LOGGER.info("Requesting food list from %s (Location: %s)...", API_ENDPOINT, location_id)
    r = requests.get(API_ENDPOINT, headers=headers, timeout=15)

    # Invalidate cached token on 401/403
    if r.status_code in (401, 403) and token_cache_file and os.path.exists(token_cache_file):
        os.remove(token_cache_file)
        raise PermissionError(f"Access token expired or unauthorized (HTTP {r.status_code}).")

    r.raise_for_status()

    payload = r.json()
    items: List[Dict] = payload.get("foodData", {}).get("items", [])
    _LOGGER.info("Retrieved %d food circle items from backend.", len(items))

    # Pre-load Whisk stock photo map in case any camera circle times out
    stock_map = load_whisk_stock_map()

    # Save manifest alongside images
    manifest_path = os.path.join(output_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2)

    downloaded = 0
    for idx, it in enumerate(items, start=1):
        item_id = it.get("id", "")
        content = it.get("content", {})
        raw_name = content.get("name", f"item_{idx}")
        # Clean filesystem-unfriendly characters
        name = re.sub(r"[^\w\-_\.]", "_", raw_name).strip("_") or f"item_{idx}"
        image_url = content.get("imageUrl")
        dest_file = os.path.join(output_dir, f"{idx:02d}_{name}.jpg")

        # 1. Attempt downloading the live camera circular crop (with retries)
        downloaded_ok = False
        if image_url:
            downloaded_ok = download_image_with_retry(image_url, dest_file, max_retries=3, timeout=25)

        if downloaded_ok:
            _LOGGER.info("[%02d/%02d] Downloaded camera circle: %s", idx, len(items), dest_file)
            downloaded += 1
            continue

        # 2. Fall back to Whisk stock food photo if camera circle timed out or failed
        stock_url = stock_map.get(item_id) or stock_map.get(raw_name.lower())
        if stock_url:
            _LOGGER.info("[%02d/%02d] Camera crop timed out for '%s'. Downloading Whisk stock fallback...", idx, len(items), raw_name)
            fallback_ok = download_image_with_retry(stock_url, dest_file, max_retries=3, timeout=25)
            if fallback_ok:
                _LOGGER.info("[%02d/%02d] Successfully downloaded STOCK FALLBACK for %s: %s", idx, len(items), raw_name, dest_file)
                downloaded += 1
            else:
                _LOGGER.warning("[%02d/%02d] Failed to download both live circle and stock fallback for %s", idx, len(items), raw_name)
        else:
            _LOGGER.warning("[%02d/%02d] Failed to download camera circle for %s (no stock fallback available)", idx, len(items), raw_name)

    return downloaded


def resolve_file_path(requested_path: str, candidate_paths: List[str]) -> str:
    """Find the first existing path among candidates, or return requested_path."""
    if os.path.exists(requested_path):
        return requested_path
    for p in candidate_paths:
        expanded = os.path.expanduser(p)
        if os.path.exists(expanded):
            return expanded
    return requested_path


def main():
    parser = argparse.ArgumentParser(description="Download Family Hub Food Circles")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    parser.add_argument("--session", default="smartthings_session.json", help="Path to smartthings_session.json")
    parser.add_argument("--token-cache", default="active_bearer_token.txt", help="Cache file for active token")
    parser.add_argument("--token", default=None, help="Explicit active Bearer token (bypasses OAuth/cache)")
    parser.add_argument("--location-id", default=None, help="SmartThings Location ID")
    parser.add_argument("--client-id", default=DEFAULT_CLIENT_ID, help="Samsung KICS Client ID")
    parser.add_argument("--rotator-url", default="http://127.0.0.1:8765/pat", help="URL of local PAT rotator service")
    parser.add_argument("--output", default="food_circles_live", help="Output directory for thumbnails")
    parser.add_argument("--no-headless", action="store_true", help="Run browser visibly")
    args = parser.parse_args()

    # Resolve config and session paths
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_file = resolve_file_path(
        args.config,
        [
            "config.json",
            os.path.join(script_dir, "../config.json"),
            os.path.join(script_dir, "../tools/pat_rotator/config.json"),
            "~/smartthings_pat_rotator/config.json",
        ],
    )
    session_file = resolve_file_path(
        args.session,
        [
            "smartthings_session.json",
            os.path.join(script_dir, "../tools/pat_rotator/smartthings_session.json"),
            "~/smartthings_pat_rotator/smartthings_session.json",
        ],
    )

    email = ""
    password = ""
    config_location_id = None
    if os.path.exists(config_file):
        try:
            with open(config_file, "r", encoding="utf-8") as cf:
                cfg = json.load(cf)
                email = cfg.get("samsung_email", "")
                password = cfg.get("samsung_password", "")
                config_location_id = cfg.get("location_id")
        except Exception as err:
            _LOGGER.warning("Could not parse config (%s): %s", config_file, err)

    # Determine Location ID
    location_id = args.location_id or config_location_id
    if not location_id:
        _LOGGER.info("Querying local PAT rotator service for Location ID...")
        location_id = discover_location_id(rotator_url=args.rotator_url)
    if not location_id:
        if DEFAULT_LOCATION_ID:
            location_id = DEFAULT_LOCATION_ID
            _LOGGER.info("Using default Location ID: %s", location_id)
        else:
            _LOGGER.error("No Location ID could be discovered. Please specify via --location-id or set 'location_id' in config.json.")
            sys.exit(1)
    else:
        _LOGGER.info("Active Location ID: %s", location_id)

    # Check for explicitly supplied token via CLI
    token = None
    if args.token:
        token = args.token.strip()
        with open(args.token_cache, "w", encoding="utf-8") as tf:
            tf.write(token)
        _LOGGER.info("Saved explicit token from --token to %s", args.token_cache)
    elif os.path.exists(args.token_cache):
        with open(args.token_cache, "r", encoding="utf-8") as tf:
            token = tf.read().strip()
        if token:
            _LOGGER.info("Loaded cached token from %s", args.token_cache)

    # 1. Try using active token
    if token:
        try:
            fetch_and_download_food_circles(
                token,
                location_id=location_id,
                client_id=args.client_id,
                output_dir=args.output,
                token_cache_file=args.token_cache,
            )
            _LOGGER.info("Execution complete.")
            return
        except PermissionError as pe:
            _LOGGER.info("Cached token invalid (%s); requesting fresh token...", pe)
        except Exception as err:
            _LOGGER.warning("Attempt with cached token failed (%s); refreshing...", err)

    # 2. Acquire fresh token via Playwright
    token = obtain_kics_token(
        session_file=session_file,
        client_id=args.client_id,
        email=email,
        password=password,
        headless=not args.no_headless,
    )

    with open(args.token_cache, "w", encoding="utf-8") as tf:
        tf.write(token)

    fetch_and_download_food_circles(
        token,
        location_id=location_id,
        client_id=args.client_id,
        output_dir=args.output,
        token_cache_file=args.token_cache,
    )
    _LOGGER.info("Execution complete.")


if __name__ == "__main__":
    main()
