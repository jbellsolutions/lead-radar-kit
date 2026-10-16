# Lead Radar hosted dashboard (Railway or any Docker host). The database lives on a volume mounted at /data.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 RADAR_STATE_DIR=/data
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
COPY contracts ./contracts
RUN pip install --no-cache-dir -e ".[server,export]"
# One worker: SQLite plus a single job runner. Daily campaigns start from inside it (RADAR_DAILY_AT).
CMD ["sh", "-c", "exec uvicorn lead_radar.server:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
