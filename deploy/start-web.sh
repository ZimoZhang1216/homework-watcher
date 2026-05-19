#!/usr/bin/env bash
set -euo pipefail

mkdir -p "${HW_WEB_DIR:-/data/web}"

if [ -z "${HW_WEB_NOVNC_URL:-}" ] && [ -n "${APP_DOMAIN:-}" ]; then
  export HW_WEB_NOVNC_URL="https://${APP_DOMAIN}/vnc/vnc.html?autoconnect=1&resize=scale"
fi

Xvfb "${DISPLAY:-:99}" -screen 0 "${XVFB_SCREEN:-1440x1000x24}" -ac +extension RANDR >/tmp/xvfb.log 2>&1 &
fluxbox >/tmp/fluxbox.log 2>&1 &

vnc_args=(
  -display "${DISPLAY:-:99}"
  -forever
  -shared
  -rfbport 5900
  -localhost
  -noxdamage
  -repeat
)

if [ -n "${NOVNC_PASSWORD:-}" ]; then
  if [ "${#NOVNC_PASSWORD}" -gt 8 ]; then
    echo "NOVNC_PASSWORD must be 8 characters or fewer because VNC passwords are limited to 8 characters." >&2
    exit 2
  fi
  x11vnc -storepasswd "${NOVNC_PASSWORD}" /tmp/novnc.pass >/tmp/x11vnc-pass.log 2>&1
  vnc_args+=(-rfbauth /tmp/novnc.pass)
fi

x11vnc "${vnc_args[@]}" >/tmp/x11vnc.log 2>&1 &
websockify --web=/usr/share/novnc/ 0.0.0.0:6080 127.0.0.1:5900 >/tmp/websockify.log 2>&1 &

exec hw-web
