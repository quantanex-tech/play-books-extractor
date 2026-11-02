FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PLAYBOOKS_DATA_DIR=/data \
    PLAYBOOKS_CONFIG_DIR=/config \
    PLAYBOOKS_HOST=0.0.0.0 \
    PLAYBOOKS_PORT=8080 \
    HOME=/home/appuser

WORKDIR /app

RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash appuser \
    && mkdir -p /data /config /ms-playwright \
    && chown -R appuser:appuser /data /config /home/appuser

RUN pip install --upgrade pip \
    && pip install \
        "beautifulsoup4>=4.12.3" \
        "fastapi>=0.115.0" \
        "httpx>=0.27.2" \
        "playwright>=1.49.0" \
        "python-multipart>=0.0.12" \
        "uvicorn[standard]>=0.30.6" \
    && playwright install --with-deps chromium-headless-shell \
    && chmod -R a+rX /ms-playwright \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE /app/
COPY playbooks_extractor /app/playbooks_extractor
RUN pip install --no-deps .

USER appuser

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3)"

CMD ["playbooks-extractor"]
