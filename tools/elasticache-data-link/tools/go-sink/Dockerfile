# ═══════════════════════════════════════════════════════════════════════════
# ECDL Go sink — broker Valkey (Streams) -> target Valkey (Hashes)
#
# Multi-architecture. The build stage is pinned to the BUILD host's platform
# ($BUILDPLATFORM) and cross-compiles to the TARGET platform, because Go
# cross-compiles natively and emulating an arm64 toolchain under QEMU to do the
# same work takes minutes instead of seconds.
#
# TARGETARCH / TARGETOS are supplied automatically by BuildKit for each platform
# in --platform. This used to be a hardcoded GOARCH=amd64, which meant the image
# could not run on Graviton -- and Graviton is the cheaper choice for a workload
# that is almost entirely network and JSON.
# ═══════════════════════════════════════════════════════════════════════════
# ECR Public mirrors of the Docker official images (not Docker Hub).
# Base images are pinned by digest (Jira story F181), so a rebuild cannot pick up different
# bytes under the same tag. To move to a newer image, look the digest up with
#   docker buildx imagetools inspect <image>:<tag>
# and replace it; the tag stays for readers.
FROM --platform=$BUILDPLATFORM public.ecr.aws/docker/library/golang:1.27@sha256:205c7636d74e0ffbe3d007162df7d74f859f5a8c8322dfb9f907977686c126cf AS build
ARG TARGETOS
ARG TARGETARCH
# Build identity, stamped into the binary. Defaults match version.go, so an
# unstamped build says "dev" rather than claiming to be a release.
ARG VERSION=dev
ARG COMMIT=unknown
ARG BUILD_DATE=unknown

WORKDIR /src
# Dependencies first, so a code-only change reuses the module cache layer.
COPY go.mod go.sum ./
RUN go mod download

COPY *.go ./
# CGO off for a static binary that needs no libc from the runtime image.
# -trimpath keeps build paths out of the binary, so the same source produces the
# same bytes regardless of where it was built.
RUN CGO_ENABLED=0 GOOS=${TARGETOS} GOARCH=${TARGETARCH} \
    go build -trimpath \
      -ldflags="-s -w \
        -X main.version=${VERSION} \
        -X main.commit=${COMMIT} \
        -X main.buildDate=${BUILD_DATE}" \
      -o /out/ecdl-go-sink .

# Runtime: alpine for a shell and curl, which the ECS container health check uses.
FROM public.ecr.aws/docker/library/alpine:3.24@sha256:f59abe656b692d901e184be3445e084786602108599d2a51d229c0f5a48c71bf
# apk upgrade takes the OS fixes published since the base image was built.
RUN apk upgrade --no-cache && apk add --no-cache curl ca-certificates
COPY --from=build /out/ecdl-go-sink /usr/local/bin/ecdl-go-sink

# Repeated on the runtime stage: ARGs do not cross stages, and these become the
# image labels that let a running task be traced back to a commit.
ARG VERSION=dev
ARG COMMIT=unknown
ARG BUILD_DATE=unknown
LABEL org.opencontainers.image.title="ecdl-go-sink" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${COMMIT}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.description="ECDL sink: broker Valkey Streams to target Valkey hashes"

# Nothing here needs root.
RUN adduser -D -u 10001 ecdl
USER 10001

EXPOSE 8081
# Configuration is entirely environment: BROKER_*, TARGET_*, STREAM_KEY(S),
# WORKERS, BATCH_SIZE, TRANSFORMS_JSON, and the credential variables handled by
# connect.go. /version reports which build is running.
# The same probe as the ECS task definition: 503 when the sink has stopped
# reading the broker (health.go).
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS -m 4 http://127.0.0.1:8081/health || exit 1
ENTRYPOINT ["/usr/local/bin/ecdl-go-sink"]
