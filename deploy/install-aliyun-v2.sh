#!/usr/bin/env bash
set -Eeuo pipefail

export DEBIAN_FRONTEND=noninteractive
export PIP_INDEX_URL="${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}"
export PLAYWRIGHT_DOWNLOAD_HOST="${PLAYWRIGHT_DOWNLOAD_HOST:-https://npmmirror.com/mirrors/playwright}"

SRC_DIR="${SRC_DIR:-/opt/homework-watcher-src}"
APP_DIR="${APP_DIR:-/opt/homework-watcher-v2}"
ENV_FILE="${ENV_FILE:-/etc/homework-watcher-v2.env}"
PUBLIC_IP="${PUBLIC_IP:-8.141.109.80}"
REPO_URL="${REPO_URL:-https://github.com/ZimoZhang1216/homework-watcher.git}"
OLD_PROFILE_ROOT="${OLD_PROFILE_ROOT:-/var/lib/homework-watcher/web/users/1/browser-profiles}"
OLD_PROFILE_DIR="${OLD_PROFILE_DIR:-$OLD_PROFILE_ROOT/xiaoya}"
MIGRATE_OLD_PROFILE="${MIGRATE_OLD_PROFILE:-1}"
REQUESTED_NOVNC_PASSWORD="${NOVNC_PASSWORD:-}"

log() {
  printf '[%s] %s\n' "$(date '+%F %T')" "$*"
}

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this script as root, for example: sudo bash deploy/install-aliyun-v2.sh" >&2
  exit 2
fi

if [ -n "$REQUESTED_NOVNC_PASSWORD" ] && [ "${#REQUESTED_NOVNC_PASSWORD}" -gt 8 ]; then
  echo "NOVNC_PASSWORD must be 8 characters or fewer because VNC passwords are limited to 8 characters." >&2
  exit 2
fi

log "starting homework-watcher v2 deployment"

apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates \
  curl \
  fluxbox \
  git \
  nginx \
  novnc \
  openssl \
  procps \
  psmisc \
  rsync \
  software-properties-common \
  websockify \
  x11vnc \
  xvfb

python_bin=""
for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
    then
      python_bin="$(command -v "$candidate")"
      break
    fi
  fi
done

if [ -z "$python_bin" ]; then
  log "installing Python 3.11 from deadsnakes PPA"
  add-apt-repository -y ppa:deadsnakes/ppa
  apt-get update
  apt-get install -y --no-install-recommends python3.11 python3.11-venv python3.11-distutils
  python_bin="$(command -v python3.11)"
fi

if [ -f "$ENV_FILE" ]; then
  # Preserve operator-managed values such as custom paths or passwords.
  set +u
  # shellcheck disable=SC1090
  . "$ENV_FILE" || true
  set -u
fi

: "${APP_VERSION:=V2.0}"
: "${DATABASE_URL:=sqlite:///$APP_DIR/data/homework_watcher.sqlite3}"
: "${PLAYWRIGHT_USER_DATA_DIR:=$APP_DIR/data/playwright-user-data}"
: "${CONFIG_PATH:=$APP_DIR/config/platforms.yaml}"
: "${DEBUG_DUMP_DIR:=/var/log/homework-watcher-v2/debug}"
: "${LOGS_DIR:=$APP_DIR/logs}"
: "${HOST:=127.0.0.1}"
: "${PORT:=8080}"
if [ -n "$REQUESTED_NOVNC_PASSWORD" ]; then
  NOVNC_PASSWORD="$REQUESTED_NOVNC_PASSWORD"
else
  : "${NOVNC_PASSWORD:=}"
fi

if [ -d "$SRC_DIR/.git" ]; then
  log "updating repository in $SRC_DIR"
  git -C "$SRC_DIR" fetch --all --prune
  git -C "$SRC_DIR" reset --hard origin/main
else
  log "cloning repository into $SRC_DIR"
  rm -rf "$SRC_DIR"
  git clone --depth 1 "$REPO_URL" "$SRC_DIR"
fi
GIT_COMMIT="$(git -C "$SRC_DIR" rev-parse --short HEAD)"

log "stopping old web service if present"
systemctl stop homework-watcher-web >/dev/null 2>&1 || true

log "syncing v2 app to $APP_DIR"
mkdir -p "$APP_DIR" "$(dirname "$ENV_FILE")" "$DEBUG_DUMP_DIR"
rsync -a --delete \
  --exclude '.env' \
  --exclude '.venv' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude 'data/' \
  --exclude 'logs/' \
  --exclude 'homework_watcher_v2.egg-info/' \
  "$SRC_DIR/v2/" "$APP_DIR/"

mkdir -p "$APP_DIR/data" "$APP_DIR/logs" "$PLAYWRIGHT_USER_DATA_DIR" "$DEBUG_DUMP_DIR"

if [ "$MIGRATE_OLD_PROFILE" = "1" ] \
  && [ -d "$OLD_PROFILE_DIR" ] \
  && [ ! -d "$PLAYWRIGHT_USER_DATA_DIR/xiaoya/Default" ]; then
  log "copying existing Xiaoya browser profile from old deployment"
  mkdir -p "$PLAYWRIGHT_USER_DATA_DIR"
  cp -a "$OLD_PROFILE_DIR" "$PLAYWRIGHT_USER_DATA_DIR/xiaoya"
fi

if [ "$MIGRATE_OLD_PROFILE" = "1" ] \
  && [ -d "$OLD_PROFILE_ROOT/changjiang-yuketang" ] \
  && [ ! -d "$PLAYWRIGHT_USER_DATA_DIR/users/default/changjiang-yuketang/Default" ]; then
  log "copying existing Changjiang Yuketang browser profile from old deployment"
  mkdir -p "$PLAYWRIGHT_USER_DATA_DIR/users/default"
  cp -a "$OLD_PROFILE_ROOT/changjiang-yuketang" "$PLAYWRIGHT_USER_DATA_DIR/users/default/changjiang-yuketang"
fi

log "creating venv with $python_bin"
"$python_bin" -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install --upgrade pip setuptools wheel
"$APP_DIR/.venv/bin/python" -m pip install -e "$APP_DIR"

mkdir -p "$APP_DIR/ms-playwright"
if ! PLAYWRIGHT_BROWSERS_PATH="$APP_DIR/ms-playwright" "$APP_DIR/.venv/bin/python" -m playwright install --with-deps chromium; then
  log "playwright mirror failed; retrying with default download host"
  unset PLAYWRIGHT_DOWNLOAD_HOST
  PLAYWRIGHT_BROWSERS_PATH="$APP_DIR/ms-playwright" "$APP_DIR/.venv/bin/python" -m playwright install --with-deps chromium
fi

cat >"$ENV_FILE" <<ENV
APP_VERSION=$APP_VERSION
GIT_COMMIT=$GIT_COMMIT
DATABASE_URL=$DATABASE_URL
PLAYWRIGHT_USER_DATA_DIR=$PLAYWRIGHT_USER_DATA_DIR
CONFIG_PATH=$CONFIG_PATH
DEBUG_DUMP_DIR=$DEBUG_DUMP_DIR
LOGS_DIR=$LOGS_DIR
HOST=$HOST
PORT=$PORT
DISPLAY=:99
XVFB_SCREEN=1440x1000x24
PLAYWRIGHT_BROWSERS_PATH=$APP_DIR/ms-playwright
NOVNC_PASSWORD=$NOVNC_PASSWORD
ENV
chmod 600 "$ENV_FILE"

cat >/usr/local/bin/homework-watcher-v2-run-web <<'RUNNER'
#!/usr/bin/env bash
set -euo pipefail
set -a
. /etc/homework-watcher-v2.env
set +a
export PATH="/opt/homework-watcher-v2/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

pkill -f "Xvfb :99" >/dev/null 2>&1 || true
pkill -f "x11vnc.*:99" >/dev/null 2>&1 || true
pkill -f "websockify.*6080" >/dev/null 2>&1 || true
pkill -x fluxbox >/dev/null 2>&1 || true

Xvfb "${DISPLAY:-:99}" -screen 0 "${XVFB_SCREEN:-1440x1000x24}" -ac +extension RANDR >/tmp/hw-v2-xvfb.log 2>&1 &
fluxbox >/tmp/hw-v2-fluxbox.log 2>&1 &

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
  x11vnc -storepasswd "${NOVNC_PASSWORD}" /tmp/hw-v2-novnc.pass >/tmp/hw-v2-x11vnc-pass.log 2>&1
  vnc_args+=(-rfbauth /tmp/hw-v2-novnc.pass)
fi

x11vnc "${vnc_args[@]}" >/tmp/hw-v2-x11vnc.log 2>&1 &
websockify --web=/usr/share/novnc/ 127.0.0.1:6080 127.0.0.1:5900 >/tmp/hw-v2-websockify.log 2>&1 &

cd /opt/homework-watcher-v2
exec /opt/homework-watcher-v2/.venv/bin/python -m homework_watcher.app
RUNNER
chmod +x /usr/local/bin/homework-watcher-v2-run-web

cat >/usr/local/bin/homework-watcher-v2-login-xiaoya <<'LOGIN'
#!/usr/bin/env bash
set -euo pipefail
set -a
. /etc/homework-watcher-v2.env
set +a
cd /opt/homework-watcher-v2
exec /opt/homework-watcher-v2/.venv/bin/python -m homework_watcher.cli login-xiaoya
LOGIN
chmod +x /usr/local/bin/homework-watcher-v2-login-xiaoya

cat >/etc/systemd/system/homework-watcher-v2.service <<'SERVICE'
[Unit]
Description=homework-watcher v2 web app
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/homework-watcher-v2
EnvironmentFile=/etc/homework-watcher-v2.env
ExecStart=/usr/local/bin/homework-watcher-v2-run-web
Restart=always
RestartSec=5
KillMode=control-group
TimeoutStartSec=60
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
SERVICE

cat >/etc/nginx/sites-available/homework-watcher-v2 <<'NGINX'
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
    client_max_body_size 20m;

    location /vnc/ {
        proxy_pass http://127.0.0.1:6080/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400;
    }

    location /websockify {
        proxy_pass http://127.0.0.1:6080/websockify;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400;
    }

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300;
    }
}
NGINX

rm -f /etc/nginx/sites-enabled/default
rm -f /etc/nginx/sites-enabled/homework-watcher
ln -sf /etc/nginx/sites-available/homework-watcher-v2 /etc/nginx/sites-enabled/homework-watcher-v2

nginx -t
systemctl daemon-reload
systemctl disable --now homework-watcher-web >/dev/null 2>&1 || true
systemctl enable nginx
systemctl restart nginx
systemctl enable homework-watcher-v2
systemctl restart homework-watcher-v2

sleep 8

cat >/root/homework-watcher-v2-deployment.txt <<CREDS
homework-watcher v2 deployment
URL: http://$PUBLIC_IP/
Health: http://$PUBLIC_IP/health
noVNC URL: http://$PUBLIC_IP/vnc/vnc.html?autoconnect=1&resize=scale&path=vnc/websockify
Login helper: homework-watcher-v2-login-xiaoya
Environment file: $ENV_FILE
Service: homework-watcher-v2
Log: journalctl -u homework-watcher-v2 -n 200 --no-pager
Scan log: tail -n 160 $LOGS_DIR/scan.log
Old service stopped: homework-watcher-web
Old data kept: /var/lib/homework-watcher/web
CREDS
chmod 600 /root/homework-watcher-v2-deployment.txt

log "service status"
systemctl --no-pager --full status homework-watcher-v2 || true
ss -lntp | sed -n '1,160p' || true
curl -fsS http://127.0.0.1:8080/health || true
curl -fsS http://127.0.0.1/health || true

log "deployment complete"
cat /root/homework-watcher-v2-deployment.txt
