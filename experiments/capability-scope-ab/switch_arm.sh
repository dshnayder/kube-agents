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
readonly CONTROL_FILE=/opt/data/capability_scope.env

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

pod=$(kubectl --context "$CTX" -n "$NS" get pod -o name | grep "$GATEWAY_DEPLOY" | grep -v Terminating | head -1 | sed 's#pod/##')
label="$ARM-$RUNG"
# The operator does not pass spec.deployment.env to the gateway container, so the arm lives in a
# control file on the data volume that the plugin loads into the environment at start-up.
kubectl --context "$CTX" -n "$NS" exec "$pod" -c platform-agent -- sh -c "cat > $CONTROL_FILE" <<CTRL
KA_SKILLS_INDEX_MODE=$index_mode
KA_SKILL_DESC_LIMIT=$desc_limit
KA_SCOPE_MODE=$scope_mode
KA_EXTRA_SKILLS_DIRS=$extra_dirs
KA_SCOPE_K_SKILLS=$K_SKILLS
KA_SCOPE_N_TOOLS=$N_TOOLS
KA_SCOPE_RECORD=/opt/data/capability_scope-$label.jsonl
HERMES_MAX_ITERATIONS=$MAX_ITERATIONS
CTRL
kubectl --context "$CTX" -n "$NS" rollout restart "deploy/$GATEWAY_DEPLOY"
sleep 5
kubectl --context "$CTX" -n "$NS" rollout status "deploy/$GATEWAY_DEPLOY" --timeout="$ROLLOUT_TIMEOUT"
sleep "$SETTLE_SECONDS"
pod=$(kubectl --context "$CTX" -n "$NS" get pod -o name | grep "$GATEWAY_DEPLOY" | grep -v Terminating | head -1 | sed 's#pod/##')
echo "arm=$ARM rung=$RUNG pod=$pod"
kubectl --context "$CTX" -n "$NS" exec "$pod" -c platform-agent -- cat "$CONTROL_FILE"
