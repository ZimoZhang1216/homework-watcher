#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -x ".venv/bin/python" ]; then
  echo "Missing .venv. Run ./scripts/bootstrap.sh first." >&2
  exit 1
fi

".venv/bin/python" -m homework_watcher check \
  --scan \
  --calendar-sync \
  --calendar-name "作业提醒-iCloud" \
  --reminders-sync \
  --reminders-list "Reminders"
