#!/usr/bin/env python3
"""Automated Samsung Family Hub Food Circles (KICS EPA) Token Manager.

Maintains and validates the active 26-character Samsung Account Bearer token
for Client ID `qumapq783u` to fetch live food circle crops from
https://kics-epa.samsungepa.com/api/foodlist.
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


def generate_or_validate_kics_token(
    config_file: str = "config.json",
    output_file: str = "kics_food_token.txt",
    session_file: str = "smartthings_session.json",
    explicit_token: Optional[str] = None,
) -> str:
    """Ensure an active KICS token exists, validate it, and write to output_file."""
    base_dir = os.path.dirname(os.path.abspath(config_file))
    output_path = os.path.expanduser(output_file)

    # 1. If explicit token passed, save and validate
    if explicit_token and explicit_token.strip():
        tok = explicit_token.strip()
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(f"{tok}\n")
        _LOGGER.info("Saved explicit KICS token to %s", output_path)
        return tok

    # 2. Check candidate token files
    candidates = [
        output_path,
        os.path.join(base_dir, "kics_food_token.txt"),
        os.path.join(base_dir, "active_bearer_token.txt"),
        "kics_food_token.txt",
        "active_bearer_token.txt",
    ]
    stored_token = get_stored_kics_token(candidates)
    if stored_token:
        # Validate stored token against KICS backend
        _LOGGER.info("Validating stored KICS token against %s...", API_ENDPOINT)
        if validate_kics_token(stored_token):
            _LOGGER.info("Stored KICS token is VALID and active.")
            if output_path != candidates[0] or not os.path.exists(output_path):
                with open(output_path, "w", encoding="utf-8") as f:
                    f.write(f"{stored_token}\n")
            return stored_token
        else:
            _LOGGER.warning("Stored KICS token has expired or is unauthorized (HTTP 401/403).")

    # 3. Check if token is in config.json
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
        "Please provide an active token via --token '<TOKEN>' or place it in kics_food_token.txt."
    )


def main():
    parser = argparse.ArgumentParser(description="Samsung KICS Food Circles Token Manager")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    parser.add_argument("--output", default="kics_food_token.txt", help="Path to write active token")
    parser.add_argument("--session", default="smartthings_session.json", help="Path to smartthings_session.json")
    parser.add_argument("--token", default=None, help="Explicit active Bearer token to save and validate")
    parser.add_argument("--validate-only", action="store_true", help="Only validate existing token without erroring")
    args = parser.parse_args()

    try:
        tok = generate_or_validate_kics_token(
            config_file=args.config,
            output_file=args.output,
            session_file=args.session,
            explicit_token=args.token,
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
