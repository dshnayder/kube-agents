#!/usr/bin/env bash
# Put the corpus repository back to the state the write rung assumes.
#
# The write probes ask the agent to open a pull request. The first arm to run
# them opens it; every arm after that finds it already open, and P21's
# "open a pull request" becomes "notice one exists". That is what happened on
# the first pass: arm A opened #151 and #152, and arms B and C were then scored
# against arm A's pull requests, because verify_writes.py reads the numbers out
# of the agent's answer and does not ask who created them. Arm C's agent said
# so in as many words -- "the change already exists and is open for review --
# I did not open a duplicate" -- which is the right behaviour and the wrong
# measurement.
#
# So the write rung is not repeatable without this: close the proposals, delete
# their branches, and leave the rung branch itself alone (an unmerged proposal
# never touched it).
#
# Usage: reset_writes.sh [rung]   # default 200
set -euo pipefail

RUNG="${1:-200}"
REPO="${REPO:-dshnayder-org/infra}"
BASE="git-access-ab/r${RUNG}"

# Every proposal the write probes can produce targets the rung branch, and
# nothing else in this repository does. Basing the sweep on the target rather
# than on a list of branch names catches the ones an arm invented for itself:
# arm C's run used policy/ingress-class-envoy-ga, which no list would have had.
# while-read rather than mapfile: the harness is driven from macOS, whose
# /bin/bash is 3.2 and has neither.
gh pr list --repo "$REPO" --base "$BASE" --state open \
  --limit 100 --json number,headRefName --jq '.[] | "\(.number) \(.headRefName)"' |
  while read -r number head; do
    [ -n "$number" ] || continue
    echo "closing #${number} (${head})"
    gh pr close "$number" --repo "$REPO" --delete-branch \
      --comment "Closed by the git-access experiment harness: resetting the write rung between arms." \
      >/dev/null
  done

# --delete-branch is best effort on a closed pull request, and a branch with no
# proposal at all leaves nothing for the loop above to find. Sweep the refs
# directly; the rung branch and main are the only two that must survive.
gh api "repos/${REPO}/git/matching-refs/heads/" --paginate \
  --jq '.[].ref | sub("^refs/heads/"; "")' |
  while read -r ref; do
    case "$ref" in
      main | git-access-ab/*) continue ;;
    esac
    # Only branches this experiment could have made. The repository is a real
    # one with years of unrelated branches on it, and deleting those is not a
    # reset.
    case "$ref" in
      platform-agent/ingress-class* | platform-agent/*drain-pool* | \
        policy/ingress-class* | smoke/armc-*) ;;
      *) continue ;;
    esac
    echo "deleting branch ${ref}"
    gh api -X DELETE "repos/${REPO}/git/refs/heads/${ref}" >/dev/null 2>&1 || true
  done

# The base branch is only touched if something was merged into it, which the
# probes never ask for. Say what it points at so the run record has the commit
# each arm started from.
echo -n "${BASE} at "
gh api "repos/${REPO}/git/ref/heads/${BASE}" --jq '.object.sha'
