#!/usr/bin/env python3
"""Unified Runner for SmartThings PAT Rotator & REST Server.

Runs the local HTTP REST server on port 8765 and automatically triggers
PAT generation every N hours (default 23 hours) in the background.
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

# Import local generator
from generate_pat import generate_pat

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
TOKEN_FILE = os.path.join(BASE_DIR, "smartthings_pat.txt")
SESSION_FILE = os.path.join(BASE_DIR, "smartthings_session.json")
LOG_FILE = os.path.join(BASE_DIR, "rotator.log")

# Setup dual logging (Console + File)
log_formatter = logging.Formatter(
    fmt="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

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
        # Flush existing file handlers before reading/writing
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
        # Calculate days until next Sunday (Python weekday: Monday=0, Sunday=6)
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


class PATHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/pat", "/pat/"):
            if os.path.exists(TOKEN_FILE):
                try:
                    with open(TOKEN_FILE, "r") as f:
                        token = f.read().strip()
                    if token:
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({"token": token, "status": "ok"}).encode("utf-8"))
                        _LOGGER.info("Served PAT to client %s", self.client_address[0])
                        return
                except Exception as err:
                    _LOGGER.error("Error reading token file: %s", err)

            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "No token currently available"}).encode("utf-8"))
        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Not Found")

    def log_message(self, format, *args):
        pass


def rotation_loop(email: str, password: str, interval_hours: int = 23):
    """Background thread that runs generate_pat periodically."""
    # Mint token immediately on service startup
    try:
        _LOGGER.info("Executing initial startup PAT generation...")
        new_token = generate_pat(
            email=email,
            password=password,
            output_file=TOKEN_FILE,
            session_file=SESSION_FILE,
            headless=True,
        )
        _LOGGER.info("PAT successfully generated: %s...", new_token[:8])
    except Exception as err:
        _LOGGER.error("Failed to generate PAT on initial startup: %s", err)

    while True:
        sleep_seconds = interval_hours * 3600
        _LOGGER.info("Sleeping for %d hours until next scheduled PAT rotation...", interval_hours)
        time.sleep(sleep_seconds)

        try:
            _LOGGER.info("Executing scheduled %d-hour PAT generation...", interval_hours)
            new_token = generate_pat(
                email=email,
                password=password,
                output_file=TOKEN_FILE,
                session_file=SESSION_FILE,
                headless=True,
            )
            _LOGGER.info("PAT successfully updated: %s...", new_token[:8])
        except Exception as err:
            _LOGGER.error("Failed to generate PAT in background loop: %s", err)
            _LOGGER.info("Will retry in 10 minutes...")
            time.sleep(600)
            continue


def main():
    if not os.path.exists(CONFIG_FILE):
        _LOGGER.error("Config file not found: %s", CONFIG_FILE)
        _LOGGER.error("Please copy config.example.json to config.json and fill in your Samsung credentials.")
        sys.exit(1)

    with open(CONFIG_FILE, "r") as f:
        config = json.load(f)

    email = config.get("samsung_email")
    password = config.get("samsung_password")
    port = int(config.get("port", 8765))
    interval_hours = int(config.get("rotation_interval_hours", 23))

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

    # Start rotation thread in background
    rotator_thread = threading.Thread(
        target=rotation_loop,
        args=(email, password, interval_hours),
        daemon=True,
    )
    rotator_thread.start()

    # Start HTTP server
    server = HTTPServer(("0.0.0.0", port), PATHandler)
    _LOGGER.info("==========================================================")
    _LOGGER.info(" SmartThings PAT Rotator Server running on port %d", port)
    _LOGGER.info(" Home Assistant endpoint: http://<SERVER_IP>:%d/pat", port)
    _LOGGER.info(" Rotation interval: %d hours", interval_hours)
    _LOGGER.info(" Token file: %s", TOKEN_FILE)
    _LOGGER.info(" Log file: %s (auto-pruned weekly)", LOG_FILE)
    _LOGGER.info("==========================================================")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _LOGGER.info("Shutting down server...")
        server.server_close()


if __name__ == "__main__":
    main()
