#!/usr/bin/env bash
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

echo "Setting up SmartThings PAT Rotator in $DIR..."

if [ ! -d ".venv" ]; then
    echo "Creating Python virtual environment..."
    python3 -m venv .venv || (curl -sS https://bootstrap.pypa.io/get-pip.py | python3 && python3 -m venv .venv)
fi

echo "Installing dependencies..."
.venv/bin/pip install -r requirements.txt

echo "Installing Playwright Chromium browser..."
.venv/bin/playwright install chromium

if [ ! -f "config.json" ]; then
    echo "Creating config.json template..."
    cp config.example.json config.json
    echo "--> Please edit config.json with your Samsung Account email and password."
fi

echo "Setup complete! Run with: .venv/bin/python main.py"
