#!/bin/bash
# Unattended entry point for the matrix on a machine with kubectl access to the install:
# keeps a port-forward to the agent API alive, exports the API token, and runs run_matrix.sh.
# Usage: run_all.sh <out-root> [reps] [parallel]
set -uo pipefail
export PATH=$HOME/bin:$PATH
readonly OUT_ROOT=${1:?out root}
readonly REPS=${2:-3}
readonly PARALLEL=${3:-3}
HERE=$(cd "$(dirname "$0")" && pwd); readonly HERE
CTX=${CTX:?kube context of the install}; export CTX
readonly NS=kubeagents-system
readonly LOCAL_PORT=${LOCAL_PORT:-18642}
# Straight to the Hermes API server on the gateway pod's loopback, not through the Service: the
# Service fronts the credential proxy's authenticated door, which drops any turn longer than
# 300 seconds with an HTML 502 and no session id. Forwarding to the Deployment re-resolves the
# pod after every arm switch. The key is the gateway container's own API_SERVER_KEY.
readonly TARGET=deploy/platform-agent-gateway
readonly TARGET_PORT=8642
readonly PF_RESTART_SECONDS=3
readonly PF_WARMUP_SECONDS=5
mkdir -p "$OUT_ROOT"
gateway_pod=$(kubectl --context "$CTX" -n "$NS" get pod -o name | grep platform-agent-gateway | grep -v Terminating | head -1 | sed 's#pod/##')
PLATFORM_AGENT_TOKEN=$(kubectl --context "$CTX" -n "$NS" exec "$gateway_pod" -c platform-agent -- sh -c 'env | grep ^API_SERVER_KEY= | cut -d= -f2-')
export PLATFORM_AGENT_TOKEN
( while true; do kubectl --context "$CTX" -n "$NS" port-forward "$TARGET" "$LOCAL_PORT:$TARGET_PORT" >> "$OUT_ROOT/port-forward.log" 2>&1; sleep "$PF_RESTART_SECONDS"; done ) &
PF_LOOP=$!
trap 'kill $PF_LOOP 2>/dev/null; pkill -P $PF_LOOP 2>/dev/null' EXIT
sleep "$PF_WARMUP_SECONDS"
BASE="http://127.0.0.1:$LOCAL_PORT"; export BASE
"$HERE/run_matrix.sh" "$OUT_ROOT" "$REPS" "$PARALLEL"
