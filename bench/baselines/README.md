# Eval baselines

Screening evidence for how each case behaves on `main`. Three of the
presubmit's rules read it: collapse (rung 4), which may only red a case that
has proved it passes reliably; judged regression (rung 6), which compares this
pull request's judge scores against main's at the same version key; and the
suite aggregate, which compares pass rates.

**This store ships empty, and it fills itself.** Every postsubmit run on `main`
appends what it measured (`bench-gate record`), and a case is admitted once its
accumulated evidence clears the bar. Until then nothing is admitted: rung 4
cannot fire, rung 6 has nothing to compare against, and the aggregate is
advisory. That is a legitimate green, not a broken gate — it is the gate
collecting. `BOOTSTRAP_ADMITTED` in `hack/ci-eval-pr.sh` names the cases that
keep blocking meanwhile.

## Layout

| File              | What it is                                                |
| ----------------- | --------------------------------------------------------- |
| `VERSIONS.json`   | The two hand-declared halves of the version key           |
| `<case-id>.jsonl` | One file per case, named for its `bench/tasks/` directory |

Each line of `<case-id>.jsonl` is **one batch of runs** — a deliberate 20-run
screening campaign, or the three repetitions an ordinary postsubmit produced —
filed under the version key it was measured at. Newlines are shown here for the
page's sake; in the file a record is one line.

```json
{
  "case": "obtainability-planted-pdb",
  "recorded_at": "2026-08-25T00:00:00Z",
  "commit": "<the main sha this batch ran on>",
  "key": {
    "setup_id": "gemini-3-1-pro-preview-kubeagents-mcp",
    "scoring_version": "v1",
    "judge_model": "gemini-3.1-pro-preview",
    "fleet": 1,
    "verifiers": 1
  },
  "runs": 20,
  "passes": 19,
  "judged": { "OutcomeValidity": { "mean": 0.81, "n": 20 } }
}
```

`runs` counts only the repetitions that produced a pass or a fail. Repetitions
that were blocked by rungs 1–3, or that never produced a record at all, are
counted separately as `blocked` and `infra`, and both keys are omitted when
zero. They stay out of the rate because rungs 1–3 block absolutely whether or
not a case is admitted, so admission need not model them; they stay _in the
line_ because dropping them would make a case that crashes half the time look
perfectly reliable in its own history.

`judged` carries a mean and its own `n` per metric, so a batch of 20 outweighs
a batch of 3 when the two are pooled. A metric the run did not produce is
absent, never zero.

**The file is append-only, and that is the point.** Nothing here is ever
rewritten: a re-screen adds a line and every earlier line stays. So the file is
the case's history rather than its current state, which buys three things a
rewritten blob does not. Re-screening after a model bump is a one-line diff a
reviewer can read. The old numbers stay available to answer "did this case get
less reliable, or was it always like this" — the question that decides whether
a case is worth keeping. And two appends conflict far less often than two
rewrites of the same object, which is what lets a checked-in store survive
more than a handful of cases.

**Reading is bottom-up and cumulative.** The bar wants 20 runs and an ordinary
postsubmit is 3 repetitions, so a rule that read only the newest line could
never admit anything the routine job produces — the store would ship empty and
stay empty. Instead the reader walks the lines at the current key newest-first
and pools them until it holds 20 runs. One 20-run campaign is therefore one
line, seven ordinary postsubmits are seven, and both admit.

Whole lines only: pooling overshoots to 21 rather than trimming a line to land
on 20 exactly, because trimming would invent a sub-record nobody measured.

Stopping at the bar rather than reading the whole file is what buys recency for
free. A case that starts failing has its old passing lines pushed out of the
window by the new failing ones, and **de-admits itself** — nobody edits the
store, and no line is ever deleted.

Recording is unconditional on the verdict. A red run on `main` is exactly the
evidence that de-admits a case that has stopped working; a store that recorded
only good days would drift its own bar upward until nothing could clear it and
nothing could fall back below it.

Only runs on `main` append. A pull request's own run is graded against this
store and never writes to it, so a case cannot move the baseline it is about to
be judged against. That is enforced twice — the postsubmit condition in
`hack/ci-eval-pr.sh`, and a refusal inside `bench-gate record` itself when
`PULL_NUMBER` is set — because a guard living only in shell is one careless
edit away from being gone.

A store that will not parse is an **error**, never an empty store. Empty means
"nothing admitted, aggregate advisory", which is a green; a corrupt file
reaching that state would silently disarm the gate.

A leftover `<case-id>.json` from the pre-JSONL format is an **error**, not a
file to skip: skipping it would read as "never screened" and silently de-admit
the case rather than telling anyone the format changed.

## The version key

Three of the five components are read off the run rather than declared, so
they cannot go stale:

| Component         | Read from                      | Covers                                       |
| ----------------- | ------------------------------ | -------------------------------------------- |
| `setup_id`        | `manifest.json` → `setupId`    | Agent model, harness, augmentation           |
| `scoring_version` | `rows.json` → `scoringVersion` | devops-bench's roll-up formula               |
| `judge_model`     | `$JUDGE_MODEL`                 | The judge, pinned independently of the agent |
| `fleet`           | `VERSIONS.json`                | The `bench/tf/fleet` state a task audits     |
| `verifiers`       | `VERSIONS.json`                | `kube_agents_bench/verifiers.py` behaviour   |

The judge model is a component of its own, and is pinned independently of the
agent model, because a judge that tracks whatever the agent is running cannot
be told apart from an agent that got better — and a drifting judge moves every
baseline at once.

`fleet` and `verifiers` are hand-bumped integers rather than content hashes: a
hash changes on a comment typo, and re-baselining here costs a pull request
rather than a backfill. It is the same contract `bench/pyproject.toml` already
asks for the devops-bench SHA. The trade-off is real — a behaviour change with
no bump silently compares against a stale baseline — and a lint for it is
still owed.

## Admission

A case is admitted when its pooled evidence at the **current** key holds at
least `EVAL_ADMISSION_MIN_RUNS` runs (default 20) at a rate of at least
`EVAL_ADMISSION_RATE` (default 0.95).

Short of that the gate says so in the case's own words, and the four states are
distinct on purpose:

| State                   | What the presubmit prints                           |
| ----------------------- | --------------------------------------------------- |
| Nothing at this key     | `no screening evidence for this case yet`           |
| Evidence at an old key  | `stale: …`, never compared against                  |
| Fewer than the min runs | `collecting: 9/9 runs recorded … 11 more needed`    |
| At the bar, below rate  | `screened at 17/21 …, below the bar of 95% over 20` |

Only the last is a problem with the case. The middle two are the store filling
up, which is the ordinary state of a new case and of every case after a version
bump.

Admission is computed here, never declared in `task.yaml`. A pull request
author therefore cannot self-admit a case in the same diff that makes it pass.

## What invalidates a record

Anything that changes the key: a new agent or judge model, a devops-bench SHA
bump that moves `scoringVersion` or `setupId`, a `fleet` or `verifiers` bump.
The record stays in the file — it is still true about the software it was
measured on — and a new one is appended once re-screened.

## Regenerating

Ordinarily nobody does: the postsubmit appends a line every time it runs on
`main`, and 20 runs of evidence arrive after seven merges. To fill the store
faster — a new case, or every case after a version bump — run the suite N times
on a `main` checkout and record each one:

```sh
uv run bench-gate record \
  --case-result "$ARTIFACTS"/case-*.json \
  --commit "$(git rev-parse HEAD)" \
  --lines-out /tmp/appended.jsonl
```

It refuses to run with `PULL_NUMBER` set. It is also unconditional on the
verdict, deliberately — see above.

The store lives in git and the postsubmit has no push credential, so its append
dies with the workspace; `--lines-out` writes the same lines as a Prow artefact
for somebody to land. Automating that push is its own change, with its own
credential argument.

Whoever writes a line — the recorder or a person — the operation is an append, and
the review question is the same: does this one new line say what the run
found? Never edit or drop a line that is already there. If a past record is
wrong rather than merely old, correct it in a commit that says so, because it
is the only way the history stops meaning what it says.
