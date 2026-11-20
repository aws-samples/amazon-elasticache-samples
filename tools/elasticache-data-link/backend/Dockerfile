# Control-plane API, DEVELOPMENT image: built by docker-compose.yml with
# backend/ as the context. The published image is Dockerfile.release.
#
# ECR Public mirror of the Docker official image (not Docker Hub), pinned by
# digest (Jira story F181) so a rebuild cannot pick up different bytes under the same tag.
# To move to a newer image: docker buildx imagetools inspect <image>:<tag>.
FROM public.ecr.aws/docker/library/python:3.11-alpine3.24@sha256:7fbad8e4652eae0186429a6f26cf5b23e27e8c6fd7ac4d0cfd7a6988cdeea1e1
WORKDIR /app
# requirements.lock pins every package, including transitive ones, with hashes.
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
# .dockerignore keeps data/ (app.db, deployments.json) and caches out.
COPY . .
ENV PYTHONUNBUFFERED=1
# The app binds loopback by default. Inside the container it has to accept the
# published port; who can reach that port is decided by the publish (127.0.0.1
# in docker-compose.yml, loopback on the instance in AccessMode=ssm) and the
# instance security group.
ENV ECDL_BIND_HOST=0.0.0.0
# Nothing here needs root (Jira story F184). The only directory it writes is /app/data.
RUN adduser -D -H -u 10001 ecdl && mkdir -p /app/data && chown 10001:10001 /app/data
USER 10001
# Liveness: the API accepts connections on its port. A TCP connect, not an
# HTTP request, so the check does not depend on the Host allowlist.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import socket; socket.create_connection(('127.0.0.1', 5000), 3).close()"]
CMD ["python", "app.py"]
