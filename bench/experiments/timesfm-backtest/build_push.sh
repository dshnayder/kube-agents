#!/usr/bin/env bash
# Build both images for linux/amd64 and push them to the demo registry. Run where docker is.
set -euo pipefail

REGISTRY="${REGISTRY:-us-central1-docker.pkg.dev/agentic-harness-demo/demo}"
TAG="${TAG:-v1}"
HERE="$(cd "$(dirname "$0")" && pwd)"

docker build --platform linux/amd64 -t "${REGISTRY}/timesfm-forecaster:${TAG}" "${HERE}/forecaster"
docker build --platform linux/amd64 -t "${REGISTRY}/timesfm-backtest:${TAG}" "${HERE}/harness"
docker push "${REGISTRY}/timesfm-forecaster:${TAG}"
docker push "${REGISTRY}/timesfm-backtest:${TAG}"
