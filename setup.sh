#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "Creating .venv..."
  python3 -m venv .venv
fi

echo "Installing Python dependencies..."
.venv/bin/python -m pip install -r requirements.txt

echo "Installing Playwright Chromium..."
.venv/bin/python -m playwright install chromium

echo "Ready. Generate a world with: .venv/bin/python generate_world.py"
