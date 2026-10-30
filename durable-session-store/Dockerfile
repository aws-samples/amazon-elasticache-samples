# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Container image for the durable session-store storefront.
# Build from the repo root:  docker build -t durable-session-store .
FROM public.ecr.aws/docker/library/python:3.12-slim AS base

# Don't buffer stdout/stderr; no .pyc files.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install runtime deps first for better layer caching. boto3 (demo) is included
# so the optional failover button works when ENABLE_FAILOVER_DEMO is set.
COPY requirements.txt requirements-demo.txt ./
RUN pip install --no-cache-dir -r requirements-demo.txt

# App source.
COPY app ./app

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8080

# Gunicorn serves the WSGI app. The container reads all configuration from the
# environment (REDIS_HOST, REDIS_AUTH_TOKEN, SECRET_KEY, etc.).
# 2 workers x 4 threads is plenty for a demo; tune for real workloads.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--threads", "4", \
     "--timeout", "30", "--access-logfile", "-", "app.main:app"]
