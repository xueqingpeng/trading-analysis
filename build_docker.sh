#!/usr/bin/env bash
set -euo pipefail
IMAGE="${IMAGE:-trading-analysis:latest}"
HERE="$(cd "$(dirname "$0")" && pwd)"
docker build -f "$HERE/docker/Dockerfile" -t "$IMAGE" "$HERE" "$@"
