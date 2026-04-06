# BDS Agent — Dockerfile
# Multi-stage build for Linux deployment
FROM python:3.14-slim-bookworm AS base

SHELL ["/bin/bash", "-e", "-c"]

# ── Stage 1: Install system dependencies ──────────────────────────────────────
FROM base AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        git \
        gnupg \
        ca-certificates \
        apt-transport-https \
        wget \
        gnupg \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js for Playwright (required for browser binaries)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x \
    | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# ── Stage 2: Python deps ────────────────────────────────────────────────────
FROM base AS python-deps

RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq-dev \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Stage 3: Playwright browsers ─────────────────────────────────────────────
FROM base AS playwright-browsers

RUN apt-get update && apt-get install -y --no-install-recommends \
        wget \
        gnupg \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
        playwright>=1.52.0

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN python -m playwright install --with-deps chromium \
    && python -m playwright install-deps chromium

# ── Final image ────────────────────────────────────────────────────────────────
FROM base AS runtime

LABEL org.opencontainers.image.title="BDS Agent"
LABEL org.opencontainers.image.description="Facebook crawler + LLM enrichment + Telegram notifications"

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        libglib2.0-0 \
        libnss3 \
        libnspr4 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libcups2 \
        libdrm2 \
        libxkbcommon0 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxrandr2 \
        libgbm1 \
        libpango-1.0-0 \
        libcairo2 \
        libasound2 \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder stages
COPY --from=python-deps /usr/local /usr/local
COPY --from=playwright-browsers /ms-playwright /ms-playwright
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Copy application
COPY . .

# Create non-root user for security
RUN useradd --create-home --shell /bin/bash appuser \
    && chown -R appuser:appuser /home/appuser
USER appuser

# Environment defaults (override via -e or .env)
ENV PYTHONUNBUFFERED=1
ENV PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.0

EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/stats')" || exit 1

# Run: use PYTHONPATH so services/ can be imported
CMD ["python", "-m", "uvicorn", "api_app:app", "--host", "0.0.0.0", "--port", "8000"]
