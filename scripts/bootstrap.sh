#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install .
.venv/bin/python -m playwright install chromium

echo
echo "homework-watcher is ready."
echo "Use: source .venv/bin/activate"
echo "Or:  .venv/bin/hw --help"
