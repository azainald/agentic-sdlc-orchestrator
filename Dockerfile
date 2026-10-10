FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first for better layer caching (requirements.txt already includes pytest).
COPY requirements.txt .
RUN pip install -r requirements.txt

# Non-root user; /app/data is where the SQLite db, audit log and link store live.
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/data \
    && chown -R app:app /app

COPY --chown=app:app . .

USER app

VOLUME ["/app/data"]
EXPOSE 8080 8090

# Default: the orchestrator API (override with `python -m shortener` for the shortener).
CMD ["python", "-m", "orchestrator"]
