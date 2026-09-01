# Multi-stage image for the cc-remote Relay.
#
# The official deploy path is a systemd venv staged by setup-vps.sh in front of
# a managed Caddy (see deploy/README.md and install-relay.sh). This container
# runs the exact same `python -m cc_remote.relay` release in an isolated root
# filesystem — for VPSes that already manage services with Docker, or where the
# relay should be pinned to its own writable state volume.
#
# The repository does not track web/dist, so the first stage builds the web
# client from source (npm run build) and the second stage only carries the
# built artifact. Python wheels are installed with the same hash-locked,
# binary-only rules the official installer uses; http-ece ships only an sdist
# on PyPI, so it is exempted with --no-binary exactly as setup-vps.sh does.
#
# Mirrors: PyPI.org by default. In mainland China build with
#   docker build -f deploy/Dockerfile \
#     --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple \
#     --build-arg PIP_EXTRA_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
#     -t cc-remote-relay .

FROM node:24-alpine AS web-build
WORKDIR /src/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.13-slim-bookworm AS relay

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    RELAY_HOST=127.0.0.1 \
    RELAY_PORT=8765 \
    WEB_STATIC_DIR=/app/web/dist \
    PUSH_DB_PATH=/app/state/relay-push.sqlite3 \
    DEVICE_DB_PATH=/app/state/relay-devices.sqlite3 \
    SESSION_TTL_SECONDS=604800 \
    LOG_LEVEL=INFO

WORKDIR /app

# Relay application code, deploy reference files, and the hash-locked
# dependency list (the installed wheels are the ones setup-vps.sh pins).
COPY cc_remote/ ./cc_remote/
COPY deploy/ ./deploy/
COPY requirements-relay.lock requirements-relay.lock

# The systemd unit runs the relay as a dedicated `ccremote` user
# (deploy/cc-remote-relay.service); mirror that inside the container. State is
# written to /app/state — mount it as a volume (see docker-compose.yml).
RUN groupadd --gid 10001 ccremote \
    && useradd --uid 10001 --gid ccremote --home-dir /app \
        --shell /usr/sbin/nologin ccremote \
    && mkdir -p /app/state \
    && chown -R ccremote:ccremote /app/state

ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_EXTRA_INDEX_URL=
RUN pip install --index-url "$PIP_INDEX_URL" \
    ${PIP_EXTRA_INDEX_URL:+--extra-index-url "$PIP_EXTRA_INDEX_URL"} \
    --require-hashes --only-binary=:all: --no-binary=http-ece \
    -r requirements-relay.lock

# Built web client from the first stage.
COPY --from=web-build --chown=ccremote:ccremote /src/web/dist ./web/dist/

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=4)" || exit 1

USER ccremote

CMD ["python", "-m", "cc_remote.relay"]
