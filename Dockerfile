FROM python:3.11-slim

# System deps for lxml, playwright browser, and cron
RUN apt-get update && apt-get install -y --no-install-recommends \
    cron \
    curl \
    wget \
    gnupg \
    ca-certificates \
    libxml2-dev \
    libxslt1-dev \
    # Playwright Chromium system deps
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
    libasound2 \
    libpangocairo-1.0-0 \
    libgtk-3-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (layer cache friendly)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium browser
RUN python -m playwright install chromium --with-deps 2>/dev/null || \
    echo "Playwright browser install skipped (non-critical)"

# Copy application source
COPY src/       ./src/
COPY templates/ ./templates/

# Data directory (mounted as a volume at runtime)
RUN mkdir -p /app/data

# ── Entrypoint ─────────────────────────────────────────────────────────────────
# The scheduler in main.py fires immediately on startup, then every 6 hours.
# No cron needed — the Python scheduler handles the loop.
# Logs go to stdout for docker compose logs / cloud log aggregators.
CMD ["python", "-u", "src/main.py"]
