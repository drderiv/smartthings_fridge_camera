#!/usr/bin/env python3
"""Lightweight REST micro-service to serve the latest SmartThings PAT to Home Assistant.

Home Assistant fetches GET http://<IP>:8765/pat, receives {"token": "..."},
and updates input_text.smartthings_pat.
"""

import os
import sys
import json
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = 8765
TOKEN_FILE = os.path.expanduser("~/.smartthings_pat")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_LOGGER = logging.getLogger("pat_server")


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
                        payload = json.dumps({"token": token, "status": "ok"})
                        self.wfile.write(payload.encode("utf-8"))
                        _LOGGER.info("Served valid PAT to client %s", self.client_address[0])
                        return
                except Exception as err:
                    _LOGGER.error("Error reading token file: %s", err)

            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "No token available in ~/.smartthings_pat"}).encode("utf-8"))
            _LOGGER.warning("GET /pat requested but ~/.smartthings_pat is missing or empty")
        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Not Found")

    def log_message(self, format, *args):
        # Suppress default noisy stderr access logs (handled in do_GET)
        pass


def main():
    server = HTTPServer(("0.0.0.0", PORT), PATHandler)
    _LOGGER.info("SmartThings PAT Server listening on http://0.0.0.0:%d/pat", PORT)
    _LOGGER.info("Reading token from: %s", TOKEN_FILE)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _LOGGER.info("Server stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
