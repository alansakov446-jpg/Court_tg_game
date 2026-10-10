FROM python:3.12-slim

WORKDIR /app

# Requirements first for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic alembic
COPY alembic.ini .
COPY ai ai
COPY db db
COPY game game
COPY scripts scripts
COPY main.py .

ENV PYTHONUNBUFFERED=1 \
    RUN_SECONDS=0 \
    HEALTHZ_PORT=8080

# Liveness: health.json must be fresh (rewritten after every polling cycle).
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
  CMD ["python", "scripts/healthz.py", "--check"]

# Long-running service: migrate, then poll forever (RUN_SECONDS=0).
CMD ["sh", "-c", "alembic upgrade head && exec python main.py"]
