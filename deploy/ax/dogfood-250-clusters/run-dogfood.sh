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
AGENT_NAMESPACE="${AGENT_NAMESPACE:-kubeagents-system}"
AGENT_NAME="${AGENT_NAME:-platform-agent}"
IMAGE="${AX_KUBE_AGENT_IMAGE:?set AX_KUBE_AGENT_IMAGE to the built ax-kube-agent digest}"
SANDBOX_IMAGE="${AX_KUBE_SANDBOX_IMAGE:?set AX_KUBE_SANDBOX_IMAGE to the built ax-kube-sandbox digest}"
TASK_CPU="${AX_TASK_CPU:-500m}"
TASK_MEMORY="${AX_TASK_MEMORY:-1Gi}"
BROKER_URL="https://${AGENT_NAME}-a2a-sandbox-broker.${AGENT_NAMESPACE}.svc:8098/v1/sandbox/exec"
KUBE_PROXY_URL="https://${AGENT_NAME}-kube-proxy.${AGENT_NAMESPACE}.svc:8443"
CREDENTIAL_PROXY_URL="https://${AGENT_NAME}-credential-proxy.${AGENT_NAMESPACE}.svc:8767"

echo "==> 1. Applying stock AX Gateways (kube-agents-agent-gw + kube-agents-sandbox-gw) on context ${CONTEXT}..."
"${AX_BIN}" apply -f "${SCRIPT_DIR}/gateway.yaml" --context="${CONTEXT}"

echo "==> 2. Ensuring paired ${AGENT_NAME} + ${AGENT_NAME}-sandbox stock AX Tasks..."
if ! "${AX_BIN}" describe task "${AGENT_NAME}" --context="${CONTEXT}" >/dev/null 2>&1; then
  "${AX_BIN}" apply --context="${CONTEXT}" -f - <<EOF
apiVersion: ax.io/v1alpha1
kind: Task
metadata:
  name: ${AGENT_NAME}
  atespace: default
spec:
  debug: true
  profile:
    image: ${IMAGE}
    resources:
      limits:
        cpu: "${TASK_CPU}"
        memory: "${TASK_MEMORY}"
  gateway:
    name: kube-agents-agent-gw
  commands:
    - /usr/local/bin/ax-sandbox-ssh-proxy
  env:
    - name: SANDBOX_BROKER_URL
      value: ${BROKER_URL}
EOF
fi
if ! "${AX_BIN}" describe task "${AGENT_NAME}-sandbox" --context="${CONTEXT}" >/dev/null 2>&1; then
  "${AX_BIN}" apply --context="${CONTEXT}" -f - <<EOF
apiVersion: ax.io/v1alpha1
kind: Task
metadata:
  name: ${AGENT_NAME}-sandbox
  atespace: default
spec:
  debug: true
  profile:
    image: ${SANDBOX_IMAGE}
    resources:
      limits:
        cpu: "${TASK_CPU}"
        memory: "${TASK_MEMORY}"
  gateway:
    name: kube-agents-sandbox-gw
  env:
    - name: KUBE_PROXY_URL
      value: ${KUBE_PROXY_URL}
    - name: CREDENTIAL_PROXY_URL
      value: ${CREDENTIAL_PROXY_URL}
EOF
fi

echo "==> 3. Creating ${COUNT} stock Cluster Agent Tasks (cluster-agent-001 .. cluster-agent-$(printf '%03d' "${COUNT}")) (concurrency=${CONCURRENCY}, cpu=${TASK_CPU}, memory=${TASK_MEMORY}; paired *-sandbox tasks wake lazily on first CLI call)..."
START_TS="$(date +%s)"
seq 1 "${COUNT}" | xargs -P "${CONCURRENCY}" -I {} bash -ec '
  idx=$(printf "%03d" "$1")
  if "$2" describe task "cluster-agent-${idx}" --context="$3" >/dev/null 2>&1; then
    exit 0
  fi
  "$2" apply --context="$3" -f - >/dev/null <<EOF
apiVersion: ax.io/v1alpha1
kind: Task
metadata:
  name: cluster-agent-${idx}
  atespace: default
spec:
  debug: true
  profile:
    image: $4
    resources:
      limits:
        cpu: "$6"
        memory: "$7"
  gateway:
    name: kube-agents-agent-gw
  commands:
    - /usr/local/bin/ax-sandbox-ssh-proxy
  env:
    - name: SANDBOX_BROKER_URL
      value: $5
EOF
' _ {} "${AX_BIN}" "${CONTEXT}" "${IMAGE}" "${BROKER_URL}" "${TASK_CPU}" "${TASK_MEMORY}"
END_TS="$(date +%s)"
echo "    Created ${COUNT} stock Cluster Agent Tasks in $((END_TS - START_TS))s."
