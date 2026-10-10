#!/usr/bin/env bash
# Build both images locally and load them into minikube.
#
# Each image is tagged with a hash of its *inputs* (Dockerfile, lock file,
# source). Same inputs -> same tag -> nothing is built or loaded and Helm sees
# no change, which keeps `make up` idempotent. Hashing the built image would not
# work: Docker stamps a fresh creation time on every build.
set -euo pipefail

mkdir -p .build

inputs_hash() {
  find "$@" -type f -not -path '*/__pycache__/*' -print0 |
    sort -z | xargs -0 sha256sum | sha256sum | cut -c1-12
}

build() {
  local name=$1 context=$2
  shift 2
  local ref
  ref="docker.io/library/$name:$(inputs_hash "$@")"
  if minikube image ls | grep -qxF "$ref"; then
    echo "[images] $ref already in minikube"
  else
    echo "[images] building $ref"
    docker build -q -t "$ref" "$context" >/dev/null
    echo "[images] loading $ref into minikube"
    minikube image load "$ref"
  fi
  echo "$ref" >".build/$name.ref"
}

build aggregator . Dockerfile .dockerignore pyproject.toml uv.lock app
build openbao-oauthapp openbao openbao
