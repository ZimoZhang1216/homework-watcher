FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DISPLAY=:99 \
    HW_WEB_HOST=0.0.0.0 \
    HW_WEB_PORT=8080 \
    HW_WEB_DIR=/data/web \
    HW_WEB_SECURE_COOKIES=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        fluxbox \
        novnc \
        websockify \
        x11vnc \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY homework_watcher ./homework_watcher

RUN python -m pip install --no-cache-dir -e . \
    && python -m playwright install --with-deps chromium

COPY deploy/start-web.sh /usr/local/bin/homework-watcher-start-web
RUN chmod +x /usr/local/bin/homework-watcher-start-web

VOLUME ["/data"]
EXPOSE 8080 6080

CMD ["homework-watcher-start-web"]
