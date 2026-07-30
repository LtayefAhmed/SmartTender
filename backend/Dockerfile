# syntax=docker/dockerfile:1.7
# =============================================================================
# Multi-stage build.
#
# One image serves the API, the workers and Beat — they differ only by command.
# That guarantees all three run identical code, which matters because a
# connector fix must reach the worker and the API's registry endpoint at the
# same moment.
#
# The Playwright browser lives in a separate final stage so a deployment that
# runs no dynamic connector is not forced to ship ~400 MB of Chromium.
# =============================================================================

ARG PYTHON_VERSION=3.11-slim-bookworm

# -----------------------------------------------------------------------------
FROM python:${PYTHON_VERSION} AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        libxml2-dev \
        libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md ./
COPY app ./app

# Build a self-contained virtualenv that the runtime stage copies wholesale.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --upgrade pip setuptools wheel && pip install .

# -----------------------------------------------------------------------------
FROM python:${PYTHON_VERSION} AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    SMARTTENDER_CONFIG_DIR=/app/config

# Runtime libraries only — no compilers in the shipped image.
#
# Tesseract with the French and English language packs powers the OCR fallback
# for scanned tender documents; libgl1/libglib are OpenCV's runtime
# dependencies for the pre-OCR image cleanup. Together they are the difference
# between reading a scanned cahier des charges and scoring it on its title.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        libxml2 \
        libxslt1.1 \
        tesseract-ocr \
        tesseract-ocr-fra \
        tesseract-ocr-eng \
        libgl1 \
        libglib2.0-0 \
        curl \
        tini \
    && rm -rf /var/lib/apt/lists/*

# Never run as root.
RUN groupadd --system --gid 1001 smarttender \
    && useradd --system --uid 1001 --gid smarttender --create-home smarttender

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=smarttender:smarttender app ./app
COPY --chown=smarttender:smarttender config ./config
COPY --chown=smarttender:smarttender alembic ./alembic
COPY --chown=smarttender:smarttender alembic.ini pyproject.toml ./
COPY --chown=smarttender:smarttender scripts ./scripts

USER smarttender

EXPOSE 8000

# tini reaps the zombie processes Celery's prefork pool leaves behind.
ENTRYPOINT ["/usr/bin/tini", "--"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health/live || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# -----------------------------------------------------------------------------
# Optional stage for deployments that run a `strategy: dynamic` connector.
# Build with:  docker build --target runtime-browser -t smarttender:browser .
FROM runtime AS runtime-browser

USER root
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
RUN pip install playwright \
    && playwright install --with-deps chromium \
    && chmod -R a+rX /opt/playwright
USER smarttender
