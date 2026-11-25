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
# Base images are pinned by digest (Jira story F181), so a rebuild cannot pick up different
# bytes under the same tag. To move to a newer image, look the digest up with
#   docker buildx imagetools inspect <image>:<tag>
# and replace it; the tag stays for readers.
FROM public.ecr.aws/docker/library/python:3.12-alpine3.24@sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89
# The OS fixes published since the base image was built.
RUN apk upgrade --no-cache

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
# requirements.lock pins every package, including transitive ones, with hashes
# (generated from requirements.txt; see its header), so a rebuild installs the
# same files.
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

# transforms.py is imported by bulk_loader.py; it is the same transform engine the
# Go sink implements, so a loaded row and a CDC-written row have the same shape.
# sqlident.py validates and quotes table/column names; a vendored copy of
# ecdl_link/sqlident.py, kept identical by a test.
COPY bulk_loader.py transforms.py sqlident.py cdc_values.py verify.py ./
# The CAs the loader verifies the source's TLS certificate with
# (SOURCE_TLS_CA_FILE): Amazon RDS's global bundle, then the system CAs (RDS
# Proxy, sources with a public certificate). The build fails if either is
# missing rather than producing an image that cannot verify.
# Amazon RDS's public CA bundle (certificates only), downloaded at build time
# and checked against this SHA-256, so the repository carries no certificate
# file and a changed download fails the build. The same pin is in the Makefile.
ARG RDS_CA_URL=https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem
ARG RDS_CA_SHA256=90a6f1a7978a1b1b096dca6c57511ca48d3b8cf052f8e704baf358f34ea2f21b
RUN python -c "import hashlib, sys, urllib.request; d = urllib.request.urlopen(sys.argv[1], timeout=60).read(); h = hashlib.sha256(d).hexdigest(); h == sys.argv[2] or sys.exit('RDS CA bundle sha256 %s, expected %s: update RDS_CA_SHA256 after checking the new bundle' % (h, sys.argv[2])); open(sys.argv[3], 'wb').write(d)" "$RDS_CA_URL" "$RDS_CA_SHA256" /app/rds-ca-bundle.pem \
 && cat /app/rds-ca-bundle.pem /etc/ssl/certs/ca-certificates.crt > /app/source-ca.pem \
 && chmod 0444 /app/source-ca.pem
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod 0555 /app/entrypoint.sh

# Nothing here needs root.
RUN adduser -D -H -u 10001 ecdl
USER 10001

# Configuration is entirely environment: SOURCE_*, VALKEY_*, TABLE(S),
# PRIMARY_KEY(S), LOADER_WORKERS, TRANSFORMS_JSON, MIN_PK/MAX_PK. See
# entrypoint.sh, which maps those onto bulk_loader.py's arguments.
# A run-once task with no port to probe: ECS reads its exit code instead.
HEALTHCHECK NONE
ENTRYPOINT ["/app/entrypoint.sh"]
