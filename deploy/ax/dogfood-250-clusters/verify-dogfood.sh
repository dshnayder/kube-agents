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
CONTEXT="${KUBECTL_CONTEXT:-kage-adc}"
AGENT_NAMESPACE="${AGENT_NAMESPACE:-kubeagents-system}"
AGENT_NAME="${AGENT_NAME:-platform-agent}"
AX_BIN="${AX_BIN:-$(command -v ax || echo "${HOME}/bin/ax")}"
SAMPLES=(
  "cluster-agent-001"
  "cluster-agent-125"
  "cluster-agent-250"
)

PF_PID=""
cleanup() {
  if [[ -n "${PF_PID}" ]] && kill -0 "${PF_PID}" >/dev/null 2>&1; then
    kill "${PF_PID}" >/dev/null 2>&1 || true
    wait "${PF_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

echo "==> 1. Verifying ax-system runs only stock ax-server + ax-postgres (zero k8s-api-proxy in ax-system)..."
kubectl --context="${CONTEXT}" -n ax-system get pods -o wide
if kubectl --context="${CONTEXT}" -n ax-system get deploy k8s-api-proxy >/dev/null 2>&1; then
  echo "FAIL: k8s-api-proxy must not exist in ax-system!" >&2
  exit 1
fi

echo "==> 2. Verifying ${AGENT_NAMESPACE} runs ${AGENT_NAME}-kube-proxy, ${AGENT_NAME}-credential-proxy, and ${AGENT_NAME}-a2a-gateway (BRIDGE_EXECUTOR=ax)..."
kubectl --context="${CONTEXT}" -n "${AGENT_NAMESPACE}" get deploy "${AGENT_NAME}-kube-proxy" "${AGENT_NAME}-credential-proxy" "${AGENT_NAME}-a2a-gateway"
kubectl --context="${CONTEXT}" -n "${AGENT_NAMESPACE}" get svc "${AGENT_NAME}-a2a-sandbox-broker"
BRIDGE_EXEC="$(kubectl --context="${CONTEXT}" -n "${AGENT_NAMESPACE}" get deploy "${AGENT_NAME}-a2a-gateway" \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="hermes-bridge")].env[?(@.name=="BRIDGE_EXECUTOR")].value}')"
if [[ "${BRIDGE_EXEC}" != "ax" ]]; then
  echo "FAIL: expected BRIDGE_EXECUTOR=ax on ${AGENT_NAME}-a2a-gateway/hermes-bridge, got ${BRIDGE_EXEC:-<empty>}" >&2
  exit 1
fi
echo "    OK: ${AGENT_NAME}-a2a-gateway/hermes-bridge configured with BRIDGE_EXECUTOR=${BRIDGE_EXEC}"

echo "==> 3. Resuming ${#SAMPLES[@]} stock Cluster Agents concurrently (${SAMPLES[*]}) via ax CLI..."
RESUME_START_MS="$(date +%s%3N)"
for agent in "${SAMPLES[@]}"; do
  (
    for _ in $(seq 1 20); do
      if "${AX_BIN}" resume task "${agent}" --context="${CONTEXT}" 2>/dev/null; then
        exit 0
      fi
      sleep 2
    done
    "${AX_BIN}" resume task "${agent}" --context="${CONTEXT}"
  ) &
done
wait
RESUME_END_MS="$(date +%s%3N)"
echo "    Resumed ${#SAMPLES[@]} Cluster Agents concurrently in $((RESUME_END_MS - RESUME_START_MS))ms."

for agent in "${SAMPLES[@]}"; do
  echo "--- ${agent} status ---"
  "${AX_BIN}" describe task "${agent}" --context="${CONTEXT}" | grep -E 'Name:|State:|Actor:|WorkspaceReady|Ready'
done

echo "==> 4. Verifying 1-to-1 paired Actor isolation (<name> -> ax-sandbox-ssh-proxy -> OIDC broker -> <name>-sandbox) and policy proxies..."
for agent in "${SAMPLES[@]}"; do
  echo "--- Testing paired isolation inside ${agent} ---"
  "${AX_BIN}" ssh "${agent}" --context="${CONTEXT}" -- env HERMES_DOCKER_EXEC_AS_ROOT=1 sh -ec '
    echo "[Agent Task] hermes=$(hermes --version 2>&1 | head -n1) | skills=$(ls /root/.hermes/skills | wc -l)"
    if command -v kubectl >/dev/null 2>&1 || command -v gcloud >/dev/null 2>&1; then
      echo "FAIL: kubectl/gcloud must not exist in agent task!" >&2
      exit 1
    fi
    if curl -m 2 -ks https://platform-agent-kube-proxy.kubeagents-system.svc:8443/version >/dev/null 2>&1; then
      echo "FAIL: agent task must not reach kube-proxy directly!" >&2
      exit 1
    fi
    AGENT_BOOT_ID="$(cat /proc/sys/kernel/random/boot_id)"
    ssh -T -p 2222 -i /etc/sandbox-ssh/id_ed25519 agent@127.0.0.1 "AGENT_BOOT_ID=${AGENT_BOOT_ID} sh -eu" << "SANDBOX_EOF"
      SANDBOX_BOOT_ID="$(cat /proc/sys/kernel/random/boot_id)"
      if [ "${SANDBOX_BOOT_ID}" = "${AGENT_BOOT_ID}" ]; then
        echo "FAIL: sandbox task must run in a distinct Substrate gVisor Actor!" >&2
        exit 1
      fi
      if [ "$(id -u)" != "1000" ] || [ -e /root/.hermes ] || command -v hermes >/dev/null 2>&1; then
        echo "FAIL: sandbox task must run as uid 1000 without /root/.hermes or hermes!" >&2
        exit 1
      fi
      echo "[Sandbox Task] uid=$(id -un) ($(id -u)) | distinct gVisor boot_id=${SANDBOX_BOOT_ID}"
      if curl -m 2 -s http://inference-gateway.kubeagents-system.svc.cluster.local/v1/models >/dev/null 2>&1; then
        echo "FAIL: sandbox task must not reach inference-gateway!" >&2
        exit 1
      fi
      if curl -m 2 -ks https://platform-agent-a2a-sandbox-broker.kubeagents-system.svc:8098/healthz >/dev/null 2>&1; then
        echo "FAIL: sandbox task must not reach a2a-sandbox-broker!" >&2
        exit 1
      fi
      if curl -m 2 -s http://169.254.169.254 >/dev/null 2>&1; then
        echo "FAIL: 169.254.169.254 must be unreachable from sandbox!" >&2
        exit 1
      fi
      test "$(cat /ax/token)" = "ax-placeholder"
      kubectl get nodes --no-headers | wc -l | xargs -I{} echo "[Sandbox Task] Visible cluster nodes via platform-agent-kube-proxy: {}"
      PW=$(kubectl get secret ax-postgres -n ax-system -o jsonpath="{.data.password}")
      echo "[Sandbox Task] Redacted Secret data.password=${PW}"
      test "${PW}" = "UkVEQUNURUQ="
SANDBOX_EOF
  '
done

echo "==> 5. Suspending ${SAMPLES[*]} and their paired *-sandbox tasks..."
for agent in "${SAMPLES[@]}"; do
  "${AX_BIN}" suspend task "${agent}" --context="${CONTEXT}"
  "${AX_BIN}" suspend task "${agent}-sandbox" --context="${CONTEXT}"
done

if kubectl --context="${CONTEXT}" -n "${AGENT_NAMESPACE}" get secret "${AGENT_NAME}-a2a-inject" >/dev/null 2>&1; then
  echo "==> 6. Driving @cluster-agent-001 turn through ${AGENT_NAME}-a2a-gateway inject door..."
  INJECT_TOKEN="$(kubectl --context="${CONTEXT}" -n "${AGENT_NAMESPACE}" get secret "${AGENT_NAME}-a2a-inject" -o jsonpath='{.data.token}' | base64 -d)"
  LEADER_POD="$(kubectl --context="${CONTEXT}" -n "${AGENT_NAMESPACE}" get lease "${AGENT_NAME}-a2a-gateway" -o jsonpath='{.spec.holderIdentity}')"
  LOCAL_PORT="${INJECT_LOCAL_PORT:-18099}"
  kubectl --context="${CONTEXT}" -n "${AGENT_NAMESPACE}" port-forward "pod/${LEADER_POD}" "${LOCAL_PORT}:8099" >/dev/null 2>&1 &
  PF_PID="$!"
  trap 'kill "${PF_PID}" 2>/dev/null || true' EXIT
  sleep 2

  CONV_KEY="ax-dogfood-$(date +%s)"
  INJECT_RESP="$(curl -fsS -X POST "http://127.0.0.1:${LOCAL_PORT}/inject" \
    -H "Authorization: Bearer ${INJECT_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"conversation\":\"${CONV_KEY}\",\"author\":\"devops-bench\",\"text\":\"@cluster-agent-001 Run kubectl get pods -n ax-system and summarize their status in one concise sentence.\"}")"
  echo "    POST /inject accepted: ${INJECT_RESP}"
  RESOLVED_CONV="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("conversation",""))' <<< "${INJECT_RESP}")"

  DEADLINE=$((SECONDS + 300))
  ANSWER=""
  while (( SECONDS < DEADLINE )); do
    CONV_STATE="$(curl -fsS "http://127.0.0.1:${LOCAL_PORT}/conversations/${RESOLVED_CONV}?probe=1" \
      -H "Authorization: Bearer ${INJECT_TOKEN}")"
    ANSWER="$(python3 -c '
import json, sys
doc = json.loads(sys.stdin.read())
entries = doc.get("entries") or []
terms = [e for e in entries if e.get("kind") == "terminal"]
if terms:
    t = terms[-1]
    state = t.get("state", "")
    reason = t.get("reason", "")
    if state != "completed":
        sys.stderr.write(f"Turn ended {state}: {reason}\n")
        sys.exit(2)
    posts = [e.get("text", "") for e in entries if e.get("kind") in ("post", "edit") and e.get("text")]
    replies = [p for p in posts if not p.startswith(("⏳", "✅"))]
    print(replies[-1] if replies else (posts[-1] if posts else "completed"))
' <<< "${CONV_STATE}")"
    if [[ -n "${ANSWER}" ]]; then
      break
    fi
    sleep 2
  done
  if [[ -z "${ANSWER}" ]]; then
    echo "FAIL: timed out waiting for ${AGENT_NAME}-a2a-gateway inject reply on ${CONV_KEY}" >&2
    exit 1
  fi
  echo "    Inject reply: ${ANSWER}"
fi

echo "==> 7. Verifying both cluster-agent-001 and cluster-agent-001-sandbox returned to TASK_STATE_SUSPENDED..."
"${AX_BIN}" describe task cluster-agent-001 --context="${CONTEXT}" | grep -E 'Name:|State:|Actor:'
"${AX_BIN}" describe task cluster-agent-001-sandbox --context="${CONTEXT}" | grep -E 'Name:|State:|Actor:'
