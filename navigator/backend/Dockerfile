# Backend Dockerfile for Valkey Insights Server
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (leverage Docker layer caching)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose default server port
EXPOSE 8000

# Environment variables can override config (see config_manager.py)
# Example:
#   -e SERVER_PORT=8000 -e VALKEY_HOST=your-valkey-host -e VALKEY_PORT=6379 -e VALKEY_USE_TLS=true

# Default command uses uvicorn to serve FastAPI app
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
