#!/usr/bin/env bash
# Re-measure the write rung for arms B and C, each against a clean repository.
#
# The first pass ran A, then B, then C without resetting between them, so only
# arm A ever met the question the probe asks. reset_writes.sh explains what that
# did to the scores. Arm A's numbers stand -- it ran first, on a clean repo --
# so only B and C are re-run here, and each gets its own reset immediately
# before it.
set -euo pipefail

cd "$(dirname "$0")/.."

CONTEXT="${CONTEXT:-gke_YOUR-PROJECT_YOUR-REGION_YOUR-CLUSTER}"
NS="${NS:-kubeagents-system}"
STAMP="${STAMP:-20260827w2}"

PLATFORM_AGENT_TOKEN="$(kubectl --context="$CONTEXT" -n "$NS" get secret \
  platform-agent-secrets -o jsonpath='{.data.API_SERVER_KEY}' | base64 -d)"
export PLATFORM_AGENT_TOKEN

for arm in B C; do
  echo "=================== arm ${arm} ==================="
  bash harness/reset_writes.sh 200
  bash harness/set_arm.sh "$arm"
  python3 harness/agent_probe.py --arm "$arm" --rung 200 \
    --only P21,P22,P23,P24 --stamp "${STAMP}${arm}" \
    --out "results/agent-${arm}-write.json"
  python3 harness/verify_writes.py \
    --results "results/agent-${arm}-write.json" --arm "$arm" \
    --out "results/verify-${arm}.json"
done

echo "=== write re-run done ==="
