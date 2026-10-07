# syntax=docker/dockerfile:1.7
# Multi-stage build for the URL shortener service (Layer B). Runs as a non-root user.

FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements.service.txt .
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install -r requirements.service.txt

FROM python:3.12-slim AS runtime
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SHORTENER_ENVIRONMENT=prod \
    SHORTENER_DB_AUTO_MIGRATE=false
RUN groupadd --system app && useradd --system --gid app --home /app --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app service ./service
USER app
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"
# Migrate explicitly (one-off, idempotent), then serve. Behind a proxy, uvicorn trusts X-Forwarded-*
# only from FORWARDED_ALLOW_IPS (default 127.0.0.1).
CMD ["sh", "-c", "python -m service.cli migrate && exec uvicorn service.app:create_app --factory --host 0.0.0.0 --port 8000 --proxy-headers --no-access-log"]
