# syntax=docker/dockerfile:1
#
# One image for both compose services: `bot` (python -m bot run) and `dashboard`
# (python -m bot dashboard). No secrets are baked in: they arrive at runtime from .env through
# docker-compose.yml. var/ and data/ are volumes, never image layers. The dashboard healthcheck
# lives in docker-compose.yml because only that service serves HTTP.

FROM python:3.11-slim

# The container user must own the bind-mounted ./var and ./data on the host, so its uid/gid
# match the host user who runs compose (see APP_UID/APP_GID in .env.example).
ARG APP_UID=1000
ARG APP_GID=1000

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=UTC \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache

# tzdata: zoneinfo needs America/New_York for bar timing and the daily loss limit, and the
# slim base does not guarantee the system zone database.
RUN test "${APP_UID}" -ne 0 && test "${APP_GID}" -ne 0 \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --non-unique --gid "${APP_GID}" app \
 && useradd --non-unique --uid "${APP_UID}" --gid "${APP_GID}" \
      --no-create-home --home-dir /tmp --shell /usr/sbin/nologin app

WORKDIR /app

# Runtime dependencies only; requirements-dev.txt (pytest) stays out of the image.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ ./bot/
COPY strategy.md ./
COPY results/ ./results/

# Precompile so a read-only root filesystem costs nothing at import time.
RUN python -m compileall -q bot \
 && mkdir -p var data/cache \
 && chown app:app var data data/cache

USER app

ENTRYPOINT ["python", "-m", "bot"]
CMD ["run"]
