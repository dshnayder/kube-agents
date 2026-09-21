#!/bin/bash
# Run the whole A/B matrix unattended: for each (rung, arm) switch the deployment, drive every
# scenario through run_ab.py, then copy the plugin's scoping record out of the pod.
#
# Usage: run_matrix.sh <out-root> [reps] [parallel]
# Env:   CTX (kube context), BASE (agent API base URL, e.g. http://127.0.0.1:8643),
#        PLATFORM_AGENT_TOKEN, SCENARIOS (path), MATRIX (space-separated "arm:rung" list)
set -euo pipefail
export PATH=$HOME/bin:$PATH

readonly OUT_ROOT=${1:?out root}
readonly REPS=${2:-3}
readonly PARALLEL=${3:-3}
readonly HERE=$(cd "$(dirname "$0")" && pwd)
readonly CTX=${CTX:-csc-adc}
readonly NS=kubeagents-system
readonly BASE=${BASE:-http://127.0.0.1:8643}
readonly SCENARIOS=${SCENARIOS:-$HERE/scenarios.json}
# Same framing on every arm: keeps a probe from turning into a fleet-wide sweep.
readonly PREFIX=${PREFIX:-"Work only on the cluster you run on and keep the investigation brief. "}
readonly GATEWAY_DEPLOY=${GATEWAY_DEPLOY:-platform-agent-gateway}
# Priority order: the two comparisons that decide the question first, the isolating arm after.
# scoped-skills is left out by default: under the API server the platform profile exposes 21
# tools, of which the tool filter hides one, so it would not differ from scoped-all.
readonly DEFAULT_MATRIX="stock:shipped scoped-all:shipped stock:grown scoped-all:grown fulldesc:shipped fulldesc:grown"
readonly MATRIX=${MATRIX:-$DEFAULT_MATRIX}

: "${PLATFORM_AGENT_TOKEN:?PLATFORM_AGENT_TOKEN}"
mkdir -p "$OUT_ROOT"
for cell in $MATRIX; do
  arm=${cell%%:*}; rung=${cell##*:}; label="$arm-$rung"
  echo "== $(date -u +%FT%TZ) cell $label"
  CTX=$CTX "$HERE/switch_arm.sh" "$arm" "$rung" 2>&1 | tee -a "$OUT_ROOT/switch.log"
  python3 "$HERE/run_ab.py" --base "$BASE" --token "$PLATFORM_AGENT_TOKEN" --scenarios "$SCENARIOS" \
    --out "$OUT_ROOT/$label" --label "$label" --reps "$REPS" --parallel "$PARALLEL" --prefix "$PREFIX" 2>&1 | tee -a "$OUT_ROOT/$label.log"
  pod=$(kubectl --context "$CTX" -n "$NS" get pod -o name | grep "$GATEWAY_DEPLOY" | head -1 | sed 's#pod/##')
  kubectl --context "$CTX" -n "$NS" exec "$pod" -c platform-agent -- cat "/opt/data/capability_scope-$label.jsonl" > "$OUT_ROOT/$label/capability_scope.jsonl" 2>/dev/null || echo "no record for $label"
  echo "== $(date -u +%FT%TZ) done $label ($(ls "$OUT_ROOT/$label"/*.json 2>/dev/null | wc -l) run files)"
done
echo "== $(date -u +%FT%TZ) MATRIX DONE"
