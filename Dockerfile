# Meeting Copilot — server image (HTTP + WebSocket + web UI). HTTPS is terminated by Caddy (docker-compose.yml).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    COPILOT_HOME=/app

WORKDIR /app

# dependencies first (cached layer)
COPY pyproject.toml README.md ./
COPY copilot ./copilot
RUN pip install . && pip uninstall -y meeting-copilot  # deps only; the app runs from /app source

COPY config.yaml ./
RUN mkdir -p /app/data /app/context && useradd -m -u 10001 copilot && chown -R copilot /app
USER copilot

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["python", "-m", "copilot", "serve", "--host", "0.0.0.0", "--port", "8000"]
