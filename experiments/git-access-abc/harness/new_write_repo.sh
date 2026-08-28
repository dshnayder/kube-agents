#!/usr/bin/env bash
# Create and seed a throwaway corpus repository for one arm's write rung.
#
# The write rung cannot be repeated on a shared repository. The first arm to run
# it opens the pull request the probe asks for; every arm after that finds the
# work already done, and "open a pull request" quietly becomes "notice one
# exists". Closing the pull requests between arms is not enough -- GitHub keeps
# closed ones searchable, and arm B's re-run found the closed #151 and cited it.
# So each arm gets its own repository and the question is asked once.
#
# The corpus is deterministic (fixed seed, fixed commit dates in
# gen_repo_corpus.py), so every arm's repository is byte-identical including its
# history. That is what keeps the arms comparable after the split.
#
# Usage: new_write_repo.sh <arm> <rung> <stamp>
set -euo pipefail

cd "$(dirname "$0")/.."

ARM="${1:?usage: new_write_repo.sh <arm> <rung> <stamp>}"
RUNG="${2:?usage: new_write_repo.sh <arm> <rung> <stamp>}"
STAMP="${3:?usage: new_write_repo.sh <arm> <rung> <stamp>}"

ORG="${ORG:-dshnayder-org}"
CONTEXT="${CONTEXT:-gke_YOUR-PROJECT_YOUR-REGION_YOUR-CLUSTER}"
NS="${NS:-kubeagents-system}"

NAME="git-access-ab-$(echo "$ARM" | tr 'A-Z' 'a-z')-r${RUNG}-${STAMP}"
FULL="${ORG}/${NAME}"
BRANCH="git-access-ab/r${RUNG}"
CORPUS="corpus/r${RUNG}"

[ -d "${CORPUS}/.git" ] || {
  echo "no corpus at ${CORPUS}; run gen_repo_corpus.py --out corpus --all" >&2
  exit 1
}

echo "creating ${FULL}"
# Already there is fine. A rung that died after the push -- the installation
# check below has done that once -- has to be resumable, and re-pushing the same
# deterministic corpus onto the same repository leaves it in the state the arm
# needs. A create that fails for any other reason still stops the run.
if ! CREATE=$(gh api -X POST "orgs/${ORG}/repos" -f "name=${NAME}" -F private=true \
  -f "description=git-access experiment, arm ${ARM} rung ${RUNG}; safe to delete" \
  --jq .full_name 2>&1); then
  case "$CREATE" in
    *"name already exists on this account"*) echo "${FULL} already exists; reusing it" ;;
    *) echo "$CREATE" >&2; exit 1 ;;
  esac
else
  echo "$CREATE"
fi

# Push the corpus history as both the default branch and the rung branch. The
# probes target the rung branch; main exists so the repository has a default and
# so a clone with no --branch lands somewhere sane.
TOKEN="$(gh auth token)"
REMOTE="https://x-access-token:${TOKEN}@github.com/${FULL}.git"
git -C "$CORPUS" push --quiet "$REMOTE" "HEAD:refs/heads/main"
git -C "$CORPUS" push --quiet "$REMOTE" "HEAD:refs/heads/${BRANCH}"
echo "pushed main and ${BRANCH}"

# The agent's credential is minted per repository, and the minter resolves a
# request by filename: one `<org>-<repo>.yaml` in CONFIGS_DIR per repository it
# will serve. An earlier version of this script appended the new name to the
# `repositories:` list inside `dshnayder-org-infra.yaml`, which reads like
# scoping and is not: that file is only consulted for requests naming `infra`,
# so a mint for the new repository never found a config at all and came back
# `HTTP 500: requested scope "platform-agent-scope" is not found`. The agent saw
# the resulting infra-scoped token 404 on the card's repository, concluded the
# repository did not exist, and did the work in `infra` instead -- a whole write
# rung scored against three designs for one wrong filename.
echo "adding a minter config for ${NAME}"
KEY="${ORG}-${NAME}.yaml"
EXISTING=$(kubectl --context="$CONTEXT" -n "$NS" get cm github-token-minter-config \
  -o json | KEY="$KEY" python3 -c '
import json, os, sys
print(json.load(sys.stdin).get("data", {}).get(os.environ["KEY"], ""))
')
if [ -n "$EXISTING" ]; then
  echo "already scoped"
else
  # Cloned from the infra config rather than written from scratch, so the rule,
  # the caller assertion and the permission set stay whatever the install
  # actually uses; only the repository list is ours.
  BASE=$(kubectl --context="$CONTEXT" -n "$NS" get cm github-token-minter-config \
    -o jsonpath='{.data.dshnayder-org-infra\.yaml}')
  PATCH=$(NEW="$NAME" KEY="$KEY" python3 -c '
import json, os, re, sys
text = sys.stdin.read()
name = os.environ["NEW"]
# One entry, not an append: this file exists for exactly one repository, and a
# list carrying the others would hand every arm a token for its neighbours.
out, count = re.subn(r"^([ \t]*)- .infra.$", lambda m: f"{m.group(1)}- {name!r}",
                     text, count=1, flags=re.M)
if count != 1:
    sys.exit("the infra minter config no longer has a - '\''infra'\'' line to clone from")
print(json.dumps({"data": {os.environ["KEY"]: out}}))
' <<<"$BASE")
  kubectl --context="$CONTEXT" -n "$NS" patch cm github-token-minter-config \
    --type=merge -p "$PATCH" >/dev/null
fi

# A key in the ConfigMap is not a file the minter can read. Its config volume is
# mounted one `subPath` at a time -- `/etc/minty/<org>/<repo>.yaml` per entry --
# so a repository whose config exists only as a ConfigMap key still resolves to
# nothing and the mint fails with the same "scope is not found" as no config at
# all. This mount is the other half, and its absence is what survived the first
# fix.
MOUNT="/etc/minty/${ORG}/${NAME}.yaml"
HAVE=$(kubectl --context="$CONTEXT" -n "$NS" get deploy github-token-minter \
  -o json | MOUNT="$MOUNT" python3 -c '
import json, os, sys
container = json.load(sys.stdin)["spec"]["template"]["spec"]["containers"][0]
mounts = container.get("volumeMounts") or []
print("yes" if any(m.get("mountPath") == os.environ["MOUNT"] for m in mounts) else "no")
')
if [ "$HAVE" = "yes" ]; then
  echo "minter already mounts ${MOUNT}"
else
  kubectl --context="$CONTEXT" -n "$NS" patch deploy github-token-minter \
    --type=json -p "$(python3 -c '
import json, sys
print(json.dumps([{
    "op": "add",
    "path": "/spec/template/spec/containers/0/volumeMounts/-",
    "value": {"name": "config-volume", "mountPath": sys.argv[1], "subPath": sys.argv[2]},
}]))
' "$MOUNT" "$KEY")" >/dev/null
fi
kubectl --context="$CONTEXT" -n "$NS" rollout restart deploy/github-token-minter
kubectl --context="$CONTEXT" -n "$NS" rollout status deploy/github-token-minter --timeout=5m

# Prove the mint rather than trust the config. This is the check whose absence
# cost the first write rung: everything above can succeed and the token still
# come back scoped to the wrong repository.
#
# Wait for the sandbox first. The mint runs in the proxy container, and a pod
# still coming back from an earlier arm's rollout answers `exec` with
# "container not found" -- which is indistinguishable here from a token the
# minter refused, and stops a run that had nothing wrong with it.
kubectl --context="$CONTEXT" -n "$NS" wait --for=condition=Ready \
  pod/platform-agent-shell-0 --timeout=10m
echo -n "minting a token for ${FULL}: "
kubectl --context="$CONTEXT" -n "$NS" exec platform-agent-shell-0 \
  -c envoy-credential-proxy -- \
  sh -lc "/opt/defaults/scripts/github_token_refresh.py '${FULL}' >/dev/null 2>&1 &&
          gh api /installation/repositories --jq '.repositories[].full_name'" \
  | grep -qx "$FULL" || {
  echo "no"
  echo "the minted token does not carry ${FULL}; the run would score the harness" >&2
  exit 1
}
echo "ok"

# The App installation may be scoped to selected repositories, and adding one
# needs an organization owner. Say so rather than letting the run discover it as
# a 404 forty minutes in.
#
# The endpoint is app-authenticated: it answers a JWT and 401s anything else.
# `gh auth token` on a machine logged in through `gh auth login` is an OAuth
# token, so the check cannot run there at all -- it 401s for every repository,
# visible or not. Failing closed on that would refuse a correctly configured
# install because of how the operator happened to log in, so only a 404 stops
# the run; anything else is reported as a check that did not happen.
if ! INSTALL=$(gh api "repos/${FULL}/installation" --jq .id 2>&1); then
  case "$INSTALL" in
    *"HTTP 404"* | *"Not Found"*)
      cat >&2 <<EOF

${FULL} is not visible to the k8s-agentic-harness GitHub App.
An organization owner has to add it, or set the installation to
"All repositories" once so the harness stops needing a human per run:
  https://github.com/organizations/${ORG}/settings/installations/145676836
EOF
      exit 3
      ;;
    *)
      echo "could not check App visibility for ${FULL}: ${INSTALL}" >&2
      echo "continuing; a write probe returning 404 means this was the reason" >&2
      ;;
  esac
fi

echo "REPO=${FULL}"
