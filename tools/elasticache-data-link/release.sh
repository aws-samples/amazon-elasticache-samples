#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# ECDL image release — builds and publishes the five container images:
#   ecdl/ecdl-sink, ecdl/ecdl-debezium-server, ecdl/ecdl-loader (the pipeline)
#   ecdl/ecdl-control-plane-api, ecdl/ecdl-control-plane-ui (the control plane)
#
#   ./release.sh build          build the images locally (host arch only)
#   ./release.sh push           build multi-arch and push to this account's ECR,
#                               recording the digests in infra/images.lock
#   ./release.sh pin            set every template's ImageTag default to VERSION
#   ./release.sh templates      upload the link templates to
#                               s3://$ECDL_TEMPLATE_BUCKET/templates/VERSION/ (also
#                               run at the end of push when the bucket is set)
#   ./release.sh show           print what would be built and where it would go
#
# RUN `push` BEFORE THE FIRST DEPLOY IN AN ACCOUNT. The templates default to
# <this account>.dkr.ecr.<region>.<suffix>/ecdl/<image>:<ImageTag>, so until the
# images are pushed there is nothing for the ECS tasks (or the control-plane
# instance) to pull, and they fail with CannotPullContainerError.
#
# DESIGN NOTES
#   * Registry and account come from the environment or from the caller's own
#     credentials. Nothing is hardcoded.
#   * Repositories are created with IMMUTABLE tags, so a version tag cannot be
#     moved once pushed and a redeploy of an unchanged stack gets the same bytes.
#     push refuses an existing repository with MUTABLE tags, and turns on
#     scan-on-push for each repository. It also refuses a tree with uncommitted
#     changes, so every pushed version is a commit.
#     The digests of all five images are recorded in infra/images.lock for
#     anyone who wants to pin one deployment by digest (pass the *Image
#     parameter). Commit that file after a push; it is only ever written here.
#   * Multi-arch: linux/amd64 + linux/arm64, so Fargate can run on Graviton.
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

VERSION="${VERSION:-$(tr -d '[:space:]' < VERSION)}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"

# Commit identity. A dirty tree is marked, because a version tag on a build that
# does not correspond to any commit is misleading: it points at code that was
# never committed.
#
# COMMIT may be supplied by the caller. That is not a convenience: the build often
# does not happen in the git checkout -- a multi-arch build needs buildx, so it may
# run on a remote builder or in CI that received a source tarball rather than a
# clone. Without an override those builds record "unknown", so the build carries
# no commit at all. A locally computed value still takes precedence when present.
if [ -z "${COMMIT:-}" ]; then
  if git rev-parse --git-dir >/dev/null 2>&1; then
    COMMIT="$(git rev-parse --short=7 HEAD 2>/dev/null || echo unknown)"
    if ! git diff --quiet HEAD 2>/dev/null || [ -n "$(git status --porcelain 2>/dev/null)" ]; then
      COMMIT="${COMMIT}-dirty"
    fi
  else
    COMMIT="unknown"
  fi
fi
BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

SINK_REPO="${SINK_REPO:-ecdl/ecdl-sink}"
DBZ_REPO="${DBZ_REPO:-ecdl/ecdl-debezium-server}"
# Upstream Debezium Server, pinned by digest (the multi-arch index). It is not on
# ECR Public, so push copies it into the publishing account's ECR as
# ${DBZ_BASE_REPO}:${DBZ_BASE_TAG} and builds from that copy: every image this
# script builds then pulls its base from ECR, never from quay.io directly.
DBZ_UPSTREAM="quay.io/debezium/server:3.0.6.Final@sha256:b9f4e9d66cf69ae0629ed5383334ca0bd1756ec1e4d0216d66a4c46b44393760"
DBZ_BASE_REPO="${DBZ_BASE_REPO:-ecdl/debezium-server-base}"
DBZ_BASE_TAG="3.0.6.Final"
LOADER_REPO="${LOADER_REPO:-ecdl/ecdl-loader}"
# The control plane itself. infra/ecdl-control-plane.yaml defaults to these, so
# they have to be published or that template's default refers to an image
# that does not exist and fails at `docker run` with "manifest unknown" -- on the instance, after
# the stack has already reported CREATE_COMPLETE.
CP_API_REPO="${CP_API_REPO:-ecdl/ecdl-control-plane-api}"
CP_UI_REPO="${CP_UI_REPO:-ecdl/ecdl-control-plane-ui}"
DIGEST_FILE="${REPO_ROOT}/infra/images.lock"

# ── Registry ───────────────────────────────────────────────────────────────
# Resolved LAZILY, by the subcommands that actually need a registry. `pin` reads
# infra/images.lock and edits a template, so it must not require credentials --
# it did, which meant pinning failed for anyone not logged in to AWS at that
# moment, including CI.
#
# ECDL_REGISTRY takes precedence. Otherwise an ECR registry is derived from whoever is logged
# in, so publishing into your own account needs no edit to this file.
resolve_registry () {
  if [ -z "${ECDL_REGISTRY:-}" ]; then
    AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-2}}"
    ACCOUNT_ID="${ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)}"
    if [ -z "$ACCOUNT_ID" ] || [ "$ACCOUNT_ID" = "None" ]; then
      echo "ERROR: could not determine an AWS account for the registry." >&2
      echo "  Either export ECDL_REGISTRY (any OCI registry), or make sure your AWS" >&2
      echo "  credentials work: aws sts get-caller-identity" >&2
      echo "  (AWS_PROFILE is respected, and is not set to anything by this script.)" >&2
      exit 1
    fi
    ECDL_REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
  fi
  SINK_IMAGE="${ECDL_REGISTRY}/${SINK_REPO}"
  DBZ_IMAGE="${ECDL_REGISTRY}/${DBZ_REPO}"
  LOADER_IMAGE="${ECDL_REGISTRY}/${LOADER_REPO}"
  CP_API_IMAGE="${ECDL_REGISTRY}/${CP_API_REPO}"
  CP_UI_IMAGE="${ECDL_REGISTRY}/${CP_UI_REPO}"
  DBZ_BASE_TAGGED="${ECDL_REGISTRY}/${DBZ_BASE_REPO}:${DBZ_BASE_TAG}"
  # Builds use the copy BY DIGEST, so what is built is exactly the pinned
  # upstream index whatever the tag points at.
  DBZ_BASE_IMAGE="${ECDL_REGISTRY}/${DBZ_BASE_REPO}@${DBZ_UPSTREAM##*@}"
}

# Copy the pinned upstream Debezium index into our ECR, once, and check it.
#
# The mirror repository is the one ECDL repository with MUTABLE tags, on
# purpose. buildx copies a multi-architecture index one manifest at a time
# (two images plus two attestation manifests), each PUT under the tag; ECR
# with IMMUTABLE tags accepts the first and rejects the rest with 400, which
# left only the amd64 image. Builds reference the copy by digest and the
# digest is verified below, so a moved tag cannot change what is built.
mirror_debezium_base () {
  local want="${DBZ_UPSTREAM##*@}" have region
  region="$(printf '%s' "$ECDL_REGISTRY" | sed -E 's/.*\.dkr\.ecr\.([^.]+)\.amazonaws\.com/\1/')"
  have="$(docker buildx imagetools inspect "$DBZ_BASE_TAGGED" 2>/dev/null \
            | awk '/^Digest:/ {print $2; exit}' || true)"
  if [ "$have" != "$want" ]; then
    case "$ECDL_REGISTRY" in
      *.dkr.ecr.*.amazonaws.com)
        aws ecr put-image-tag-mutability --repository-name "$DBZ_BASE_REPO" \
          --image-tag-mutability MUTABLE --region "$region" >/dev/null ;;
    esac
    say "mirroring ${DBZ_UPSTREAM} -> ${DBZ_BASE_TAGGED}"
    docker buildx imagetools create --tag "$DBZ_BASE_TAGGED" "$DBZ_UPSTREAM"
    have="$(docker buildx imagetools inspect "$DBZ_BASE_TAGGED" | awk '/^Digest:/ {print $2; exit}')"
  fi
  if [ "$have" != "$want" ]; then
    echo "ERROR: ${DBZ_BASE_TAGGED} is ${have}, expected the pinned ${want}." >&2
    exit 1
  fi
  # Every platform the build needs must be in the copy, not just the first.
  local archs p
  archs="$(docker buildx imagetools inspect --raw "$DBZ_BASE_IMAGE" | python3 -c \
    'import json,sys; print(" ".join(m.get("platform",{}).get("architecture","") for m in json.load(sys.stdin).get("manifests",[])))')"
  for p in ${PLATFORMS//,/ }; do
    case " $archs " in
      *" ${p#*/} "*) ;;
      *) echo "ERROR: ${DBZ_BASE_IMAGE} has no ${p} image (has: ${archs})." >&2; exit 1 ;;
    esac
  done
  say "debezium base ${DBZ_BASE_IMAGE}"
}

say () { printf '%s\n' "$*"; }
hr  () { printf '─────────────────────────────────────────────────────────────\n'; }

show () {
  resolve_registry
  hr
  say "version    ${VERSION}"
  say "commit     ${COMMIT}"
  say "built      ${BUILD_DATE}"
  say "registry   ${ECDL_REGISTRY}"
  say "platforms  ${PLATFORMS}"
  hr
  say "sink       ${SINK_IMAGE}:${VERSION}"
  say "           context tools/go-sink"
  say "debezium   ${DBZ_IMAGE}:${VERSION}"
  say "           context data-plane/debezium-server (base ${DBZ_BASE_IMAGE}, mirrored from upstream by digest, + config)"
  say "loader     ${LOADER_IMAGE}:${VERSION}"
  say "           context data-plane/bulk-loader (initial load; run-once Fargate task)"
  say "cp-api     ${CP_API_IMAGE}:${VERSION}"
  say "           context backend (control-plane API; infra/ecdl-control-plane.yaml)"
  say "cp-ui      ${CP_UI_IMAGE}:${VERSION}"
  say "           context frontend (control-plane UI)"
  hr
}

require_buildx () {
  if ! docker buildx version >/dev/null 2>&1; then
    echo "ERROR: docker buildx is required for a multi-architecture build." >&2
    echo "  Docker Desktop ships it. On Linux: docker-buildx-plugin." >&2
    exit 1
  fi
  # A multi-platform build needs a builder that is not the default docker driver.
  if ! docker buildx inspect ecdl-builder >/dev/null 2>&1; then
    say "creating buildx builder 'ecdl-builder' (the default driver cannot do multi-platform)"
    docker buildx create --name ecdl-builder --driver docker-container --bootstrap >/dev/null
  fi
  docker buildx use ecdl-builder
}

ecr_login () {
  case "$ECDL_REGISTRY" in
    *.dkr.ecr.*.amazonaws.com)
      local region
      region="$(printf '%s' "$ECDL_REGISTRY" | sed -E 's/.*\.dkr\.ecr\.([^.]+)\.amazonaws\.com/\1/')"
      say "ECR login to ${ECDL_REGISTRY} (${region})"
      aws ecr get-login-password --region "$region" \
        | docker login --username AWS --password-stdin "$ECDL_REGISTRY" >/dev/null
      local mutable=()
      for repo in "$SINK_REPO" "$DBZ_REPO" "$LOADER_REPO" \
                  "$CP_API_REPO" "$CP_UI_REPO"; do
        local settings mutability scan
        settings="$(aws ecr describe-repositories --repository-names "$repo" --region "$region" \
                      --query 'repositories[0].[imageTagMutability,imageScanningConfiguration.scanOnPush]' \
                      --output text 2>/dev/null)" \
          || { say "creating ECR repository ${repo}"
               aws ecr create-repository --repository-name "$repo" --region "$region" \
                 --image-tag-mutability IMMUTABLE \
                 --image-scanning-configuration scanOnPush=true >/dev/null
               settings="IMMUTABLE True"; }
        read -r mutability scan <<<"$settings"
        # The templates default to <repo>:<ImageTag>, so a tag that can be moved
        # is a deployment that can change without notice. Repositories this script
        # creates are IMMUTABLE; an existing one may not be, and is refused
        # rather than warned about (a warning scrolls past in a long build).
        if [ "$mutability" != "IMMUTABLE" ]; then
          mutable+=("$repo")
        fi
        # Scan each image for known vulnerabilities as it is pushed.
        if [ "$scan" != "True" ]; then
          say "turning on scan-on-push for ${repo}"
          aws ecr put-image-scanning-configuration --repository-name "$repo" --region "$region" \
            --image-scanning-configuration scanOnPush=true >/dev/null
        fi
      done
      if [ ${#mutable[@]} -gt 0 ]; then
        echo "ERROR: these repositories have MUTABLE tags, so :${VERSION} could be overwritten" >&2
        echo "  and the template defaults would not be pinned. Make them immutable:" >&2
        for repo in "${mutable[@]}"; do
          echo "  aws ecr put-image-tag-mutability --repository-name ${repo} --image-tag-mutability IMMUTABLE --region ${region}" >&2
        done
        exit 1
      fi
      # The Debezium base mirror: MUTABLE on purpose (see mirror_debezium_base);
      # builds use it by digest, so the tag is never what is trusted.
      aws ecr describe-repositories --repository-names "$DBZ_BASE_REPO" --region "$region" >/dev/null 2>&1 \
        || { say "creating ECR repository ${DBZ_BASE_REPO}"
             aws ecr create-repository --repository-name "$DBZ_BASE_REPO" --region "$region" \
               --image-tag-mutability MUTABLE \
               --image-scanning-configuration scanOnPush=true >/dev/null; }
      ;;
    *) say "non-ECR registry ${ECDL_REGISTRY}: assuming you are already logged in" ;;
  esac
}

# Local build, host architecture only. For a quick check that both images build;
# it does NOT produce the artifact that gets deployed.
do_build () {
  resolve_registry
  show
  say "local single-arch build (use 'push' for the real multi-arch artifact)"
  docker build \
    --build-arg VERSION="$VERSION" --build-arg COMMIT="$COMMIT" --build-arg BUILD_DATE="$BUILD_DATE" \
    -t "${SINK_IMAGE}:${VERSION}" tools/go-sink
  # Builds from the ECR copy of the base, so it needs `push` to have mirrored it
  # once (or DBZ_BASE_IMAGE pointed at another copy of the same digest).
  docker build --build-arg DEBEZIUM_BASE="$DBZ_BASE_IMAGE" \
    -t "${DBZ_IMAGE}:${VERSION}" data-plane/debezium-server
  docker build \
    --build-arg VERSION="$VERSION" --build-arg COMMIT="$COMMIT" --build-arg BUILD_DATE="$BUILD_DATE" \
    -t "${LOADER_IMAGE}:${VERSION}" data-plane/bulk-loader
  docker build \
    --build-arg VERSION="$VERSION" --build-arg COMMIT="$COMMIT" --build-arg BUILD_DATE="$BUILD_DATE" \
    -f backend/Dockerfile.release \
    -t "${CP_API_IMAGE}:${VERSION}" .
  docker build -f frontend/Dockerfile.prod \
    -t "${CP_UI_IMAGE}:${VERSION}" frontend
  say "built sink, debezium-server, loader and the control plane at ${VERSION}"
}

# A single-architecture build only runs on Fargate tasks of that architecture,
# and the templates default to X86_64. Say so before building rather than
# letting the first deploy fail with "exec format error".
warn_single_arch () {
  case "$PLATFORMS" in
    *,*) ;;   # multi-arch: runs on either
    *arm64*)
      say "NOTE: arm64-only build. Deploy with the tasks on ARM64:"
      say "      link: advanced.cpu_architecture: arm64 | quickstart: ECDL_CPU_ARCH=ARM64"
      say "      | CloudFormation: CpuArchitecture=ARM64 (cdc and loader stacks)" ;;
    *amd64*)
      say "NOTE: amd64-only build. The tasks must stay on X86_64 (the default)." ;;
  esac
}

# A pushed version must be a commit: a "-dirty" build carries code that no
# commit has, so nobody can rebuild or review what was released.
require_clean_tree () {
  case "$COMMIT" in
    *-dirty)
      echo "ERROR: the working tree has uncommitted changes (commit ${COMMIT})." >&2
      echo "  Commit or stash them, then push again. git status lists them." >&2
      exit 1 ;;
  esac
}

do_push () {
  require_clean_tree
  resolve_registry
  show
  warn_single_arch
  require_buildx
  ecr_login

  # --no-cache: every pushed image takes the OS fixes published up to today.
  # A cached `apk upgrade` / layer would ship the fixes of the last build.
  say "building and pushing sink (${PLATFORMS})"
  docker buildx build --no-cache --platform "$PLATFORMS" \
    --build-arg VERSION="$VERSION" --build-arg COMMIT="$COMMIT" --build-arg BUILD_DATE="$BUILD_DATE" \
    -t "${SINK_IMAGE}:${VERSION}" --push tools/go-sink

  # The Debezium image is upstream + config files. Upstream publishes amd64 and
  # arm64, so the same --platform list applies and BuildKit picks the right base
  # for each. There is nothing to compile.
  mirror_debezium_base
  say "building and pushing debezium-server (${PLATFORMS})"
  docker buildx build --no-cache --platform "$PLATFORMS" \
    --build-arg VERSION="$VERSION" --build-arg COMMIT="$COMMIT" --build-arg BUILD_DATE="$BUILD_DATE" \
    --build-arg DEBEZIUM_BASE="$DBZ_BASE_IMAGE" \
    -t "${DBZ_IMAGE}:${VERSION}" --push data-plane/debezium-server

  say "building and pushing loader (${PLATFORMS})"
  docker buildx build --no-cache --platform "$PLATFORMS" \
    --build-arg VERSION="$VERSION" --build-arg COMMIT="$COMMIT" --build-arg BUILD_DATE="$BUILD_DATE" \
    -t "${LOADER_IMAGE}:${VERSION}" --push data-plane/bulk-loader

  # The control-plane API image needs ecdl_link/ and infra/, which live at the
  # repo ROOT. Built from the root with backend/Dockerfile.release, which copies
  # them in. It used backend/Dockerfile, which is written for a backend/ context:
  # from the root its COPY requirements.txt does not exist, so push failed here.
  say "building and pushing control-plane API (${PLATFORMS})"
  docker buildx build --no-cache --platform "$PLATFORMS" \
    --build-arg VERSION="$VERSION" --build-arg COMMIT="$COMMIT" --build-arg BUILD_DATE="$BUILD_DATE" \
    -f backend/Dockerfile.release \
    -t "${CP_API_IMAGE}:${VERSION}" --push .

  say "building and pushing control-plane UI (${PLATFORMS})"
  docker buildx build --no-cache --platform "$PLATFORMS" -f frontend/Dockerfile.prod \
    -t "${CP_UI_IMAGE}:${VERSION}" --push frontend

  record_digests
  say ""
  say "wrote ${DIGEST_FILE}. Run './release.sh pin' to set the templates' ImageTag to ${VERSION}."
  if [ -n "${ECDL_TEMPLATE_BUCKET:-}" ]; then
    do_templates
  else
    say "ECDL_TEMPLATE_BUCKET not set: templates not uploaded (the control plane deploys from them)"
  fi
}

# The link templates the control plane deploys (decision D1). It creates every
# link stack from these objects, through its deploy role, and may read but not
# write the bucket -- so what it can build is exactly what was released. Each
# version has its own prefix; an uploaded version is never overwritten.
LINK_TEMPLATES="ecdl-broker-quickstart.yaml ecdl-cdc-ecs.yaml ecdl-loader-ecs.yaml"
# The control plane's own template goes to the same prefix: it is larger than
# the 51,200 bytes CloudFormation accepts inline, so it is created from S3
# (--template-url) too.
UPLOADED_TEMPLATES="$LINK_TEMPLATES ecdl-control-plane.yaml"
do_templates () {
  : "${ECDL_TEMPLATE_BUCKET:?set ECDL_TEMPLATE_BUCKET to the TemplateBucket of the control plane}"
  local f tag dest
  for f in $UPLOADED_TEMPLATES; do
    tag="$(awk '/^  ImageTag:/{p=1} p&&/^    Default:/{print $2; exit}' "infra/$f")"
    if [ -n "$tag" ] && [ "$tag" != "$VERSION" ]; then
      echo "ERROR: infra/$f pins ImageTag $tag, not $VERSION; run './release.sh pin' first" >&2
      exit 1
    fi
  done
  dest="s3://${ECDL_TEMPLATE_BUCKET}/templates/${VERSION}"
  for f in $UPLOADED_TEMPLATES; do
    if aws s3api head-object --bucket "$ECDL_TEMPLATE_BUCKET" \
         --key "templates/${VERSION}/$f" >/dev/null 2>&1; then
      if ! aws s3 cp "$dest/$f" - 2>/dev/null | cmp -s - "infra/$f"; then
        echo "ERROR: $dest/$f exists with different content; a released version is never replaced" >&2
        exit 1
      fi
      say "template $f already uploaded for ${VERSION}"
      continue
    fi
    aws s3 cp "infra/$f" "$dest/$f" --only-show-errors
    say "uploaded $dest/$f"
  done
}

# Resolve each tag to its immutable digest and write them down, asking the
# registry so the recorded value is what it actually serves. ECR is asked
# directly rather than parsing buildx: the
# `imagetools inspect --format '{{.Manifest.Digest}}'` used here before printed
# the whole human-readable manifest list on buildx 0.17, and that text was
# written into images.lock as the "digest". Anything that is not sha256:<64 hex>
# is treated as "could not read".
_digest () {
  local image="$1" tag="$2" d="" region
  case "$ECDL_REGISTRY" in
    *.dkr.ecr.*.amazonaws.com)
      region="$(printf '%s' "$ECDL_REGISTRY" | sed -E 's/.*\.dkr\.ecr\.([^.]+)\.amazonaws\.com/\1/')"
      d="$(aws ecr describe-images --repository-name "${image#*/}" --image-ids imageTag="$tag" \
            --region "$region" --query 'imageDetails[0].imageDigest' \
            --output text 2>/dev/null || true)" ;;
  esac
  if [[ ! "$d" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    d="$(docker buildx imagetools inspect "${image}:${tag}" 2>/dev/null \
          | awk '/^Digest:/ {print $2; exit}' || true)"
  fi
  [[ "$d" =~ ^sha256:[0-9a-f]{64}$ ]] && printf '%s' "$d" || true
}

record_digests () {
  local sink_digest dbz_digest loader_digest
  sink_digest="$(_digest "${SINK_IMAGE}" "${VERSION}")"
  dbz_digest="$(_digest "${DBZ_IMAGE}" "${VERSION}")"
  loader_digest="$(_digest "${LOADER_IMAGE}" "${VERSION}")"
  local cp_api_digest cp_ui_digest
  cp_api_digest="$(_digest "${CP_API_IMAGE}" "${VERSION}")"
  cp_ui_digest="$(_digest "${CP_UI_IMAGE}" "${VERSION}")"
  # All five, or none: a lock that records three images is one that silently
  # stops describing the control plane.
  local missing=()
  [ -n "$sink_digest" ]   || missing+=("${SINK_IMAGE}:${VERSION}")
  [ -n "$dbz_digest" ]    || missing+=("${DBZ_IMAGE}:${VERSION}")
  [ -n "$loader_digest" ] || missing+=("${LOADER_IMAGE}:${VERSION}")
  [ -n "$cp_api_digest" ] || missing+=("${CP_API_IMAGE}:${VERSION}")
  [ -n "$cp_ui_digest" ]  || missing+=("${CP_UI_IMAGE}:${VERSION}")
  if [ ${#missing[@]} -gt 0 ]; then
    echo "ERROR: could not read a digest back from the registry for:" >&2
    printf '  %s\n' "${missing[@]}" >&2
    echo "  ${DIGEST_FILE} was not written." >&2
    exit 1
  fi
  mkdir -p "$(dirname "$DIGEST_FILE")"
  # Written to a temporary file and renamed, so a failure part way leaves the
  # previous lock rather than half of a new one.
  cat > "${DIGEST_FILE}.tmp" <<EOF
# ECDL published images. Generated by release.sh -- do not edit manually.
#
# A tag is a mutable pointer and a digest is the image. Deployments reference the
# digest so that redeploying an unchanged stack cannot pick up different bytes.
version=${VERSION}
commit=${COMMIT}
built=${BUILD_DATE}
platforms=${PLATFORMS}
sink_image=${SINK_IMAGE}:${VERSION}
sink_digest=${sink_digest}
sink_pinned=${SINK_IMAGE}@${sink_digest}
debezium_image=${DBZ_IMAGE}:${VERSION}
debezium_digest=${dbz_digest}
debezium_pinned=${DBZ_IMAGE}@${dbz_digest}
debezium_base=${DBZ_BASE_IMAGE}
debezium_upstream=${DBZ_UPSTREAM}
loader_image=${LOADER_IMAGE}:${VERSION}
loader_digest=${loader_digest}
loader_pinned=${LOADER_IMAGE}@${loader_digest}
control_plane_api_image=${CP_API_IMAGE}:${VERSION}
control_plane_api_digest=${cp_api_digest}
control_plane_api_pinned=${CP_API_IMAGE}@${cp_api_digest}
control_plane_ui_image=${CP_UI_IMAGE}:${VERSION}
control_plane_ui_digest=${cp_ui_digest}
control_plane_ui_pinned=${CP_UI_IMAGE}@${cp_ui_digest}
EOF
  mv "${DIGEST_FILE}.tmp" "$DIGEST_FILE"
  say "sink     ${SINK_IMAGE}@${sink_digest}"
  say "debezium ${DBZ_IMAGE}@${dbz_digest}"
  say "loader   ${LOADER_IMAGE}@${loader_digest}"
  say "cp-api   ${CP_API_IMAGE}@${cp_api_digest}"
  say "cp-ui    ${CP_UI_IMAGE}@${cp_ui_digest}"
}

# Rewrite the CloudFormation defaults from images.lock. Separate from push on
# purpose: pushing is a registry operation and pinning edits tracked files, and
# mixing the two makes it unclear what a failed run left behind.
#
# What it rewrites is the ImageTag default, not image URIs. The image defaults
# are empty and resolve to the DEPLOYING account's ECR at ImageTag, so the
# templates hold no account id and work in any account that ran `push`. Pinning
# used to write this account's digests into the defaults, which is how the
# public templates came to point at one development account that no other account could
# pull from. To pin a digest for one deployment, pass DebeziumImage / SinkImage /
# LoaderImage (or advanced.images in the link); images.lock records the digests.
do_pin () {
  local tpl
  for tpl in infra/ecdl-cdc-ecs.yaml infra/ecdl-loader-ecs.yaml infra/ecdl-control-plane.yaml; do
    python3 - "$tpl" "$VERSION" <<'PY'
import re, sys
path, version = sys.argv[1], sys.argv[2]
src = open(path).read()
# Replace only the Default: line inside the ImageTag parameter's block.
pat = re.compile(r'(^  ImageTag:\n(?:.*\n)*?    Default: )(\S+)', re.M)
new, n = pat.subn(lambda m: m.group(1) + version, src, count=1)
if n != 1:
    sys.exit("could not find a single Default: for ImageTag in %s" % path)
# The control plane's wait condition is named for its release (Jira story F179): a new
# name is what makes CloudFormation wait for the instance again on the update.
tag = re.sub(r'[^A-Za-z0-9]', 'x', version)
new = re.sub(r'\b(Ready(?:Handle)?V)[A-Za-z0-9]+\b', lambda m: m.group(1) + tag, new)
open(path, 'w').write(new)
print("pinned ImageTag -> %s in %s" % (version, path))
PY
  done
}

case "${1:-show}" in
  build) do_build ;;
  push)  do_push ;;
  pin)   do_pin ;;
  templates) do_templates ;;
  show)  show ;;
  *) echo "usage: $0 {build|push|pin|templates|show}" >&2; exit 2 ;;
esac
