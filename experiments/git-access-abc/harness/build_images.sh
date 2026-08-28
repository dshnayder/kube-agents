#!/usr/bin/env bash
# Build and push the three images the experiment runs on, at one tag.
#
# One tag for all three because the arms are only comparable if the only thing
# that differs between them is the arm. Building the sandbox at one tag and the
# proxy at another is how a "result" turns out to be a version skew.
#
# REPO is the `demo` Artifact Registry repository, which is the one the live
# install pulls from. `make`'s own default is a `kube-agents` repository that
# does not exist in this project, and a push to it fails with "Repository not
# found" -- in a pipeline that hid the exit status, once.
set -euo pipefail
TAG="${1:?usage: build_images.sh <tag>}"
REPO="YOUR-REGION-docker.pkg.dev/YOUR-PROJECT/YOUR-REPO"
cd ~/kube-agents-sync

make REPO="$REPO" docker-build-platform docker-build-credential-proxy docker-build-sandbox

for image in platform-agent credential-proxy agent-sandbox; do
  docker tag "${REPO}/${image}:latest" "${REPO}/${image}:${TAG}"
  docker push "${REPO}/${image}:${TAG}"
done
echo "pushed ${TAG}"
