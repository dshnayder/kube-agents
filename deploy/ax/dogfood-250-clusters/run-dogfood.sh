#!/usr/bin/env bash
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

set -euo pipefail

export PATH="${HOME}/bin:${PATH}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTEXT="${KUBECTL_CONTEXT:-kage-adc}"
AX_BIN="${AX_BIN:-$(command -v ax || echo "${HOME}/bin/ax")}"
COUNT="${CLUSTER_COUNT:-250}"
CONCURRENCY="${CONCURRENCY:-16}"
IMAGE="${AX_KUBE_AGENT_IMAGE:-us-central1-docker.pkg.dev/agentic-harness-demo/ate-images/ax-kube-agent@sha256:6625820ce5ac879a4771b83d88f3f27541204276f1c7f10b7dfb7c9e8daf825f}"
SANDBOX_IMAGE="${AX_KUBE_SANDBOX_IMAGE:-us-central1-docker.pkg.dev/agentic-harness-demo/ate-images/ax-kube-sandbox@sha256:9fbb446bed4ae22b67ed9ab78d5cd20d78aeee86186b1eaf638e2b23fa6bc024}"

echo "==> 1. Applying Gateway/kube-agents-cluster-gw from ${SCRIPT_DIR}/gateway.yaml on context ${CONTEXT}..."
"${AX_BIN}" apply -f "${SCRIPT_DIR}/gateway.yaml" --context="${CONTEXT}"

echo "==> 2. Provisioning 1 shared 2-container ActorTemplate via cluster-agent-seed (agent=${IMAGE}, sandbox=${SANDBOX_IMAGE}, limits: cpu=2, memory=2Gi)..."
"${AX_BIN}" apply --context="${CONTEXT}" -f - <<EOF
apiVersion: ax.io/v1alpha1
kind: Task
metadata:
  name: cluster-agent-seed
  atespace: default
spec:
  debug: true
  profile:
    image: ${IMAGE}
    resources:
      limits:
        cpu: "2"
        memory: "2Gi"
  gateway:
    name: kube-agents-cluster-gw
  env:
    - name: AX_SANDBOX_IMAGE
      value: ${SANDBOX_IMAGE}
EOF

SHARED_TEMPLATE="$("${AX_BIN}" get task cluster-agent-seed --context="${CONTEXT}" | awk '/ateActorTemplate:/ {print $2}')"
if [[ -z "${SHARED_TEMPLATE}" ]]; then
  echo "error: failed to resolve shared ateActorTemplate from cluster-agent-seed" >&2
  exit 1
fi
echo "    Shared ActorTemplate: ${SHARED_TEMPLATE}"

echo "==> 3. Ensuring default platform-agent AX Task shares ${SHARED_TEMPLATE}..."
"${AX_BIN}" apply --context="${CONTEXT}" -f - <<EOF
apiVersion: ax.io/v1alpha1
kind: Task
metadata:
  name: platform-agent
  atespace: default
spec:
  debug: true
  profile:
    ateActorTemplate: ${SHARED_TEMPLATE}
  gateway:
    name: kube-agents-cluster-gw
EOF

echo "==> 4. Creating ${COUNT} Cluster Agent Tasks (cluster-agent-001 .. cluster-agent-$(printf '%03d' "${COUNT}")) sharing ${SHARED_TEMPLATE} (concurrency=${CONCURRENCY})..."
START_TS="$(date +%s)"
seq 1 "${COUNT}" | xargs -P "${CONCURRENCY}" -I {} bash -ec '
  idx=$(printf "%03d" "$1")
  "$2" apply --context="$3" -f - >/dev/null <<EOF
apiVersion: ax.io/v1alpha1
kind: Task
metadata:
  name: cluster-agent-${idx}
  atespace: default
spec:
  debug: true
  profile:
    ateActorTemplate: $4
  gateway:
    name: kube-agents-cluster-gw
EOF
' _ {} "${AX_BIN}" "${CONTEXT}" "${SHARED_TEMPLATE}"
END_TS="$(date +%s)"
echo "    Created ${COUNT} Cluster Agent Tasks + Substrate Actors sharing ${SHARED_TEMPLATE} in $((END_TS - START_TS))s."
