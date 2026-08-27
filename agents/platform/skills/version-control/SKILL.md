---
name: version-control
description: Read and change a hosted repository through forge-neutral verbs — clone brings the history down as a bundle you can run log, annotate and grep against locally, and publish sends your revisions back. Also covers proposals and issues, so no forge CLI is needed. Use it for any question about history or file modes, and for opening a change proposal.
---

# version-control - version-control verbs, with the credential on the broker's side

The script is `scripts/vcs.py`. Every subcommand prints one JSON object on
stdout. `--repo` takes `owner/name` or a full URL, and the broker decides which
forge that is.

History comes **down** as a git bundle and is unpacked here, so `log`, `show`,
`annotate`, `grep` and file modes are answered locally at full fidelity with no
credential involved. Your revisions go **up** the same way, as a bundle, which
is why a branch of five commits arrives as five commits. Nothing that came out
of the repository is ever executed beside the credential.

Verb names are the version-control concept; the spelling you know is an alias.
`annotate`/`blame`, `log`/`history`, `files`/`manifest`, `grep`/`search`,
`publish`/`push`, `proposal`/`pr`/`mr` all work.

## When to Use

- **Anything about the past.** When a value changed, which revision removed a
  flag, who last touched a file, what a file looked like three revisions ago.
- **File modes.** Whether a script is executable is a tree-entry property;
  `files` reports it and it survives the round trip.
- **Changing a repository** and opening the change proposal for it.
- **Issues and proposals.** Use `issue` and `proposal` rather than a forge CLI.
- **A repository on a forge that is not GitHub.** Run `capabilities` first — it
  answers with what this install can and cannot do for that host, and it spends
  no credential doing it.

## When NOT to Use

- **A one-off read of a large upstream repository.** `clone` pulls a whole
  branch's history and there is no shallow option; **inspect-repository** pages
  a shallow view and is cheaper for "how does upstream implement this".
- **The GitOps write flow that already gave you a workspace.** `fleet-audit`
  and `submit-suggestion` own theirs; do not open a second view.

## Read

```bash
V=agents/platform/skills/version-control/scripts/vcs.py
python3 $V clone    https://github.com/dshnayder-org/infra
python3 $V log      -n 20 -- inventory/clusters.yaml
python3 $V show     HEAD~3:inventory/clusters.yaml
python3 $V annotate scripts/rotate-keys.sh
python3 $V files
python3 $V grep     'nodeCount:'
python3 $V status
```

`clone` prints the working copy's `path`. Read files in it with ordinary tools.
Every verb after the first infers the repository from the only copy there is, or
from the directory you are standing in; `--repo` says which when there are
several.

## Write

```bash
python3 $V branch  fix/replicas
# edit files under the path `clone` printed, then:
python3 $V commit  inventory/clusters.yaml -m 'raise replicas to 5'
python3 $V publish
python3 $V proposal create --title 'Raise replicas' \
                           --body 'Rollout headroom for the evening peak.'
python3 $V discard
```

`branch` and `commit` are local and make no network call. `publish` sends every
revision made since the clone, and the identifiers `log` printed here are the
identifiers that land on the forge.

## Collaborate

```bash
python3 $V issue list --state open --labels bug
python3 $V issue view 42 --comments
python3 $V issue create --title 'Cluster drift on prod-eu' --body '...'
python3 $V proposal list
python3 $V proposal view 17 --comments --diff
python3 $V proposal comment 17 --body 'Rebased on main.'
```

## Rules

- **`clone` before any other verb.** The read verbs answer from the local copy
  and say so when there is not one. The collaboration verbs do not need one if
  you pass `--repo`.
- **Do not `git push`, `git fetch` or `git remote add` in the working copy.** It
  has no remote on purpose. Revisions go up through `publish`.
- **`publish` can be refused, and the refusal is the answer.** `BASE_MOVED`
  means somebody pushed to the target since you cloned: clone again and reapply
  the change. Do not try to force it.
- **`discard` when finished.** It removes the local copy. Nothing is held on the
  credential side, so there is nothing else to release.
- **Read `exitCode` and `stderr`.** The read verbs pass git's own exit status
  through; a `log` that returned nothing because the pathspec matched no file is
  not the same answer as one that returned nothing because the file has no
  history.
- **Say which repository and which branch** in anything you report.
- **`capabilities` before assuming a non-GitHub forge works.** GitLab and
  Bitbucket parse their specs and then tell you exactly what this install is
  missing. That is the answer, not a bug to work around.

## Reference

| Subcommand         | What it does                                                                                                |
| ------------------ | ----------------------------------------------------------------------------------------------------------- |
| `capabilities`     | What this install can do for this repository's forge, before anything is spent                              |
| `clone`            | The history down as a bundle, unpacked into a local working copy; `--branch` for one line                   |
| `log`              | The revisions behind HEAD; `--patch` for diffs, `--format` a pretty format string, trailing args a pathspec |
| `show`             | One revision, or `revision:path` for a file as of that revision                                             |
| `diff`             | Differences in the working copy, or against `--revision`                                                    |
| `annotate`         | Per-line last-change attribution for one path                                                               |
| `files`            | Tracked paths with the mode the revision records                                                            |
| `grep`             | Text search over the working copy; `--regex`, `--ignore-case`                                               |
| `status`           | What the working copy has that its revision does not                                                        |
| `branch`           | List lines of development, or start one. Local                                                              |
| `commit`           | Record a revision locally, with a real parent and identifier                                                |
| `publish`          | Send the revisions made since `clone` to the shared repository                                              |
| `discard`          | Remove the local copy                                                                                       |
| `proposal create`  | Open the forge's change proposal (pull request, merge request)                                              |
| `proposal list`    | Open proposals; `--state open\|closed\|all`                                                                 |
| `proposal view`    | One proposal; `--comments` for the discussion, `--diff` for the patch                                       |
| `proposal comment` | Reply on a proposal                                                                                         |
| `issue list`       | Work items; `--state`, `--labels`                                                                           |
| `issue view`       | One issue; `--comments` for the discussion                                                                  |
| `issue create`     | Open an issue; `--labels`                                                                                   |
| `issue comment`    | Reply on an issue                                                                                           |
