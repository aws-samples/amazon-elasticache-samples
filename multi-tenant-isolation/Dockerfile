# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Container image for the multi-tenant Valkey storefront demo.
# Built at deploy time by the app stack (ECR image asset).
FROM public.ecr.aws/docker/library/python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install runtime deps first for better layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# App source.
COPY app ./app

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8080

# Gunicorn serves the WSGI app. All configuration comes from the environment
# (REDIS_HOST, SECRET_KEY, TENANT*_PASSWORD, ...).
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--threads", "4", \
     "--timeout", "30", "--access-logfile", "-", "app.main:app"]
