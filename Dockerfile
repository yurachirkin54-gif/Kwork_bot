FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Moscow

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY config ./config
COPY pyproject.toml README.md ./
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# NB: no VOLUME instruction (Railway forbids it). Mount persistent storage at /app/data.
# The container starts as root only to fix ownership of the mounted volume, then the
# entrypoint drops privileges to `botuser` (see docker-entrypoint.sh).
RUN sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh \
    && chmod +x /usr/local/bin/docker-entrypoint.sh \
    && mkdir -p /app/data /app/logs \
    && useradd --create-home --uid 1000 botuser \
    && chown -R botuser:botuser /app

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "app.main"]
