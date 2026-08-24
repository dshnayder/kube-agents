# Eval baselines

Screening evidence for how each case behaves on `main`. Two of the presubmit's
rules read it: collapse (rung 4), which may only red a case that has proved it
passes reliably, and the suite aggregate, which compares a pull request's pass
rate against main's.

**This store ships empty.** Nothing is admitted until it has been screened, so
rung 4 cannot fire and the aggregate is advisory. `BOOTSTRAP_ADMITTED` in
`hack/ci-eval-pr.sh` names the cases that keep blocking meanwhile.

## Layout

| File              | What it is                                                |
| ----------------- | --------------------------------------------------------- |
| `VERSIONS.json`   | The two hand-declared halves of the version key           |
| `<case-id>.jsonl` | One file per case, named for its `bench/tasks/` directory |

Each line of `<case-id>.jsonl` is one screening campaign, filed under the
version key it was measured at. Newlines are shown here for the page's sake;
in the file a record is one line.

```json
{
  "case": "obtainability-planted-pdb",
  "recorded_at": "2026-08-25T00:00:00Z",
  "commit": "<the main sha the screening ran on>",
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

**The file is append-only, and that is the point.** Nothing here is ever
rewritten: a re-screen adds a line and every earlier line stays. So the file is
the case's history rather than its current state, which buys three things a
rewritten blob does not. Re-screening after a model bump is a one-line diff a
reviewer can read. The old numbers stay available to answer "did this case get
less reliable, or was it always like this" — the question that decides whether
a case is worth keeping. And two appends conflict far less often than two
rewrites of the same object, which is what lets a checked-in store survive
more than a handful of cases.

Reading is bottom-up: **the newest line at the current key wins**, and the
lines above it are history. Note the corollary — a case is de-admitted by
appending a line that says so, never by deleting the line that admitted it.

Only runs on `main` append. A pull request's own run is graded against this
store and never writes to it, so a case cannot move the baseline it is about to
be judged against.

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

A case is admitted when the store holds a record at the **current** key with
`passes/runs` at or above `EVAL_ADMISSION_RATE` (default 0.95) over at least
`EVAL_ADMISSION_MIN_RUNS` runs (default 20).

Admission is computed here, never declared in `task.yaml`. A pull request
author therefore cannot self-admit a case in the same diff that makes it pass,
and a key with no record is reported _stale_ rather than compared against
software it was not measured on.

## What invalidates a record

Anything that changes the key: a new agent or judge model, a devops-bench SHA
bump that moves `scoringVersion` or `setupId`, a `fleet` or `verifiers` bump.
The record stays in the file — it is still true about the software it was
measured on — and a new one is appended once re-screened.

## Regenerating

The screener that runs a case N times against `main` and appends its line is
PR 2. Until then a line is added by hand from a deliberate run, with `commit`
naming the `main` SHA it ran on.

Whoever writes it — the screener or a person — the operation is an append, and
the review question is the same: does this one new line say what the run
found? Never edit or drop a line that is already there. If a past record is
wrong rather than merely old, correct it in a commit that says so, because it
is the only way the history stops meaning what it says.
