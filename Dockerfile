# syntax=docker/dockerfile:1.7
# -----------------------------------------------------------------------------
# Multi-stage build for rental_finder.
#
# Build:    docker build -t rental-finder:latest .
# Run:      docker run --rm -p 8000:8000 \
#               -e GOOGLE_CSE_KEY=... -e GOOGLE_CSE_CX=... \
#               -v rf-cache:/home/app/.rental_finder \
#               rental-finder:latest
#
# Tunables (env vars at runtime):
#   PORT     - listen port (default 8000)
#   WORKERS  - number of uvicorn worker processes (default 1; raise for more
#              concurrency, but keep <= number of CPUs and remember the
#              SQLite cache is per-worker - see DEPLOY.md).
#
# Heavy Playwright deps are NOT installed by default. To enable the optional
# Facebook source, build with: --build-arg INCLUDE_PLAYWRIGHT=1
# -----------------------------------------------------------------------------

# ---------- Stage 1: build wheels ----------
FROM python:3.13-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Compile-time deps for lxml / brotli wheels.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libxml2-dev libxslt1-dev libz-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

ARG INCLUDE_PLAYWRIGHT=0
RUN if [ "$INCLUDE_PLAYWRIGHT" = "0" ]; then \
        grep -v '^playwright' requirements.txt > /tmp/req.txt; \
    else \
        cp requirements.txt /tmp/req.txt; \
    fi \
    && pip wheel --wheel-dir=/wheels -r /tmp/req.txt

# ---------- Stage 2: runtime ----------
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    WORKERS=1

# Run-time native libs needed by lxml + brotli + curl for healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libxml2 libxslt1.1 libstdc++6 \
        ca-certificates tini curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root user.
RUN useradd --create-home --shell /bin/bash --uid 1000 app
WORKDIR /home/app/rental_finder

COPY --from=builder /wheels /wheels
ARG INCLUDE_PLAYWRIGHT=0
COPY requirements.txt .
RUN if [ "$INCLUDE_PLAYWRIGHT" = "0" ]; then \
        grep -v '^playwright' requirements.txt > /tmp/req.txt; \
    else \
        cp requirements.txt /tmp/req.txt; \
    fi \
    && pip install --no-index --find-links=/wheels -r /tmp/req.txt \
    && rm -rf /wheels /tmp/req.txt

# Optionally install Chromium for the Playwright Facebook source.
RUN if [ "$INCLUDE_PLAYWRIGHT" = "1" ]; then \
        python -m playwright install --with-deps chromium; \
    fi

# Copy the package itself (the docker context is the project root).
COPY --chown=app:app . /home/app/rental_finder/

# Persist disk cache (and any future Playwright state) on a named volume.
RUN mkdir -p /home/app/.rental_finder && chown -R app:app /home/app

USER app
WORKDIR /home/app

EXPOSE 8000

# Built-in HEALTHCHECK so `docker ps` and orchestrators see container health.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS "http://localhost:${PORT}/healthz" || exit 1

# tini so SIGTERM is forwarded cleanly when the container is stopped.
ENTRYPOINT ["/usr/bin/tini", "--"]

# We bind to 0.0.0.0 so it's reachable from the host.
# `rental_finder` package directory must be importable; PWD here is /home/app.
# --proxy-headers + --forwarded-allow-ips trusts X-Forwarded-* from a reverse proxy.
ENV PYTHONPATH=/home/app
CMD ["sh", "-c", "exec uvicorn rental_finder.web.app:app \
    --host 0.0.0.0 --port ${PORT} --workers ${WORKERS} \
    --proxy-headers --forwarded-allow-ips '*' \
    --timeout-keep-alive 30 --access-log"]
