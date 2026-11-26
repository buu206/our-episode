FROM node:22-bookworm-slim AS web
WORKDIR /build
ENV NEXT_TELEMETRY_DISABLED=1
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk ca-certificates libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home app
COPY --from=web /usr/local/bin/node /usr/local/bin/node
WORKDIR /srv/app
COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY backend/app ./backend/app
COPY backend/alembic ./backend/alembic
COPY backend/alembic.ini ./backend/alembic.ini
COPY backend/static ./backend/static
COPY --from=web /build/.next/standalone ./web
COPY --from=web /build/.next/static ./web/.next/static
COPY --from=web /build/public ./web/public
COPY deploy/serve.py ./deploy/serve.py
RUN mkdir -p /srv/app/data && chown -R app:app /srv/app
ENV APP_DATA_DIR=/srv/app/data BACKEND_URL=http://127.0.0.1:8788 BACKEND_MODE=normal LOCAL_DEMO_ENTRY=0 NEXT_TELEMETRY_DISABLED=1 PYTHONUNBUFFERED=1
USER app
EXPOSE 3000
HEALTHCHECK --interval=30s --start-period=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8788/api/v1/status', timeout=4)"
CMD ["python", "deploy/serve.py"]
