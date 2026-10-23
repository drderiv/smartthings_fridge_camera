#!/usr/bin/env bash
# Create a virtualenv, install dependencies and Chromium, and seed config.json.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

if [ ! -d .venv ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv || {
        echo "python3 -m venv failed. On Debian/Ubuntu: sudo apt install python3-venv" >&2
        exit 1
    }
fi

echo "Installing dependencies..."
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "Installing Playwright Chromium..."
.venv/bin/playwright install chromium

if [ ! -f config.json ]; then
    cp config.example.json config.json
    chmod 600 config.json
    echo "Created config.json. Edit it with your Samsung account details."
fi

echo "Done. Start the service with: .venv/bin/python main.py"
