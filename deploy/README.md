# Public Web Deployment

This deployment runs the hosted homework-watcher web app, a Playwright display, noVNC, and Caddy HTTPS reverse proxy.

## Requirements

- A Linux VPS with Docker and Docker Compose.
- A domain name pointing to the VPS with an `A` record.
- TCP ports `80` and `443` open.
- SMTP credentials for the report sender mailbox.

## Deploy

From the repository root on the VPS:

```bash
cp deploy/.env.example deploy/.env
nano deploy/.env
docker compose --env-file deploy/.env -f deploy/docker-compose.yml up -d --build
```

Set these values in `deploy/.env` before starting:

- `APP_DOMAIN`: the public domain, for example `homework.yourdomain.com`.
- `CADDY_EMAIL`: email used by Caddy for Let's Encrypt notices.
- `SMTP_*` and `EMAIL_FROM`: sender mailbox settings.
- `HW_WEB_SECRET_KEY`: long random value used to sign web sessions.
- `HW_WEB_ADMIN_TOKEN`: long random value for `/admin/run-daily`.
- `HW_WEB_NOVNC_URL`: `https://APP_DOMAIN/vnc/vnc.html?autoconnect=1&resize=scale&path=vnc/websockify`.
- `NOVNC_PASSWORD`: optional noVNC password, at most 8 characters. Leave unset for the hosted student site so users do not see an extra noVNC password prompt.

Generate secrets on the VPS:

```bash
python3 - <<'PY'
import secrets
print("HW_WEB_SECRET_KEY=" + secrets.token_urlsafe(48))
print("HW_WEB_ADMIN_TOKEN=" + secrets.token_urlsafe(32))
PY
```

## Use

Open:

```text
https://APP_DOMAIN/
```

Students register their own account, then use the platform login buttons to manually log into 小雅 and 长江雨课堂 in the remote browser. The service stores browser session state per user under the persistent Docker volume.

## Daily Trigger

Use cron-job.org to call:

```text
POST https://APP_DOMAIN/admin/run-daily?token=HW_WEB_ADMIN_TOKEN
```

This starts a background run for every registered user: scan platforms, sync this week's fixed assignments, and send the email report.

## Maintenance

```bash
docker compose --env-file deploy/.env -f deploy/docker-compose.yml ps
docker compose --env-file deploy/.env -f deploy/docker-compose.yml logs -f app
docker compose --env-file deploy/.env -f deploy/docker-compose.yml pull
docker compose --env-file deploy/.env -f deploy/docker-compose.yml up -d --build
```

The important persistent data is in the `homework_data` Docker volume. Back it up before rebuilding or moving servers.

## Security Notes

The public app still does not store platform passwords, bypass captchas, or submit homework. Users manually log into platforms through noVNC. Do not expose this without HTTPS. For a larger rollout, put the site behind a school-only domain, invite-only registration, or an external access-control layer.
