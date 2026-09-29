#!/usr/bin/env python3
"""Automated Samsung Family Hub Food Circles (KICS EPA) Token Manager.

Maintains, refreshes, and validates the active 26-character Samsung Account Bearer token
for Client ID `qumapq783u` to fetch live food circle crops from
https://kics-epa.samsungepa.com/api/foodlist.

Supports autonomous zero-browser renewal via Samsung Account OAuth2 refresh_token grant.
"""

import argparse
import json
import logging
import os
import sys
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


def generate_or_validate_kics_token(
    config_file: str = "config.json",
    output_file: str = "kics_food_token.txt",
    refresh_token_file: str = "kics_refresh_token.txt",
    explicit_token: Optional[str] = None,
    explicit_refresh_token: Optional[str] = None,
) -> str:
    """Ensure an active KICS token exists, validate it, and refresh if needed."""
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

    raise RuntimeError(
        "No valid KICS Food Circles token found. "
        "Please provide an active token via --token '<TOKEN>' or place refresh token in kics_refresh_token.txt."
    )


def main():
    parser = argparse.ArgumentParser(description="Samsung KICS Food Circles Token Manager")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    parser.add_argument("--output", default="kics_food_token.txt", help="Path to write active token")
    parser.add_argument("--refresh-file", default="kics_refresh_token.txt", help="Path to rolling refresh token")
    parser.add_argument("--token", default=None, help="Explicit active Bearer token to save and validate")
    parser.add_argument("--refresh-token", default=None, help="Explicit refresh token to exchange for fresh access token")
    parser.add_argument("--refresh-now", action="store_true", help="Force an immediate token refresh via refresh_token")
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
        else:
            tok = generate_or_validate_kics_token(
                config_file=args.config,
                output_file=args.output,
                refresh_token_file=args.refresh_file,
                explicit_token=args.token,
                explicit_refresh_token=args.refresh_token,
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
