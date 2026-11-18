#!/bin/sh
# Fix ownership of mounted volumes (Railway / docker bind mounts are root-owned),
# then run the bot as the unprivileged `botuser`.
set -e

mkdir -p /app/data /app/logs

if [ "$(id -u)" = "0" ]; then
    chown -R botuser:botuser /app/data /app/logs /app/config 2>/dev/null || true
    exec runuser -u botuser -- "$@"
fi

exec "$@"
