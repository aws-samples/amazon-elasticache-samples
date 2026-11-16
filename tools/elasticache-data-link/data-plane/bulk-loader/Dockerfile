# ═══════════════════════════════════════════════════════════════════════════
# ECDL bulk loader — parallel initial load, source table -> target cache.
#
# There was no image for this. The loader could only run as a local python3
# process, so an initial load depended on whoever ran it having source and cache
# reachability, the right database drivers installed, and the database password on
# their machine. Packaging it is what lets `mode: initial_load` be something the
# control plane performs rather than a manual step it asks the operator to run.
#
# Multi-arch, same as the sink, so it can run on Graviton.
# ═══════════════════════════════════════════════════════════════════════════
# ECR Public mirror of the Docker official image (not Docker Hub).
FROM public.ecr.aws/docker/library/python:3.12-slim

# Build identity, for the same reason the sink carries it: so a running task can
# be traced back to a commit.
ARG VERSION=dev
ARG COMMIT=unknown
ARG BUILD_DATE=unknown
LABEL org.opencontainers.image.title="ecdl-loader" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${COMMIT}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.description="ECDL initial load: source table to target Valkey"
ENV ECDL_VERSION=${VERSION} ECDL_COMMIT=${COMMIT}

# psycopg2-binary and pymssql ship wheels for both architectures, so no compiler
# is needed. pymssql bundles FreeTDS, which is why no ODBC packages are installed.
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# transforms.py is imported by bulk_loader.py; it is the same transform engine the
# Go sink implements, so a loaded row and a CDC-written row have the same shape.
# sqlident.py validates and quotes table/column names; a vendored copy of
# ecdl_link/sqlident.py, kept identical by a test.
COPY bulk_loader.py transforms.py sqlident.py cdc_values.py ./
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod 0555 /app/entrypoint.sh

# Nothing here needs root.
RUN useradd -r -u 10001 ecdl
USER 10001

# Configuration is entirely environment: SOURCE_*, VALKEY_*, TABLE(S),
# PRIMARY_KEY(S), LOADER_WORKERS, TRANSFORMS_JSON, MIN_PK/MAX_PK. See
# entrypoint.sh, which maps those onto bulk_loader.py's arguments.
ENTRYPOINT ["/app/entrypoint.sh"]
