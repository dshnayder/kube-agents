#!/usr/bin/env bash
# The sealed arm C write rung.
#
# What "sealed" means here, and why it is a separate run rather than a rerun:
# the w6 arm C ran with contentWorkspaces still true and a `gh` on PATH that
# refused rather than a `gh` that was absent. That configuration answers "what
# did the agent prefer with every door open", which is the right question for
# whether the abstraction is attractive and the wrong one for whether it is
# sufficient. This run closes both doors -- arm B's routes 404, and there is no
# gh binary under any path -- so what comes out is what an install that shipped
# only the abstraction would get.
set -x
cd "$HOME/git-access-ab"
SEAL=1 ARMS=C bash harness/clean_writes.sh 20260828w7 200 "" 2>&1
echo "=== exit $? ==="
date -u
