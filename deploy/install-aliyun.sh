#!/usr/bin/env bash
set -Eeuo pipefail

export DEBIAN_FRONTEND=noninteractive
export PIP_INDEX_URL="${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}"
export PLAYWRIGHT_DOWNLOAD_HOST="${PLAYWRIGHT_DOWNLOAD_HOST:-https://npmmirror.com/mirrors/playwright}"

APP_DIR="${APP_DIR:-/opt/homework-watcher}"
ENV_DIR="${ENV_DIR:-/etc/homework-watcher}"
WEB_DIR="${WEB_DIR:-/var/lib/homework-watcher/web}"
PUBLIC_IP="${PUBLIC_IP:-8.141.109.80}"
REPO_URL="${REPO_URL:-https://github.com/ZimoZhang1216/homework-watcher.git}"
REQUESTED_NOVNC_PASSWORD="${NOVNC_PASSWORD:-}"

log() {
  printf '[%s] %s\n' "$(date '+%F %T')" "$*"
}

log "starting homework-watcher deployment"

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
  python3 \
  python3-pip \
  python3-venv \
  websockify \
  x11vnc \
  xvfb

mkdir -p "$ENV_DIR" "$WEB_DIR"

if [ -f "$ENV_DIR/web.env" ]; then
  # Preserve already generated deployment secrets across updates.
  set +u
  # shellcheck disable=SC1091
  . "$ENV_DIR/web.env" || true
  set -u
fi

: "${HW_WEB_SECRET_KEY:=$(openssl rand -hex 32)}"
: "${HW_WEB_ADMIN_TOKEN:=$(openssl rand -hex 24)}"
: "${HW_WEB_JOB_TIMEOUT_SECONDS:=900}"
: "${HW_XIAOYA_SCAN_TIMEOUT_SECONDS:=600}"
NOVNC_PASSWORD="$REQUESTED_NOVNC_PASSWORD"
if [ -n "$NOVNC_PASSWORD" ] && [ "${#NOVNC_PASSWORD}" -gt 8 ]; then
  echo "NOVNC_PASSWORD must be 8 characters or fewer because VNC passwords are limited to 8 characters." >&2
  exit 2
fi

if [ -d "$APP_DIR/.git" ]; then
  log "updating repository"
  git -C "$APP_DIR" fetch --all --prune
  git -C "$APP_DIR" reset --hard origin/main
else
  log "cloning repository"
  rm -rf "$APP_DIR"
  git clone --depth 1 "$REPO_URL" "$APP_DIR"
fi

chmod +x "$APP_DIR/deploy/start-web.sh"

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install --upgrade pip setuptools wheel
"$APP_DIR/.venv/bin/python" -m pip install -e "$APP_DIR"

mkdir -p "$APP_DIR/ms-playwright"
if ! PLAYWRIGHT_BROWSERS_PATH="$APP_DIR/ms-playwright" "$APP_DIR/.venv/bin/python" -m playwright install --with-deps chromium; then
  log "playwright mirror failed; retrying with default download host"
  unset PLAYWRIGHT_DOWNLOAD_HOST
  PLAYWRIGHT_BROWSERS_PATH="$APP_DIR/ms-playwright" "$APP_DIR/.venv/bin/python" -m playwright install --with-deps chromium
fi

cat >"$ENV_DIR/web.env" <<ENV_FILE
DISPLAY=:99
XVFB_SCREEN=1440x1000x24
PLAYWRIGHT_BROWSERS_PATH=$APP_DIR/ms-playwright
HW_WEB_HOST=127.0.0.1
HW_WEB_PORT=8080
HW_WEB_DIR=$WEB_DIR
HW_WEB_SECRET_KEY=$HW_WEB_SECRET_KEY
HW_WEB_ADMIN_TOKEN=$HW_WEB_ADMIN_TOKEN
HW_WEB_JOB_TIMEOUT_SECONDS=$HW_WEB_JOB_TIMEOUT_SECONDS
HW_WEB_SECURE_COOKIES=0
HW_WEB_NOVNC_URL=http://$PUBLIC_IP/vnc/vnc.html?autoconnect=1&resize=scale&path=vnc/websockify
HW_XIAOYA_SCAN_TIMEOUT_SECONDS=$HW_XIAOYA_SCAN_TIMEOUT_SECONDS
NOVNC_PASSWORD=$NOVNC_PASSWORD
SMTP_HOST=${SMTP_HOST:-}
SMTP_PORT=${SMTP_PORT:-587}
SMTP_USERNAME=${SMTP_USERNAME:-}
SMTP_PASSWORD=${SMTP_PASSWORD:-}
SMTP_SSL=${SMTP_SSL:-0}
SMTP_STARTTLS=${SMTP_STARTTLS:-1}
EMAIL_FROM=${EMAIL_FROM:-}
ENV_FILE
chmod 600 "$ENV_DIR/web.env"

cat >/usr/local/bin/homework-watcher-run-web <<'RUNNER'
#!/usr/bin/env bash
set -euo pipefail
set -a
. /etc/homework-watcher/web.env
set +a
export PATH="/opt/homework-watcher/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

pkill -f "Xvfb :99" >/dev/null 2>&1 || true
pkill -f "x11vnc.*:99" >/dev/null 2>&1 || true
pkill -f "websockify.*6080" >/dev/null 2>&1 || true
pkill -x fluxbox >/dev/null 2>&1 || true

cd /opt/homework-watcher
exec /opt/homework-watcher/deploy/start-web.sh
RUNNER
chmod +x /usr/local/bin/homework-watcher-run-web

cat >/etc/systemd/system/homework-watcher-web.service <<'SERVICE'
[Unit]
Description=homework-watcher web app
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/homework-watcher
EnvironmentFile=/etc/homework-watcher/web.env
ExecStart=/usr/local/bin/homework-watcher-run-web
Restart=always
RestartSec=5
KillMode=control-group
TimeoutStartSec=60

[Install]
WantedBy=multi-user.target
SERVICE

cat >/etc/nginx/sites-available/homework-watcher <<'NGINX'
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
ln -sf /etc/nginx/sites-available/homework-watcher /etc/nginx/sites-enabled/homework-watcher

nginx -t
systemctl daemon-reload
systemctl enable nginx
systemctl restart nginx
systemctl enable homework-watcher-web
systemctl restart homework-watcher-web

sleep 10

if [ -n "$NOVNC_PASSWORD" ]; then
  NOVNC_PASSWORD_STATUS="enabled"
else
  NOVNC_PASSWORD_STATUS="disabled"
fi

cat >/root/homework-watcher-credentials.txt <<CREDS
homework-watcher deployment
URL: http://$PUBLIC_IP/
Admin URL: http://$PUBLIC_IP/admin?token=$HW_WEB_ADMIN_TOKEN
Admin token: $HW_WEB_ADMIN_TOKEN
noVNC URL: http://$PUBLIC_IP/vnc/vnc.html?autoconnect=1&resize=scale&path=vnc/websockify
noVNC password: $NOVNC_PASSWORD_STATUS
Environment file: $ENV_DIR/web.env
Service: homework-watcher-web
Log: journalctl -u homework-watcher-web -n 200 --no-pager
CREDS
chmod 600 /root/homework-watcher-credentials.txt

log "local service status"
systemctl --no-pager --full status homework-watcher-web || true
ss -lntp | sed -n '1,160p' || true
curl -fsS -I http://127.0.0.1:8080/ || true
curl -fsS -I http://127.0.0.1/ || true

log "deployment complete"
cat /root/homework-watcher-credentials.txt
