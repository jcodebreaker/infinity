#!/usr/bin/env bash
# Builds Superchat's two images from this checkout.
#
#   deployment/build.sh                     # engine and host, tagged superchat/<name>:local
#   deployment/build.sh engine              # one of them
#   IMAGE_TAG=v1.4.0 IMAGE_REGISTRY=ghcr.io/acme PUSH=1 PLATFORM=linux/amd64,linux/arm64 \
#     deployment/build.sh                   # multi-arch build pushed to a registry
#
# Optional environment, all unset by default (see .env.docker.example):
#   NODE_IMAGE PYTHON_IMAGE UBUNTU_IMAGE CHROMIUM_IMAGE   base images, for registry mirrors
#   PYPI_INDEX_URL NPM_CONFIG_REGISTRY TORCH_INDEX_URL    package mirrors
#   CA_BUNDLE=/path/ca.pem                                a company CA, passed as the build secret
#   CLAUDE_CODE_VERSION PI_VERSION                        the two CLIs in the host image
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE_REGISTRY="${IMAGE_REGISTRY:-superchat}"
IMAGE_TAG="${IMAGE_TAG:-local}"
images=("$@")
[[ ${#images[@]} -gt 0 ]] || images=(engine host)

build() {
  local name="$1"
  local args=(--file "$ROOT/deployment/$name.Dockerfile" --tag "$IMAGE_REGISTRY/$name:$IMAGE_TAG")
  local var
  for var in NODE_IMAGE PYTHON_IMAGE UBUNTU_IMAGE CHROMIUM_IMAGE PYPI_INDEX_URL NPM_CONFIG_REGISTRY \
             TORCH_INDEX_URL CLAUDE_CODE_VERSION PI_VERSION; do
    [[ -z "${!var:-}" ]] || args+=(--build-arg "$var=${!var}")
  done
  [[ -z "${CA_BUNDLE:-}" ]] || args+=(--secret "id=ca_bundle,src=$CA_BUNDLE")
  [[ -z "${PLATFORM:-}" ]] || args+=(--platform "$PLATFORM")
  if [[ "${PUSH:-0}" == "1" ]]; then
    docker buildx build "${args[@]}" --push "$ROOT"
  else
    docker build "${args[@]}" "$ROOT"
  fi
  echo "built $IMAGE_REGISTRY/$name:$IMAGE_TAG"
}

for name in "${images[@]}"; do
  case "$name" in
    engine|host) build "$name" ;;
    *) echo "unknown image '$name' (engine, host)" >&2; exit 2 ;;
  esac
done
