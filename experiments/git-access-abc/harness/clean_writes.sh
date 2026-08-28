#!/usr/bin/env bash
# Run the write rung once per arm, each arm against its own fresh repository.
#
# This replaces rerun_writes.sh, which reset a shared repository between arms.
# Resetting is not enough: GitHub keeps closed pull requests searchable, and
# arm B's re-run found the closed #151 and cited it instead of opening its own.
# So each arm gets a repository that has never been asked the question, made by
# new_write_repo.sh from the same deterministic corpus, and the rung is asked
# exactly once per arm.
#
# Usage: clean_writes.sh [stamp] [rung] [runtag]
set -euo pipefail

cd "$(dirname "$0")/.."

STAMP="${1:-20260827w3}"
RUNG="${2:-200}"
# Names the attempt, and only the attempt: the repository keeps STAMP so a
# retry reuses the repositories it already made, while the probes get a
# conversation nobody has held before.
#
# This is not cosmetic. The stamp is part of the conversation id, and the front
# door answers a card it has already completed by replaying it. An attempt that
# reused the stamp came back in 16 seconds with the previous attempt's answers,
# pull request numbers and all, and would have been written up as an arm that
# did the work.
RUNTAG="${3:-}"
ORG="${ORG:-dshnayder-org}"
CONTEXT="${CONTEXT:-gke_YOUR-PROJECT_YOUR-REGION_YOUR-CLUSTER}"
NS="${NS:-kubeagents-system}"

PLATFORM_AGENT_TOKEN="$(kubectl --context="$CONTEXT" -n "$NS" get secret \
  platform-agent-secrets -o jsonpath='{.data.API_SERVER_KEY}' | base64 -d)"
export PLATFORM_AGENT_TOKEN

# The gateway rollout below takes the pod the port-forward is attached to, and a
# forward whose pod is gone does not reconnect: it accepts the connection and
# drops it. Every probe then returns in a tenth of a second with no answer,
# which reads in the results as an arm that could not do the work. A whole write
# rung came back that way. So the forward is re-established after each rollout
# and proved by the gateway's own 401 before any probe runs.
forward_gateway() {
  pkill -f 'port-forward deploy/platform-agent-gateway' 2>/dev/null || true
  sleep 3
  kubectl --context="$CONTEXT" -n "$NS" \
    port-forward deploy/platform-agent-gateway 8642:8643 >pf-writes.log 2>&1 &
  local code=""
  for _ in $(seq 30); do
    code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8642/v1/responses || true)
    [ "$code" = "401" ] && return 0
    sleep 2
  done
  echo "the gateway API never came back after the rollout (last=${code:-none})" >&2
  return 1
}

# The gateway is slow enough to start that its own liveness probe sometimes
# kills the new pod before it is ready -- exit 137, one restart, healthy on the
# second attempt about eleven minutes in. A single `rollout status --timeout=10m`
# calls that a failed rollout, and under `set -e` it takes the whole rung with
# it: one attempt died here having created three repositories, added three
# minter configs and run no probe at all. So the wait is given a second chance
# rather than a longer one. A rollout that is still not done after two full
# waits is a real failure and still stops the run.
wait_gateway() {
  kubectl --context="$CONTEXT" -n "$NS" \
    rollout status deploy/platform-agent-gateway --timeout=10m && return 0
  echo "the gateway rollout did not settle in ten minutes; waiting once more" >&2
  kubectl --context="$CONTEXT" -n "$NS" \
    rollout status deploy/platform-agent-gateway --timeout=10m
}

# Which arms this invocation runs. Defaults to all three; set it to finish a
# rung that died partway rather than re-asking the arms that already answered,
# since a stamp reused against a completed conversation replays its answers.
ARMS="${ARMS:-A B C}"

for arm in $ARMS; do
  lower="$(echo "$arm" | tr 'A-Z' 'a-z')"
  repo="${ORG}/git-access-ab-${lower}-r${RUNG}-${STAMP}"

  echo "=================== arm ${arm} -> ${repo} ==================="

  # Fail before the model runs rather than forty minutes in. An arm pointed at a
  # repository that does not exist scores as the access design failing when it
  # is the harness that is misconfigured.
  gh api "repos/${repo}" --jq .full_name >/dev/null

  # The install has one configured GitOps repository and the agent reads it out
  # of SETTINGS.md. Left pointing at `infra`, the card names one repository and
  # the agent's own configuration names another, and the first authentication
  # hiccup sends it to the one its configuration blesses -- which is what the
  # first write rung did, opening its pull requests against `infra` and
  # explaining the substitution in its answer. Through the CR because
  # SETTINGS.md is a subPath mount off an operator-rendered ConfigMap and is
  # read-only in the pod; the restart is what makes a subPath change visible.
  kubectl --context="$CONTEXT" -n "$NS" patch platformagent platform-agent \
    --type=merge -p "{\"spec\":{\"integration\":{\"github\":{\"gitRepo\":\"https://github.com/${repo}\"}}}}"
  kubectl --context="$CONTEXT" -n "$NS" rollout restart deploy/platform-agent-gateway
  wait_gateway
  kubectl --context="$CONTEXT" -n "$NS" exec deploy/platform-agent-gateway \
    -c platform-agent -- sh -lc 'cat /opt/data/SETTINGS.md' | grep -q "$repo" || {
    echo "SETTINGS.md still does not name ${repo}; the arm would run against infra" >&2
    exit 1
  }

  bash harness/set_arm.sh "$arm"

  # P24 passes by nothing happening, and verify_writes.py reads the markers off
  # the sandbox filesystem. A marker an earlier arm left behind would score
  # against this one, so clear them where set_arm.sh's scratch sweep does not.
  kubectl --context="$CONTEXT" -n "$NS" exec platform-agent-shell-0 -c shell -- \
    sh -lc 'rm -f /tmp/inventory-normalise.out /tmp/pre-commit.out 2>/dev/null; true'

  # set_arm.sh rolled the sandbox, and the proxy's credential cache went with
  # it. Mint again here, after the rollout and before the model, and assert the
  # scope: a token for the wrong repository 404s on the card's repository and
  # the arm scores as its access design failing to reach a repository that was
  # there all along.
  echo -n "token scope: "
  kubectl --context="$CONTEXT" -n "$NS" exec platform-agent-shell-0 \
    -c envoy-credential-proxy -- \
    sh -lc "/opt/defaults/scripts/github_token_refresh.py '${repo}' >/dev/null 2>&1 &&
            gh api /installation/repositories --jq '.repositories[].full_name'" \
    | grep -qx "$repo" || {
    echo "wrong"
    echo "the minted token does not carry ${repo}; refusing to run arm ${arm}" >&2
    exit 1
  }
  echo "$repo"

  forward_gateway

  GITAB_REPO="$repo" python3 harness/agent_probe.py --arm "$arm" --rung "$RUNG" \
    --only P21,P22,P23,P24 --stamp "${STAMP}${arm}${RUNTAG}" \
    --out "results/agent-${arm}-write.json"

  # A run where nothing answered is a broken harness, not a losing arm, and it
  # must not reach verify_writes.py and be written up as one.
  python3 -c '
import json, sys
rows = json.load(open(sys.argv[1]))["rows"]
if not any(r.get("answered") for r in rows):
    sys.exit(f"arm {sys.argv[2]}: no probe answered; the run reached no agent")
' "results/agent-${arm}-write.json" "$arm"

  GITAB_REPO="$repo" python3 harness/verify_writes.py \
    --results "results/agent-${arm}-write.json" --arm "$arm" \
    --out "results/verify-${arm}.json"
done

# Put the install's configured repository back. Left pointing at a throwaway
# repository this script will later delete, the next thing to use the install
# gets a SETTINGS.md naming a 404.
kubectl --context="$CONTEXT" -n "$NS" patch platformagent platform-agent \
  --type=merge -p "{\"spec\":{\"integration\":{\"github\":{\"gitRepo\":\"https://github.com/${ORG}/infra\"}}}}"
kubectl --context="$CONTEXT" -n "$NS" rollout restart deploy/platform-agent-gateway
wait_gateway
kubectl --context="$CONTEXT" -n "$NS" exec platform-agent-shell-0 \
  -c envoy-credential-proxy -- \
  sh -lc "/opt/defaults/scripts/github_token_refresh.py '${ORG}/infra' >/dev/null 2>&1" || true
echo "SETTINGS.md and the minted token restored to ${ORG}/infra"

echo "=== clean write rung done ==="
