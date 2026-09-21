#!/bin/bash
# Put the deployed Platform Agent into one arm of the capability-scoping A/B and wait for it.
#
# Usage: switch_arm.sh <arm> <rung>
#   arm:  stock | fulldesc | scoped-skills | scoped-all
#   rung: shipped | grown
#
# The arm is a set of environment variables on the PlatformAgent CR (spec.deployment.env), read
# by the capability_scope plugin and by the build-time patch in the image. The rung adds the
# distractor skills directory (already copied onto the agent's data volume) to the catalogue.
# Every switch rolls the gateway, and the skills-index render is rebuilt because the patch keys
# its cache on these variables and skips the disk snapshot while they are set.
set -euo pipefail
export PATH=$HOME/bin:$PATH

readonly ARM=${1:?arm}
readonly RUNG=${2:?rung}
readonly CTX=${CTX:-csc-adc}
readonly NS=kubeagents-system
readonly CR=${CR:-platform-agent}
readonly DISTRACTOR_DIR=/opt/data/distractor-skills
readonly DESC_UNLIMITED=100000
readonly K_SKILLS=${K_SKILLS:-6}
readonly N_TOOLS=${N_TOOLS:-6}
readonly MAX_ITERATIONS=${MAX_ITERATIONS:-8}
readonly ROLLOUT_TIMEOUT=600s
readonly GATEWAY_DEPLOY=${GATEWAY_DEPLOY:-platform-agent-gateway}
readonly SETTLE_SECONDS=45

index_mode=""; desc_limit=""; scope_mode="off"
case "$ARM" in
  stock)          ;;
  fulldesc)       desc_limit=$DESC_UNLIMITED ;;
  scoped-skills)  index_mode=names; desc_limit=$DESC_UNLIMITED; scope_mode=skills ;;
  scoped-all)     index_mode=names; desc_limit=$DESC_UNLIMITED; scope_mode=skills+tools ;;
  *) echo "unknown arm $ARM" >&2; exit 2 ;;
esac
extra_dirs=""
case "$RUNG" in
  shipped) ;;
  grown)   extra_dirs=$DISTRACTOR_DIR ;;
  *) echo "unknown rung $RUNG" >&2; exit 2 ;;
esac

env_json=$(python3 - "$index_mode" "$desc_limit" "$scope_mode" "$extra_dirs" "$K_SKILLS" "$N_TOOLS" "$MAX_ITERATIONS" "$ARM-$RUNG" <<'EOF'
import json, sys
index_mode, desc_limit, scope_mode, extra_dirs, k, n, max_it, label = sys.argv[1:]
env = [
    {"name": "KA_SKILLS_INDEX_MODE", "value": index_mode},
    {"name": "KA_SKILL_DESC_LIMIT", "value": desc_limit},
    {"name": "KA_SCOPE_MODE", "value": scope_mode},
    {"name": "KA_EXTRA_SKILLS_DIRS", "value": extra_dirs},
    {"name": "KA_SCOPE_K_SKILLS", "value": k},
    {"name": "KA_SCOPE_N_TOOLS", "value": n},
    {"name": "KA_SCOPE_RECORD", "value": "/opt/data/capability_scope-%s.jsonl" % label},
    {"name": "HERMES_MAX_ITERATIONS", "value": max_it},
]
print(json.dumps({"spec": {"deployment": {"env": env}}}))
EOF
)
kubectl --context "$CTX" -n "$NS" patch platformagent "$CR" --type merge -p "$env_json"
sleep 5
kubectl --context "$CTX" -n "$NS" rollout status "deploy/$GATEWAY_DEPLOY" --timeout="$ROLLOUT_TIMEOUT"
sleep "$SETTLE_SECONDS"
pod=$(kubectl --context "$CTX" -n "$NS" get pod -l app.kubernetes.io/name="$GATEWAY_DEPLOY" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
[ -n "$pod" ] || pod=$(kubectl --context "$CTX" -n "$NS" get pod -o name | grep "$GATEWAY_DEPLOY" | head -1 | sed 's#pod/##')
echo "arm=$ARM rung=$RUNG pod=$pod"
kubectl --context "$CTX" -n "$NS" exec "$pod" -c platform-agent -- env 2>/dev/null | grep -E '^KA_|^HERMES_MAX_ITERATIONS' || true
