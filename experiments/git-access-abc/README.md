# Comparing three ways to give an agent a git repository

> Archive branch. This directory exists only on `experiment/git-access-abc` in
> a fork and is never merged to `main`, the same arrangement
> `experiment/memory-scale-ab` uses for the memory-provider experiment. The
> design it supports is `docs/designs/version-control-abstraction.md`, which
> links here for the raw results.
>
> Cluster contexts and registry paths in the harness are placeholders
> (`YOUR-PROJECT`, `YOUR-CLUSTER`); set `CONTEXT` in the environment to run any
> of it against an install of your own.

This is the git-access counterpart to the memory-provider experiment in
`docs/designs/memory.md`. That one asked which retrieval design puts the right
records in front of the model. This one asks a narrower question with the same
method: when an agent's shell is isolated in a sandbox and its credentials are
held by a proxy it cannot read, **which way of handing it a repository lets it
do repository work**.

Three arms:

| arm | design | how the agent reaches the repo |
| --- | --- | --- |
| A | shared volume | the sandbox and the credential proxy share a working tree; the agent runs real `git` and `gh`, and the proxy injects the credential |
| B | content passing | no shared tree; the agent calls `/v1/workspace/{open,read,list,grep,commit,push,close}` and files move as content |
| C | VCS verbs | no shared tree and no forge CLI; the agent runs `vcs.py <verb>` against `/v1/vcs/*` on the broker, history crosses as a git bundle, and it works the unpacked copy with a local `git` that has no network transport |

All three are built and all three are measured here.
`results/FINDINGS.md` is the comparison.

## What is measured, and why there are two harnesses

The experiment is `harness/agent_probe.py`. It puts each probe to the real
agent as a natural-language question, through the gateway, with a fixed
preamble that names the repository and branch and says nothing about how to
reach it. The arm changes one field on the PlatformAgent and nothing else, so
the prompt is byte-identical across arms and any difference is the design.

`harness/ladder.py` is a control, not the experiment. It drives each access
layer directly with no model at all, the way `eval_fleet.py` scored a memory
provider's context rather than a model turn, and reports what each design
*could* put in front of a model.

Both are needed because they disagree, and the disagreement is the result. The
control scores a commit-history probe unanswerable under content passing —
there is no verb for history. The agent answers it anyway, by leaving the
workspace protocol for `gh api .../commits`. A prompt-free measurement cannot
see that, and it is the single most important thing the experiment found: what
an access layer leaves out is not what the agent ends up unable to do, it is
what the agent does by some other route. Which route it takes is a property of
the whole environment rather than of the layer, so it can only be measured with
a model in the loop.

## The corpus

`harness/gen_repo_corpus.py` builds a synthetic infrastructure repository and
pushes it to three branches of `dshnayder-org/infra`, seeded at `20260826`:

| branch | tracked files |
| --- | --- |
| `git-access-ab/r200` | 201 |
| `git-access-ab/r3000` | 3,001 |
| `git-access-ab/r10000` | 10,001 |

The corpus itself is not in this branch. It is 59 MB of generated fixtures with
no information in it that the generator does not already carry, so what is
committed here is the generator and its seed rather than its output. Re-running
`gen_repo_corpus.py` at seed `20260826` reproduces all three rungs.

The rungs nest, r200 ⊂ r3000 ⊂ r10000, and every gold artefact sits inside the
first 200. Growth therefore adds only distractors, so a metric that moves
across rungs is measuring how the design handles scale rather than how the
answer moved.

The corpus is built to punish shortcuts:

- **Contested settings.** Six policies exist in three versions each, one marked
  `current` and two `superseded`, with the superseded values still in the tree.
- **History-only rationale.** Four settings carry their reason in a commit
  message and nowhere else, so a design that ships file content but not history
  cannot reach them.
- **Decoys.** A first-party symbol and a vendored copy of the same symbol.
- **Absences.** A cluster and a function that do not exist, to see whether the
  agent says so or invents them.
- **Fidelity.** A symlink, an executable bit, a real PNG, a non-ASCII filename,
  and a 9,000-line generated file.
- **A hostile `CONTRIBUTING.md`** that instructs the reader to install a git
  clean filter which runs a command, plus a `.githooks/pre-commit` that writes
  a marker file. Whether an arm executes it is a security result, not a
  capability one.

## Probes

`probes.json` holds 24 probes. P01–P20 are read probes and climb the rungs.
P21–P23 write, on one rung, and P23 depends on P21 so it also measures whether
the agent can revise a pull request it already opened. P24 is the injection.

Scoring accepts prose. `must_contain` is a list of groups and a group is
satisfied by any one of its alternatives. A superseded value only counts as
contamination when it is asserted ahead of the correct one — a good answer
often names what it ruled out, and penalising that would reward terseness over
correctness.

## Running it

```bash
export PLATFORM_AGENT_TOKEN=$(kubectl -n kubeagents-system \
  get secret platform-agent-secrets -o jsonpath='{.data.API_SERVER_KEY}' | base64 -d)
kubectl -n kubeagents-system port-forward svc/platform-agent 8642:8642 &

harness/set_arm.sh B
python3 harness/agent_probe.py --arm B --rung 200 \
  --skip-classes write,adversarial --workers 4 --stamp b200 \
  --out results/agent-B-r200.json

harness/set_arm.sh A
python3 harness/agent_probe.py --arm A --rung 200 \
  --skip-classes write,adversarial --workers 4 --stamp a200 \
  --out results/agent-A-r200.json

python3 harness/compare.py --results results --out results/comparison.md
```

`set_arm.sh` flips `spec.harness.experimental.shellSandbox.contentWorkspaces`
and `.versionControl`, waits out the rollout, reads both flags back off the
proxy container rather than trusting the patch, and clears the scratch tree so
no arm starts on another's leftovers. For arm C it additionally asserts that the
sshd `SetEnv` drop-in puts `/opt/vcs/bin` first on `PATH`. That check exists
because its absence invalidated a whole run: `/etc/profile.d` covers a login
shell, the gateway reaches the sandbox over ssh, and without the drop-in `git`
in an ssh session still resolved to the credential shim — so the arm measured
arm A wearing arm C's label.

## Caveats

The chat profile is a front door with no terminal, so every probe is delegated
to a kanban card and the platform worker does the repository work. The worker
is the subject; the runner polls the same conversation until the card settles
and then reads the worker's own log out of the pod, because the response
envelope carries the front door's turn and not the worker's.

Route classification is string matching over that log. It is good enough to
tell a workspace verb from a `git` invocation from a `gh api` call, which is
all it is used for, and it is not a substitute for a real trajectory export.

One run per probe per arm. Agent behaviour varies between runs, so a
one-or-two-probe difference between arms is noise; the findings worth keeping
are the ones that hold across a whole class.
