#!/usr/bin/env bash
# Switch the experiment between arms.
#
# Arms A and B differ by one field on the PlatformAgent. With
# contentWorkspaces true the credential proxy serves /v1/workspace/* and the
# inspect-repository skill speaks the content verbs (arm B); with it false
# those routes 404 and the skill falls back to a leased checkout on the volume
# the sandbox shares with the proxy, driven by real git and gh (arm A). The
# prompts are byte-identical across the two, which is what makes them
# comparable.
#
# Arm C is not a flag flip and the write-up has to say so. A different access
# design means a different sanctioned door, so the switch is a skill swap:
# version-control is put in front of the agent and inspect-repository is taken
# away. Its routes are a separate family, /v1/vcs/*, armed by its own field on
# the CR and read by the broker as CREDENTIAL_PROXY_VCS.
#
# C nonetheless runs with contentWorkspaces true, which is deliberate and not
# leftover. Leaving arm B's routes serving means C is measured with every other
# door still open -- the workspace verbs and `gh api` both reachable -- so a run
# that never leaves /v1/vcs/* is evidence about what the agent chose, not about
# what it was left with. Closing the doors first would produce the same number
# by construction and prove nothing.
#
# Parking a skill moves the whole directory out of the catalogue rather than
# renaming it in place. The agent enumerates that directory, so a skill renamed
# to something that looks disabled is still a skill it can find and read, and
# an arm whose "unavailable" route is one `ls` away measures nothing.
#
# Usage: set_arm.sh A|B|C
set -euo pipefail

ARM="${1:?usage: set_arm.sh A|B|C}"
CONTEXT="${CONTEXT:-gke_YOUR-PROJECT_YOUR-REGION_YOUR-CLUSTER}"
NS="${NS:-kubeagents-system}"
K="kubectl --context=${CONTEXT} -n ${NS}"

SKILLS=/opt/data/profiles/platform/skills
PARKED=/opt/data/scratch/arm-parked

# SEAL=1 runs arm C in the configuration it would actually ship in rather than
# the configuration that makes its route choice observable. Two differences,
# both of them doors this arm never used and therefore doors whose removal
# should change nothing:
#
#   - contentWorkspaces goes false, so arm B's routes 404 instead of serving.
#   - /opt/credential-proxy/bin/gh, the credentialed shim, is replaced by the
#     same refusing stub that owns the name on PATH. Unsealed, the stub shadows
#     the shim and the shim is still one absolute path away; sealed, there is
#     no spelling of `gh` that reaches GitHub.
#
# This exists because the unsealed arm answers "what did the agent prefer with
# everything open" and the question that decides the design is "how well does
# it do with only this". Those are the same number only if the preference was
# total, and on the write rung it was not: one probe in four touched `gh`.
SEAL="${SEAL:-0}"

case "$ARM" in
  A) WANT=false; VCS=false; SHOW=inspect-repository; PARK=version-control ;;
  B) WANT=true;  VCS=false; SHOW=inspect-repository; PARK=version-control ;;
  C) WANT=true;  VCS=true;  SHOW=version-control;    PARK=inspect-repository ;;
  *) echo "arm must be A, B or C" >&2; exit 2 ;;
esac

if [ "$SEAL" = "1" ]; then
  [ "$ARM" = "C" ] || { echo "SEAL=1 is only defined for arm C" >&2; exit 2; }
  WANT=false
fi

# Both fields, every time. Parking the skill hides the documentation for a
# route; it does not close the route, and an arm that runs after C with
# /v1/vcs/* still serving is not the arm it says it is. This matters for a
# re-run rather than for the first pass, where C simply went last.
CURRENT=$($K get platformagent platform-agent -o jsonpath=\
'{.spec.harness.experimental.shellSandbox.contentWorkspaces}/{.spec.harness.experimental.shellSandbox.versionControl}')

if [ "$CURRENT" = "${WANT}/${VCS}" ]; then
  echo "contentWorkspaces/versionControl already ${WANT}/${VCS}"
else
  $K patch platformagent platform-agent --type=merge \
    -p "{\"spec\":{\"harness\":{\"experimental\":{\"shellSandbox\":{\"contentWorkspaces\":${WANT},\"versionControl\":${VCS}}}}}}"
  # The operator rewrites the sandbox pod spec, so wait on the workload rather
  # than on the patch returning.
  $K rollout status statefulset/platform-agent-shell --timeout=10m
fi

$K wait --for=condition=Ready pod/platform-agent-shell-0 --timeout=5m

# Prove the arm rather than trust the field: the proxy's own environment is
# what decides whether the routes exist.
LIVE=$($K exec platform-agent-shell-0 -c envoy-credential-proxy -- \
  sh -lc 'echo "${CREDENTIAL_PROXY_CONTENT_WORKSPACES:-unset}/${CREDENTIAL_PROXY_VCS:-unset}"')
echo "arm=${ARM} contentWorkspaces=${WANT} versionControl=${VCS} proxy_env=${LIVE}"

# Arm C's routes come from a different field, so the contentWorkspaces check
# above says nothing about them. Without this an unarmed broker 404s every vcs
# call and the run scores as the design failing.
if [ "$ARM" = "C" ] && [ "${LIVE#*/}" != "1" ]; then
  echo "arm C needs CREDENTIAL_PROXY_VCS=1 on the broker; it is ${LIVE#*/}" >&2
  exit 1
fi
if [ "$ARM" != "C" ] && [ "${LIVE#*/}" = "1" ]; then
  echo "arm ${ARM} must not see the vcs routes; the broker still has them" >&2
  exit 1
fi

# The skill swap, asserted for every arm rather than only for C. A and B each
# state that version-control is not in front of the agent, so a run that
# follows a C run cannot inherit its door.
$K exec deploy/platform-agent-gateway -c platform-agent -- sh -lc "
  set -e
  mkdir -p '${PARKED}'
  if [ -d '${SKILLS}/${PARK}' ]; then
    rm -rf '${PARKED}/${PARK}'
    mv '${SKILLS}/${PARK}' '${PARKED}/${PARK}'
  fi
  if [ ! -d '${SKILLS}/${SHOW}' ]; then
    if [ -d '${PARKED}/${SHOW}' ]; then
      mv '${PARKED}/${SHOW}' '${SKILLS}/${SHOW}'
    else
      echo \"MISSING: ${SHOW} is in neither the catalogue nor the park\" >&2
      exit 1
    fi
  fi
"
echo -n "skills: "
$K exec deploy/platform-agent-gateway -c platform-agent -- \
  sh -lc "ls ${SKILLS} | grep -E '^(inspect-repository|version-control)$' | tr '\n' ' '; echo"

# Arm C's read path needs the sandbox's own git, which lives off PATH so the
# credential shim keeps the bare name. An image without it makes every C probe
# fail as a capability miss, which is not the thing being measured.
if [ "$ARM" = "C" ]; then
  $K exec platform-agent-shell-0 -c shell -- \
    sh -lc '/opt/vcs/libexec/git --version' \
    || { echo "arm C needs a sandbox image carrying /opt/vcs/libexec/git" >&2; exit 1; }

  # The binary existing is not the arm. What makes `git` mean the local one is
  # the PATH prepend, and it is delivered twice: /etc/profile.d/vcs-path.sh for
  # a login shell, and the sshd SetEnv line for the non-login session Hermes
  # actually gets. The first build had only the profile.d half, so every probe
  # ran with `git` still resolving to the credential shim while the arm
  # reported itself armed -- a whole set of read rungs measured the wrong
  # thing and had to be thrown away. Assert the half that was missing, from
  # the file that carries it, because a `kubectl exec -- sh -lc` here is a
  # login shell and would pass on the broken image.
  DROPIN=$($K exec platform-agent-shell-0 -c shell -- \
    sh -lc 'cat /etc/ssh/sshd_config.d/10-sandbox-env.conf 2>/dev/null' || true)
  case "$DROPIN" in
    *"SetEnv PATH=\"/opt/vcs/bin:"*) echo "sshd SetEnv puts /opt/vcs/bin first" ;;
    *)
      echo "arm C: the sshd drop-in does not put /opt/vcs/bin first on PATH," >&2
      echo "so an ssh session still gets the credential shim as \`git\`:" >&2
      echo "${DROPIN:-<no drop-in file>}" >&2
      exit 1
      ;;
  esac
fi

# No gh on arm C. Both of them: the refusing stub at /opt/vcs/bin/gh that owns
# the name on PATH, and the credentialed shim at /opt/credential-proxy/bin/gh
# that the stub only shadows. After this `command -v gh` finds nothing and no
# absolute path reaches one either.
#
# The stub was an earlier attempt at the same goal and it argued for itself in
# its own header: a named gap is an answer, a missing binary is a `command not
# found` the model may read as a broken image. That argument loses to the
# thing being measured. An abstraction whose case rests on the agent not
# reaching for the forge-specific tool has to be tested with the tool absent,
# because a stub that names the replacement is itself a hint, and a hint is not
# available on any install that has not already decided to ship the
# abstraction. What tells the agent what to use is the version-control skill,
# which is the surface that ships.
#
# Both names live in the container's writable layer, so this lasts exactly as
# long as the container. That is the right lifetime: a rollout restores the
# image's arrangement, and every arm switch that changes contentWorkspaces
# rolls the sandbox.
if [ "$SEAL" = "1" ]; then
  $K exec platform-agent-shell-0 -c shell -- sh -lc '
    set -e
    [ -e /opt/credential-proxy/bin/gh.shim ] || \
      mv /opt/credential-proxy/bin/gh /opt/credential-proxy/bin/gh.shim
    [ -e /opt/vcs/bin/gh.stub ] || mv /opt/vcs/bin/gh /opt/vcs/bin/gh.stub
  '
  # Assert absence rather than refusal, and by both spellings. `command -v`
  # covers PATH; the explicit test covers the shim the stub used to shadow.
  $K exec platform-agent-shell-0 -c shell -- sh -lc '
    command -v gh >/dev/null 2>&1 && { echo "gh still on PATH: $(command -v gh)"; exit 1; }
    [ -e /opt/credential-proxy/bin/gh ] && { echo "the credentialed shim is still there"; exit 1; }
    exit 0
  ' || { echo "sealed arm C: gh is still reachable" >&2; exit 1; }
  echo "sealed: no gh on this sandbox"
else
  # Undo a previous seal in the same container. Without this a sealed C
  # followed by an unsealed anything runs with no gh at all, which is arm A
  # with its own route deleted.
  $K exec platform-agent-shell-0 -c shell -- sh -lc '
    [ -e /opt/credential-proxy/bin/gh.shim ] && \
      mv -f /opt/credential-proxy/bin/gh.shim /opt/credential-proxy/bin/gh
    [ -e /opt/vcs/bin/gh.stub ] && mv -f /opt/vcs/bin/gh.stub /opt/vcs/bin/gh
    exit 0
  '
fi

# No arm may start on top of another's leftovers: a checkout left by arm A is a
# route arm B is not supposed to have, and a C checkout is a repository with
# full history sitting where an A or B probe can find it.
$K exec platform-agent-shell-0 -c shell -- \
  sh -lc 'rm -rf /opt/data/scratch/git-access-ab /opt/data/scratch/vcs /opt/data/workspaces/* 2>/dev/null; true'
echo "scratch cleared"

# Ready is not the same as reachable. The agent reaches the sandbox over the
# headless Service, whose DNS lags the pod becoming Ready by some seconds, and
# a probe that starts inside that window fails with "shell backend does not
# resolve" and scores as a capability miss when it is nothing of the kind.
# Two runs were lost to this before the wait was added.
echo -n "waiting for the shell backend to resolve"
for _ in $(seq 60); do
  if $K exec deploy/platform-agent-gateway -c platform-agent -- \
       sh -lc 'getent hosts platform-agent-shell-0.platform-agent-shell >/dev/null' \
       2>/dev/null; then
    echo " ok"
    break
  fi
  echo -n .
  sleep 5
done
