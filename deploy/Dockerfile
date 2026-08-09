# The episode runner: everything a Workers isolate cannot do.
#
# FFmpeg for encoding, Chromium for discovery, Cairo for SVG rasterization,
# and the mpvf package itself. Inference and storage are remote, so this image
# holds no model weights and keeps no state between runs.

FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
    MPVF_MIGRATIONS_DIR=/app/migrations

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        librsvg2-bin \
        fonts-dejavu-core \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README-MPVF.md ./
COPY src/ ./src/
COPY migrations/ ./migrations/
COPY alembic.ini ./
COPY config/ ./config/

RUN pip install --no-cache-dir ".[acquisition,publish,cloud]" \
    && python -m playwright install --with-deps chromium

# The container answers a single HTTP request per episode; the Worker holds the
# connection open for the duration of the run.
COPY deploy/entrypoint.py ./entrypoint.py
EXPOSE 8080
CMD ["python", "entrypoint.py"]
