#!/usr/bin/env python3
"""Unified Runner for SmartThings PAT & Samsung Food Token Rotator & REST Server.

Runs the local HTTP REST server on port 8765 and automatically triggers:
1. SmartThings PAT generation every N hours (default 23 hours).
2. Samsung Food (Whisk) token generation every N days (default 28 days).
Includes built-in file logging and automated weekly log pruning.
"""

import os
import sys
import time
import json
import logging
import threading
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

# Import local generators
from generate_pat import generate_pat
from generate_food_token import generate_food_token

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
TOKEN_FILE = os.path.join(BASE_DIR, "smartthings_pat.txt")
FOOD_TOKEN_FILE = os.path.join(BASE_DIR, "samsung_food_token.txt")
SESSION_FILE = os.path.join(BASE_DIR, "smartthings_session.json")
LOG_FILE = os.path.join(BASE_DIR, "rotator.log")

# Setup dual logging (Console + File)
log_formatter = logging.Formatter(
    fmt="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.handlers.clear()

# Console handler
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(log_formatter)
root_logger.addHandler(console_handler)

# File handler
file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
file_handler.setFormatter(log_formatter)
root_logger.addHandler(file_handler)

_LOGGER = logging.getLogger("pat_rotator")


def prune_log_file(log_file: str = LOG_FILE, max_lines: int = 100) -> None:
    """Trim log file to the most recent max_lines lines."""
    if not os.path.exists(log_file):
        return
    try:
        for handler in logging.root.handlers:
            if isinstance(handler, logging.FileHandler):
                handler.flush()

        with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

        if len(lines) > max_lines:
            trimmed = lines[-max_lines:]
            with open(log_file, "w", encoding="utf-8") as f:
                f.writelines(trimmed)
            _LOGGER.info(
                "Weekly log maintenance: Trimmed %s from %d lines down to the most recent %d lines.",
                os.path.basename(log_file),
                len(lines),
                len(trimmed),
            )
    except Exception as err:
        _LOGGER.warning("Could not prune log file %s: %s", log_file, err)


def weekly_log_cleanup_loop(log_file: str = LOG_FILE, max_lines: int = 100) -> None:
    """Background thread to prune the log file every Sunday at midnight."""
    while True:
        now = datetime.now()
        days_ahead = (6 - now.weekday()) % 7
        next_sunday_midnight = (now + timedelta(days=days_ahead)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        if next_sunday_midnight <= now:
            next_sunday_midnight += timedelta(days=7)

        sleep_seconds = (next_sunday_midnight - now).total_seconds()
        _LOGGER.debug(
            "Next scheduled log cleanup on Sunday at midnight (%s, in %.1f hours)",
            next_sunday_midnight.strftime("%Y-%m-%d %H:%M:%S"),
            sleep_seconds / 3600.0,
        )
        time.sleep(sleep_seconds)

        try:
            prune_log_file(log_file, max_lines)
        except Exception as e:
            _LOGGER.warning("Error during scheduled log cleanup: %s", e)


def get_token_age(token_file: str) -> float | None:
    """Return age of token file in seconds, or None if file doesn't exist or is empty."""
    if not os.path.exists(token_file):
        return None
    try:
        with open(token_file, "r", encoding="utf-8") as f:
            tok = f.read().strip()
        if not tok:
            return None
        return time.time() - os.path.getmtime(token_file)
    except Exception:
        return None


def is_token_fresh(token_file: str, max_age_seconds: float) -> bool:
    """Check if a token file exists, is non-empty, and newer than max_age_seconds."""
    age = get_token_age(token_file)
    return age is not None and age < max_age_seconds


class PATHandler(BaseHTTPRequestHandler):
    """HTTP request handler serving PAT and Samsung Food tokens."""

    def _read_file_token(self, filepath: str) -> str | None:
        if os.path.exists(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    tok = f.read().strip()
                if tok:
                    return tok
            except Exception as err:
                _LOGGER.error("Error reading %s: %s", filepath, err)
        return None

    def do_GET(self):
        # 1. SmartThings PAT endpoint
        if self.path in ("/pat", "/pat/"):
            token = self._read_file_token(TOKEN_FILE)
            if token:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                response = {"token": token, "status": "ok", "type": "smartthings_pat"}
                self.wfile.write(json.dumps(response).encode("utf-8"))
                _LOGGER.info("Served SmartThings PAT to client %s", self.client_address[0])
            else:
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                response = {"error": "PAT not generated yet", "status": "unavailable"}
                self.wfile.write(json.dumps(response).encode("utf-8"))
                _LOGGER.warning("PAT requested by %s but token file is empty or missing.", self.client_address[0])

        # 2. Samsung Food (Whisk) Token endpoint
        elif self.path in ("/food_token", "/food_token/"):
            token = self._read_file_token(FOOD_TOKEN_FILE)
            if token:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                response = {"token": token, "status": "ok", "type": "samsung_food_token"}
                self.wfile.write(json.dumps(response).encode("utf-8"))
                _LOGGER.info("Served Samsung Food token to client %s", self.client_address[0])
            else:
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                response = {"error": "Samsung Food token not generated yet", "status": "unavailable"}
                self.wfile.write(json.dumps(response).encode("utf-8"))
                _LOGGER.warning("Samsung Food token requested by %s but token file is empty or missing.", self.client_address[0])

        # 3. Combined Tokens endpoint
        elif self.path in ("/tokens", "/tokens/"):
            pat = self._read_file_token(TOKEN_FILE)
            food_token = self._read_file_token(FOOD_TOKEN_FILE)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            response = {
                "smartthings_pat": pat,
                "samsung_food_token": food_token,
                "pat": pat,
                "food_token": food_token,
                "status": "ok" if (pat or food_token) else "empty",
            }
            self.wfile.write(json.dumps(response).encode("utf-8"))
            _LOGGER.info("Served combined tokens to client %s", self.client_address[0])

        # 4. Health endpoint
        elif self.path in ("/health", "/health/"):
            pat_ok = self._read_file_token(TOKEN_FILE) is not None
            food_ok = self._read_file_token(FOOD_TOKEN_FILE) is not None
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            response = {
                "status": "healthy",
                "smartthings_pat_available": pat_ok,
                "samsung_food_token_available": food_ok,
            }
            self.wfile.write(json.dumps(response).encode("utf-8"))

        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Not Found")

    def log_message(self, format, *args):
        pass


def pat_rotation_loop(email: str, password: str, interval_hours: int = 23, two_factor_method: str = "device"):
    """Background thread that runs generate_pat periodically."""
    max_seconds = interval_hours * 3600
    age = get_token_age(TOKEN_FILE)
    if age is not None and age < max_seconds:
        remaining = max_seconds - age
        _LOGGER.info(
            "Existing PAT token in %s is still fresh (age: %.1f hours). Scheduling next rotation in %.1f hours.",
            os.path.basename(TOKEN_FILE),
            age / 3600.0,
            remaining / 3600.0,
        )
        time.sleep(remaining)
    else:
        _LOGGER.info("No fresh PAT token found. Executing startup generation...")

    while True:
        # Check if an external run (e.g. manual generate_pat.py) already refreshed the token
        age = get_token_age(TOKEN_FILE)
        if age is not None and age < (max_seconds - 300):
            remaining = max_seconds - age
            _LOGGER.info(
                "Fresh PAT token detected in %s (age: %.1f hours). Scheduling next rotation in %.1f hours.",
                os.path.basename(TOKEN_FILE),
                age / 3600.0,
                remaining / 3600.0,
            )
            time.sleep(remaining)
            continue

        try:
            _LOGGER.info("Executing scheduled %d-hour PAT generation...", interval_hours)
            new_token = generate_pat(
                email=email,
                password=password,
                output_file=TOKEN_FILE,
                session_file=SESSION_FILE,
                two_factor_method=two_factor_method,
                headless=True,
            )
            _LOGGER.info("PAT successfully updated: %s...", new_token[:8])
            _LOGGER.info("Sleeping for %d hours until next scheduled PAT rotation...", interval_hours)
            time.sleep(max_seconds)
        except Exception as err:
            _LOGGER.error("Failed to generate PAT in background loop: %s", err)
            _LOGGER.info("Will retry in 10 minutes...")
            time.sleep(600)


def food_rotation_loop(email: str, password: str, interval_days: int = 28):
    """Background thread that runs generate_food_token periodically."""
    max_seconds = interval_days * 86400
    age = get_token_age(FOOD_TOKEN_FILE)
    if age is not None and age < max_seconds:
        remaining = max_seconds - age
        _LOGGER.info(
            "Existing Samsung Food token in %s is still fresh (age: %.1f days). Scheduling next rotation in %.1f days.",
            os.path.basename(FOOD_TOKEN_FILE),
            age / 86400.0,
            remaining / 86400.0,
        )
        time.sleep(remaining)
    else:
        _LOGGER.info("No fresh Samsung Food token found. Executing startup generation...")

    while True:
        # Check if an external run (e.g. manual generate_food_token.py) already refreshed the token
        age = get_token_age(FOOD_TOKEN_FILE)
        if age is not None and age < (max_seconds - 3600):
            remaining = max_seconds - age
            _LOGGER.info(
                "Fresh Samsung Food token detected in %s (age: %.1f days). Scheduling next rotation in %.1f days.",
                os.path.basename(FOOD_TOKEN_FILE),
                age / 86400.0,
                remaining / 86400.0,
            )
            time.sleep(remaining)
            continue

        try:
            _LOGGER.info("Executing scheduled %d-day Samsung Food token generation...", interval_days)
            new_token = generate_food_token(
                email=email,
                password=password,
                output_file=FOOD_TOKEN_FILE,
                session_file=SESSION_FILE,
                headless=True,
            )
            _LOGGER.info("Samsung Food token successfully updated: %s...", new_token[:8])
            _LOGGER.info("Sleeping for %d days until next scheduled Samsung Food token rotation...", interval_days)
            time.sleep(max_seconds)
        except Exception as err:
            _LOGGER.error("Failed to generate Samsung Food token in background loop: %s", err)
            _LOGGER.info("Will retry in 1 hour...")
            time.sleep(3600)


def main():
    if not os.path.exists(CONFIG_FILE):
        _LOGGER.error("Config file not found: %s", CONFIG_FILE)
        _LOGGER.error("Please copy config.example.json to config.json and fill in your Samsung credentials.")
        sys.exit(1)

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        config = json.load(f)

    email = config.get("samsung_email")
    password = config.get("samsung_password")
    port = int(config.get("port", 8765))
    pat_interval_hours = int(config.get("rotation_interval_hours", 23))
    food_interval_days = int(config.get("food_rotation_interval_days", 28))

    two_factor_method = config.get("two_factor_method", "device")

    if not email or not password or email == "YOUR_SAMSUNG_EMAIL":
        _LOGGER.error("Please configure 'samsung_email' and 'samsung_password' in config.json")
        sys.exit(1)

    # Start weekly log cleanup thread in background
    cleanup_thread = threading.Thread(
        target=weekly_log_cleanup_loop,
        args=(LOG_FILE, 100),
        daemon=True,
    )
    cleanup_thread.start()

    # Start PAT rotation thread in background (every 23 hours)
    pat_thread = threading.Thread(
        target=pat_rotation_loop,
        args=(email, password, pat_interval_hours, two_factor_method),
        daemon=True,
    )
    pat_thread.start()

    # Start Food token rotation thread in background (every 28 days)
    food_thread = threading.Thread(
        target=food_rotation_loop,
        args=(email, password, food_interval_days),
        daemon=True,
    )
    food_thread.start()

    # Start HTTP server
    server = HTTPServer(("0.0.0.0", port), PATHandler)
    _LOGGER.info("==========================================================")
    _LOGGER.info(" Samsung FamilyHub Dual Token Rotator Server (Port %d)", port)
    _LOGGER.info(" - SmartThings PAT:      http://<SERVER_IP>:%d/pat (every %dh)", port, pat_interval_hours)
    _LOGGER.info(" - Samsung Food Token:   http://<SERVER_IP>:%d/food_token (every %dd)", port, food_interval_days)
    _LOGGER.info(" - Combined Status:      http://<SERVER_IP>:%d/tokens", port)
    _LOGGER.info(" - 2FA Method:           %s", two_factor_method)
    _LOGGER.info(" - Log file:             %s (auto-pruned weekly)", LOG_FILE)
    _LOGGER.info("==========================================================")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _LOGGER.info("Shutting down server...")
        server.server_close()


if __name__ == "__main__":
    main()
