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

RUN mkdir -p /app/data /app/logs \
    && useradd --create-home --uid 1000 botuser \
    && chown -R botuser:botuser /app
USER botuser

VOLUME ["/app/data", "/app/logs"]

CMD ["python", "-m", "app.main"]
