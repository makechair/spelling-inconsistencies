# Single image, two entrypoints (collector / api). Building once and running
# it twice keeps the two processes on identical code without a second build.
#
# There is deliberately no Node.js stage: the frontend ships as plain ES
# modules with the chart library vendored, because a bundler build on a 1 GB
# instance is a realistic OOM (docs/spec-review.md B-7).
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY web ./web

# Non-root: the process needs nothing but its data directory.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data \
    && chown -R app:app /data /app
USER app

ENV USSTOCKS_DB_PATH=/data/market.db \
    USSTOCKS_LIVE_DB_PATH=/dev/shm/live.db \
    USSTOCKS_WEB_DIR=/app/web

EXPOSE 8000

# Overridden by docker-compose for the collector service.
CMD ["python", "-m", "usstocks.api"]
