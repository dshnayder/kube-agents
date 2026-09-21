#!/bin/bash
# Unattended entry point for the matrix on a machine with kubectl access to the install:
# keeps a port-forward to the agent API alive, exports the API token, and runs run_matrix.sh.
# Usage: run_all.sh <out-root> [reps] [parallel]
set -uo pipefail
export PATH=$HOME/bin:$PATH
readonly OUT_ROOT=${1:?out root}
readonly REPS=${2:-3}
readonly PARALLEL=${3:-3}
readonly HERE=$(cd "$(dirname "$0")" && pwd)
readonly CTX=${CTX:-csc-adc}
readonly NS=kubeagents-system
readonly LOCAL_PORT=${LOCAL_PORT:-18642}
readonly SERVICE=svc/platform-agent
readonly SERVICE_PORT=8642
readonly PF_RESTART_SECONDS=3
mkdir -p "$OUT_ROOT"
PLATFORM_AGENT_TOKEN=$(kubectl --context "$CTX" -n "$NS" get secret platform-agent-secrets -o jsonpath='{.data.API_SERVER_KEY}' | base64 -d)
export PLATFORM_AGENT_TOKEN
( while true; do kubectl --context "$CTX" -n "$NS" port-forward "$SERVICE" "$LOCAL_PORT:$SERVICE_PORT" >> "$OUT_ROOT/port-forward.log" 2>&1; sleep "$PF_RESTART_SECONDS"; done ) &
PF_LOOP=$!
trap 'kill $PF_LOOP 2>/dev/null; pkill -P $PF_LOOP 2>/dev/null' EXIT
sleep 5
CTX=$CTX BASE="http://127.0.0.1:$LOCAL_PORT" "$HERE/run_matrix.sh" "$OUT_ROOT" "$REPS" "$PARALLEL"
