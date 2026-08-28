#!/usr/bin/env bash
# Run the remaining read rungs from the cloudtop, so a closed laptop does not
# kill them. The harness talks to the agent's API over a port-forward it starts
# itself: the laptop's forward dies with the laptop, and a probe that loses it
# scores as the access design failing rather than as the network it was.
set -euo pipefail
cd "$(dirname "$0")"
ARM="${1:?usage: run_remote.sh A|B|C}"
CTX=gke_YOUR-PROJECT_YOUR-REGION_YOUR-CLUSTER
NS=kubeagents-system

kubectl --context=$CTX -n $NS port-forward deploy/platform-agent-gateway 8642:8643 \
  > pf-remote.log 2>&1 &
PF=$!
trap 'kill $PF 2>/dev/null || true' EXIT
for _ in $(seq 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8642/v1/responses || true)
  [ "$code" = "401" ] && break
  sleep 2
done
[ "$code" = "401" ] || { echo "the gateway API never came up on 8642 (last=$code)" >&2; exit 1; }
echo "port-forward up"

bash run_reads.sh "$ARM"
