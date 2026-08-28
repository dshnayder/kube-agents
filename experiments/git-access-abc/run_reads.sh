#!/usr/bin/env bash
# Re-run the read rungs for one arm at full probe width on the current image.
set -euo pipefail
cd "$(dirname "$0")"
ARM="${1:?usage: run_reads.sh A|B}"
K="kubectl --context=gke_YOUR-PROJECT_YOUR-REGION_YOUR-CLUSTER -n kubeagents-system"
PLATFORM_AGENT_TOKEN="$($K get secret platform-agent-secrets -o jsonpath='{.data.API_SERVER_KEY}' | base64 -d)"
export PLATFORM_AGENT_TOKEN
READ=$(python3 -c "print(','.join('P%02d'%i for i in range(1,21)))")
bash harness/set_arm.sh "$ARM"
for rung in 200 3000 10000; do
  echo "=== ${ARM} r${rung} ==="
  python3 harness/agent_probe.py --arm "$ARM" --rung "$rung" --stamp "e${rung}" \
    --workers 4 --only "$READ" --out "results/agent-${ARM}-r${rung}.json"
done
echo "=== ${ARM} reads done ==="
