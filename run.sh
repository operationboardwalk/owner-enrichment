#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

echo "Installing dependencies..."
pip install -q -r requirements.txt

echo "Starting Owner Enrichment app at http://localhost:5000"

# Open browser in background (works on macOS + Linux with common browsers)
(
  sleep 1.5
  if command -v xdg-open &>/dev/null; then
    xdg-open http://localhost:5000
  elif command -v open &>/dev/null; then
    open http://localhost:5000
  fi
) &

python app.py
