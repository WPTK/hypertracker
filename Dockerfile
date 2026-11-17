FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# Only what the app needs at runtime. scripts/ is kept so the airports loader
# can be run inside the container (python -m scripts.update_airports).
COPY app ./app
COPY scripts ./scripts
COPY run.py ./

# Non-root user. /app/data is where the app keeps its SQLite database; the
# compose file mounts the host data folder over it.
RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid 1000 --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/data \
    && chown 1000:1000 /app/data

USER 1000:1000
EXPOSE 8008

# Exactly one worker: live state, rate limits and the poller live in process memory.
# No --proxy-headers: the app resolves the client address itself via TRUSTED_PROXY.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8008"]
