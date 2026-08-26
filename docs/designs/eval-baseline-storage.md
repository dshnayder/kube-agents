# Eval Baseline Storage

> **STATUS — design of record; partially implemented.** The record format, the version key,
> admission, reset and rung 6 are implemented in `bench/kube_agents_bench/` and covered by
> `bench/tests/`. The GCS backend is implemented and defaults **off**; the bucket and its IAM
> grant do not exist yet, and the dashboard is described here but not built.

**Scope:** Where the eval scorer's results are stored, how a baseline is established, compared
against and reset, and how a quality-over-time dashboard would read the same data.
**Owns:** the JSONL record format, the five-component version key, the admission rule, the storage
backends, and rung 6's comparison. The verdict ladder itself belongs to
`docs/designs/testing-strategy.md` §4.2 and the case format to
`docs/designs/bench-case-format.md`; both arrive with other pull requests (#896 and #921), so
they are named rather than linked here until they land.

---

## The problem

The eval presubmit could not answer "did this pull request make things worse", because it had
nothing to compare against. It ran each case once, demanded a pass, and forgot the result. Two
consequences followed. A single flaky failure redded the whole job — `agent-kanban-smoke` redded 8
of 11 recent pull requests for reasons no pull request caused — so the job was marked
`optional: true`, which is the polite form of switched off. And no run's evidence survived:
`dump_prow_artifacts_on_failure()` wrapped its artifact copy in a non-zero-exit check, so a
**passing** run kept nothing.

A rate-based gate needs the opposite of that. Its evidence comes from green runs on `main`, it
needs many of them, and it needs to know which software they were measured on.

## What is stored

One JSON object per line, one line per **batch of runs** — a deliberate screening campaign, or the
three repetitions an ordinary postsubmit produced.

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
  "blocked": 0,
  "infra": 0,
  "judged": { "OutcomeValidity": { "mean": 0.81, "n": 20 } }
}
```

`runs` counts only repetitions that produced a pass or a fail. `blocked` (stopped by ladder rungs
1–3) and `infra` (no record came back at all) are counted separately and omitted when zero. They
stay **out of the rate** because rungs 1–3 block absolutely whether or not a case is admitted, so
admission need not model them. They stay **in the line** because dropping them would make a case
that crashes half the time look perfectly reliable in its own history.

`judged` carries a mean and its own `n` per metric, so a 20-run batch outweighs a 3-run batch when
the two are pooled. A metric the run did not produce is **absent, never zero** — the same
omitted-is-not-zero rule the scorer applies to `VerificationCatastrophic`.

### Why append-only, and why it keeps history

Nothing is ever rewritten. A re-screen adds a line and every earlier line stays, so the file is the
case's _history_ rather than its current state. That buys three things a rewritten blob does not:

- Re-screening after a model bump is a one-line diff a reviewer can read.
- The old numbers stay available to answer "did this case get less reliable, or was it always like
  this" — the question that decides whether a case is worth keeping.
- Two appends conflict far less often than two rewrites of the same object.

It is also what makes a dashboard possible at all; see [Dashboard](#dashboard).

## Where it is stored

Two backends, one record format, identical semantics. The scorer reads an ordered list of records
per case and never touches a file directly.

| Backend         | Location                 | Layout                                                      |
| --------------- | ------------------------ | ----------------------------------------------------------- |
| Local (default) | `bench/baselines/`       | one appendable `<case>.jsonl` per case                      |
| GCS             | `gs://<bucket>/<prefix>` | one immutable object per batch, filed under its version key |

Selected by `--baseline-store`, then `$EVAL_BASELINE_STORE`, then `--baseline-dir` — in that
precedence. A value starting `gs://` picks GCS; anything else is a directory path. All three
unset means `bench/baselines/`, so nothing changes for a developer running the gate on a checkout,
and every unit test stays hermetic and offline.

### Why GCS is the intended home

The local backend puts the store in git, which was the original choice. It has one structural
problem: **something has to commit it.** The postsubmit that measures the evidence has no push
credential, so either a bot gets write access to `main` — a new and fairly broad trust grant — or a
human lands machine-generated count lines by hand, forever.

GCS removes that. The job writes with the credential it already has, and there is no bot with
write access to a protected branch.

The Prow artifact bucket is **not** the right home, for four reasons: it is write-once per build so
there is nothing to append to; it is keyed by build id rather than by case, so a read becomes a
scan of CI history; it normally carries a lifecycle deletion rule, which would silently expire
baselines and de-admit cases because storage deleted their evidence; and it belongs to
test-infra, whose policy can change without anyone here hearing about it.

### GCS layout

```
gs://<bucket>/<prefix>/<case-id>/<setup-id>/<judge-model>/<sv>-f<n>-v<n>/<recorded_at>-<build-id>.jsonl
```

For example:

```
gs://kube-agents-evals-bench/evidence/
  agent-kanban-smoke/
    gemini-3-1-pro-preview-kubeagents-mcp/
      gemini-3.1-pro-preview/
        v1-f1-v1/
          2026-08-01T02-03-04Z-12345.jsonl
```

One object per batch, never appended to. Object names begin with an ISO-8601 UTC timestamp, so
lexical sort is chronological and the reader gets newest-first ordering for free. The build id
suffix keeps two batches in the same second from colliding. Any character that is not
alphanumeric, `-`, `_` or `.` is flattened to `-` in every segment, so a model spelled
`vendor/model:tag` cannot add a path level; dots survive, because the judge model is spelled with
them and the point of this layout is that a human can read it.

**The key is in the path** because evidence is only ever pooled within one key —
`evidence_for()` discards every line measured on different software. Filing by key means a prefix
stops growing the moment the key changes: a model bump freezes the old directory forever and
starts a new one, so no single prefix grows without bound while the software moves. That is the
whole reason for the nesting, and it is why the partition is the **whole** key rather than the
judge model alone — partitioning on one component would leave a `setup_id` or `verifiers` bump
still piling into the same directory.

It also makes the store navigable, which a content hash would not: `ls` on a case shows which
setups have been screened, and `*/gemini-3.1-pro-preview/**` finds every case a given judge
scored, neither of which a hash would answer without opening a record.

**The path is an index, never the truth.** Every record carries its own `key` and the reader
filters on that, not on where the object sat. A name that disagrees with its contents loses, which
is the only safe way round for something a future writer could get wrong. A record with no key at
all is filed under `<case-id>/unkeyed/` rather than dropped — `bench-gate record` already skips
those, so this is the belt to that braces: the writer must never be the reason a merge to `main`
loses data.

This layout exists to fit the **`roles/storage.objectCreator`** grant, which can create new objects
but cannot overwrite or delete existing ones. That makes append-only an IAM guarantee rather than a
convention — strictly stronger than what git gives, where a force-push can rewrite history.

**Why a file per batch instead of one growing file per case.** GCS objects are immutable; there is
no append. Growing one `<case>.jsonl` means download, concatenate, re-upload — an overwrite, which
in IAM terms needs `storage.objects.delete`, which is precisely the permission whose absence was
the argument for GCS over git in the first place. It also races: two postsubmits that read the same
object and both write back silently lose one batch, with no error anywhere. `compose` does not
rescue it either, because composing into the existing name is still an overwrite of that name (and
it caps at 32 sources per call, with composite objects accumulating components toward a hard
ceiling).

In practice that means each GCS object holds **exactly one record** — one `bench-gate record` call
for one case, which is that job's repetitions. It is still JSONL rather than a JSON document, and
the distinction is load-bearing rather than pedantic: the reader concatenates objects and parses
per line, so the trailing newline every object ends with is what makes the next one safe to append
to the stream, and BigQuery's external table is `NEWLINE_DELIMITED_JSON`. Nothing caps an object at
one line; a writer that emitted several would read back unchanged.

**The sharding is invisible to every reader, and that is not luck.** JSONL is closed under
concatenation: the meaning of a set of lines does not depend on how they were split across files.
So the local backend's one-file-per-case and the GCS backend's one-object-per-batch produce
byte-identical input to the parser, BigQuery's external table over `<prefix>/*.jsonl` sees one table
regardless of the split, and `evidence_for()`'s pooling never learns that objects exist. The format
is doing the work that an append would otherwise have to.

`VERSIONS.json` deliberately stays in git even when evidence lives in GCS. It is hand-declared,
reviewed configuration — the `fleet` and `verifiers` integers a contributor bumps on purpose — not
measured data. Config belongs where it gets reviewed.

### Reading is capped, and says so

The reader lists the whole prefix once, groups the object names by case and then by key directory,
takes the newest `EVAL_BASELINE_MAX_OBJECTS` (default 200) **per case per key**, and concatenates
what survives in one `cat`. 200 objects is roughly 600 runs, two orders of magnitude past the 20
the admission bar wants, so the cap never binds in practice — but it bounds a read that would
otherwise grow without limit as one key accumulates years of history, and when it does bind the
gate says which case was capped and by how much. A cap that is silent reads as "I considered
everything" when it did not.

**Per key, not per case, and that distinction is load-bearing.** Capping a case as a whole would
sort its key directories against each other, so an alphabetically early _current_ key could be
dropped to keep a _superseded_ one — silently de-admitting a case that has in fact been screened.
There is a test that fails if the cap is moved back up to the case level.

Ordering survives the nesting for the same reason: a key deterministically determines its
directory, so all of one key's records land in one directory and sort by stamp within it, and
`evidence_for()` filters to a single key before it walks. It never sees the interleaving between
directories.

**The cap bounds the fetch, not the listing.** Listing is O(every object ever written under the
prefix), because the reader cannot know which names are newest without seeing them. The key
partition largely settles this on its own: a prefix stops growing when the key changes, and a
long-lived key at three merges a day is on the order of a thousand objects. What remains unbounded
is the _total_ across all historical keys, which grows only as fast as the software versions do. At
today's scale — a handful of active cases, one batch per case per merge — that is a few hundred
objects a year and invisible. If it ever stops being invisible, the fix is to scope the listing to
the key being read rather than the whole prefix, which the layout now makes a one-line change; see
[Open items](#open-items).

Costs are not the constraint at any of these scales. Standard storage bills actual bytes with no
minimum object size, and both the listing and the per-object fetches are fractions of a cent per
run.

The key partition also retires a caveat this section used to carry. Under a flat layout and a
per-case window, a version key that went A → B → A could push the revert's own evidence at key A
out of the window, so a genuinely screened case would read as "no evidence" and be de-admitted.
With one directory per key and a per-key cap, key B's volume cannot displace key A's records at
all: the revert lands back in A's directory and finds its own history intact.

### When the store is unreachable

Three failure classes, deliberately not treated alike:

| Failure                            | Behaviour                                   | Why                                                                                        |
| ---------------------------------- | ------------------------------------------- | ------------------------------------------------------------------------------------------ |
| Bytes arrived, they will not parse | **exit 2**, the job stops                   | A gate that cannot read its own evidence must not report green                             |
| Cannot reach the store at all      | Advisory, with a loud banner in the verdict | A network blip redding every pull request is the failure mode that gets gates switched off |
| A write failed                     | Warning only, verdict unaffected            | Bookkeeping must never be the reason a merge to `main` reds                                |

The middle row is a real trade: a sustained outage silently loosens the gate. That is why the
banner is in the markdown verdict and not only in the log.

## Alternatives considered

### Checked-in JSONL in git — the original decision, and why it is not the production store

This is what the first implementation shipped, and it remains the default backend. It has real
advantages, which is why it was chosen first: the store travels with the checkout, so the presubmit
needs no credential, no network and no new infrastructure; every unit test is hermetic; `git log`
and `git bisect` answer "what did the gate believe at commit X" exactly; and a re-screen after a
model bump is a diff a human reviews.

It fails on one question: **who writes it.** The job that measures the evidence is a postsubmit
with no push credential, and every way of giving it one was worse than the problem:

| Option                             | Why it was rejected                                                                                                                                                                                                                                                             |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Bot pushes to `main` on each merge | Needs write access to a protected branch, which is a broad and permanent grant for the sake of appending `{"runs": 3, "passes": 3}`. It also breaks the property that `main` only changes through reviewed pull requests, and two concurrent postsubmits race on the same file. |
| Bot opens a pull request per merge | About seven pull requests a week of pure noise, each triggering CI. To be useful they would need auto-merge, which is the same trust grant by a longer route.                                                                                                                   |
| Periodic job lands a batched PR    | The least bad, and still a bot with PR rights. Worse, evidence sits unlanded for up to a week — including the failing lines that should have de-admitted a broken case, so the gate keeps blocking on a case it already has the evidence to release.                            |

Two smaller problems compound it. **When to update was never clean**: the natural moment is "every
postsubmit", which is precisely the moment that needs the credential. And **repo churn** — every
eval merge would touch a dozen `bench/baselines/*.jsonl` files, so `git blame` on the tree fills
with machine-generated count lines and the diff of a real change gets harder to read.

Against that, the thing git was protecting turned out to be weaker than it looked. Nobody
meaningfully _reviews_ a machine-generated `{"runs": 3, "passes": 3}` line; what the review was
really buying was **auditability**, and immutable versioned GCS objects under `objectCreator`
provide that more strongly than git does — a force-push can rewrite git history, and an IAM grant
without `storage.objects.delete` cannot.

So the local backend is not discarded. It is demoted: still the default, still how developers run
the gate on a checkout, still how every test stays offline — just not where production evidence
accumulates.

### The Prow artifact bucket

Rejected for four reasons given under [Why GCS is the intended home](#why-gcs-is-the-intended-home):
write-once per build, keyed by build rather than case, a lifecycle rule that would silently expire
baselines, and ownership by test-infra rather than by this project. It remains useful as a
_spool_ — `--lines-out` writes each run's lines there — so nothing is lost while the real store is
unavailable.

### One rewritten JSON blob per case

The cheapest thing that satisfies #899, which asks only that a baseline be keyed on five versions
and "backfills on demand". A single `{"runs": 20, "passes": 19}` per key would do it.

Rejected because it throws away the question worth asking. Without history there is no answer to
"was this case always this flaky", no way to audit why a case de-admitted, and no dashboard — the
whole of [Dashboard](#dashboard) exists only because every batch is retained. History was a
deliberate addition beyond #899's requirement, taken with Jayanti, not an accident of format.

### BigQuery (or any database) as the primary store

Rejected as the _write_ path. The presubmit would need query access, per-PR latency and cost, and a
credential on the read side as well as the write side; and table rows are mutable, so append-only
becomes a convention again rather than an IAM guarantee.

BigQuery is the right _read_ path, over the GCS objects as an external table. Write to immutable
files, query them relationally — see [Dashboard](#dashboard).

### Re-deriving the baseline on demand

#899's phrase "re-running the suite against the merge target backfills on demand" suggests
computing main's numbers when they are needed instead of storing them. At 20 runs per case that is
20× the eval cost on every pull request that touches an unscreened case. The stored baseline exists
precisely to avoid this.

## The version key

A baseline is valid for exactly one combination of five versions. Three are read off the run
itself, so they cannot go stale:

| Component         | Read from                      | Covers                                       |
| ----------------- | ------------------------------ | -------------------------------------------- |
| `setup_id`        | `manifest.json` → `setupId`    | Agent model, harness, augmentation           |
| `scoring_version` | `rows.json` → `scoringVersion` | devops-bench's roll-up formula               |
| `judge_model`     | `$JUDGE_MODEL`                 | The judge, pinned independently of the agent |
| `fleet`           | `VERSIONS.json`                | The `bench/tf/fleet` state a task audits     |
| `verifiers`       | `VERSIONS.json`                | `kube_agents_bench/verifiers.py` behaviour   |

The judge model is its own component, pinned independently of the agent model, because a judge that
tracks whatever the agent is running cannot be told apart from an agent that got better — and a
drifting judge moves every baseline at once.

`fleet` and `verifiers` are hand-bumped integers rather than content hashes: a hash changes on a
comment typo. The trade-off is real — a behaviour change with no bump silently compares against a
stale baseline — and a lint for it is still owed.

## Establishing a baseline

There is **no counter and no stored admission flag.** The evidence is the count, and it is
recomputed on every read.

1. Every postsubmit run on `main` appends one line per case (`bench-gate record`).
2. On any later run, `BaselineStore.evidence_for()` keeps only the lines whose `key` matches the
   **current** key, walks them newest-first, and sums `runs` and `passes` until it holds
   `EVAL_ADMISSION_MIN_RUNS` (default 20).
3. The case is admitted when that pool has ≥ 20 runs **and** a rate ≥ `EVAL_ADMISSION_RATE`
   (default 0.95).

So "the case admits itself" is not a transition anybody writes — it is the same pure function
returning a different answer once the file crossed a threshold.

**Runs, not lines.** #899 specifies "20 runs against `main`, at least 19 passing", and it fixes the
unit elsewhere in the same table: "an admitted case that fails **all three of its runs**". A run is
one execution. Three repetitions per postsubmit therefore means about **seven merges** from empty
to admitted, not twenty.

**Whole lines only.** Pooling overshoots to 21 runs rather than trimming a line to land on 20
exactly, because trimming would invent a sub-record nobody measured.

**Recording is unconditional on the verdict.** A red run on `main` is exactly the evidence that
de-admits a case that has stopped working. A store that recorded only good days would drift its own
bar upward until nothing could clear it and nothing could fall back below it.

**A pull request never writes.** Enforced twice: the `JOB_TYPE = postsubmit` condition in
`hack/ci-eval-pr.sh`, and an independent refusal inside `bench-gate record` when `PULL_NUMBER` is
set. A guard living only in shell is one careless edit from being gone.

### The four pre-admission states

Reported distinctly, because only one of them is a problem with the case:

| State                   | What the gate prints                                |
| ----------------------- | --------------------------------------------------- |
| Nothing at this key     | `no screening evidence for this case yet`           |
| Evidence at an old key  | `stale: …`, never compared against                  |
| Fewer than the min runs | `collecting: 9/9 runs recorded … 11 more needed`    |
| At the bar, below rate  | `screened at 17/21 …, below the bar of 95% over 20` |

The middle two are the store filling up, which is the ordinary state of a new case and of every
case after a version bump. During that window nothing is admitted, so rung 4 cannot fire and rung 6
is silent — a legitimate green, not a broken gate.

## Resetting a baseline

Three resets, all of which happen without deleting anything.

**Version bump (automatic).** Any of the five components changing means zero lines match the
current key, so every case drops to unadmitted and re-screens itself over the next ~7 merges. Old
lines stay — they are still true about the software they were measured on.

**Degradation (automatic).** A case that starts failing has its passing lines pushed out of the
20-run window by the new failing ones, and de-admits itself. Nobody edits the store, no line is
deleted, and the case stops being able to red the job on its own.

**Correcting a wrong record (manual, and it should be loud).** If a past line is _wrong_ rather
than merely old, correct it in a commit or an object that says so. Never quietly drop a line —
that is the only way the history stops meaning what it says.

There is deliberately no "reset this baseline" command. Every legitimate reset is a consequence of
new evidence, and a button that discards evidence is a button that gets pressed when the gate is
inconvenient.

### Bootstrap

`BOOTSTRAP_ADMITTED` names cases that keep blocking before any screening exists. It is a bridge,
not a destination: a bootstrap-admitted case has no measured evidence, so it arms rung 4 but leaves
rung 6 quiet and contributes nothing to `main`'s side of the aggregate.

## How "judged scores below main's baseline" is determined

This is rung 6, and it is the only place in the ladder where "it technically passed but got worse"
is sayable.

**Step 1 — this run's number.** `judged_means(reps)` averages each judged metric over the
repetitions that were actually **scored** (outcome pass or fail). Blocked and infra repetitions are
excluded: a judge that scored a run the harness never completed is scoring an artefact.

**Step 2 — main's number.** `_pool_judged()` combines the `judged` blocks of the pooled baseline
records into one mean per metric, **weighted by each block's own `n`** so 20 runs of evidence
outweigh 3. A block with no usable mean, or a non-positive `n`, is dropped rather than counted as
zero.

Both sides produce the same `{"mean": …, "n": …}` shape from the same code path, which is the
point: the number a pull request is judged _against_ was computed the same way as the number it is
judged _with_.

**Step 3 — compare.** For each metric in `EVAL_JUDGED_METRICS` (default `OutcomeValidity`), rung 6
fires when

```
this_run_mean < main_mean - EVAL_JUDGED_MARGIN
```

If either side is missing the metric, it is skipped — omitted is not zero on either side.

**Step 4 — the gates on the rung itself.** It only runs when the case is **admitted**, is not
`expected_fail`, has **complete** evidence (every repetition scored), and main actually has judged
evidence at this key. A bootstrap-admitted case therefore never trips it, because it has no
measured baseline by construction.

### Why the margin is 0.5

Arithmetic on measured spread, not preference. Three repetitions of **one unchanged task** scored
`OutcomeValidity` 0.9, 1.0 and 0.2 — a standard deviation near 0.44, so the standard error of a
three-repetition mean is about 0.25.

| Margin      | Reds an unchanged PR about |
| ----------- | -------------------------- |
| 0.25 (1 SE) | 1 run in 6                 |
| 0.50 (2 SE) | 1 run in 50                |

Two standard errors is the same order the collapse rule was sized to, so 0.5 it is. The first draft
used 0.25 and the test written to check it caught the mistake.

**Say plainly what that buys and what it does not.** At three repetitions, rung 6 catches a
_collapse_ in judged quality and **cannot see drift**, because at three repetitions drift and noise
are the same picture. The fix for drift is more repetitions or a less variable metric — not a
smaller number here.

This is also why the gate is two-speed. The same three runs that produced 0.9 / 1.0 / 0.2 from the
judge produced a rock-steady deterministic `VerificationCorrectness` of 0.5. The deterministic
scores decide whether a repetition passed; no judged score can fail a repetition on its own; and
the judge is trusted with exactly one thing, sized off its own measured noise.

## Dashboard

The store is already the right shape for one: append-only, timestamped, dimension-tagged, and never
rewritten. Nothing further needs to be produced by CI.

**Ingest.** Point BigQuery at the bucket as an external table over
`gs://<bucket>/<prefix>/*.jsonl` with `format = NEWLINE_DELIMITED_JSON`. No ETL job, no schedule,
no second copy — new objects appear in query results as soon as they are written. Promote to a
native table with a scheduled load only if query cost ever justifies it.

The key directories need no configuration: BigQuery's single `*` in a source URI matches across
`/`, so one wildcard covers the whole tree however deep it is filed. Hive partitioning is
deliberately **not** enabled — the segments are bare values rather than `key=value`, and every
dimension they encode is already a column on each row.

**Model.** Each line is already a fact row. The `key` object supplies the dimensions
(`setup_id`, `judge_model`, `scoring_version`, `fleet`, `verifiers`), `case` and `commit` the
grain, `recorded_at` the time axis. Read those from the row, never from the object path: the path
is an index and the record is the truth.

**Views worth having, in rough priority:**

| View                     | Reads                                        | Answers                                         |
| ------------------------ | -------------------------------------------- | ----------------------------------------------- |
| Pass rate over time      | `SUM(passes)/SUM(runs)` per case per week    | Is the agent getting better or worse?           |
| Judged mean over time    | `SUM(mean*n)/SUM(n)` per metric              | Is quality drifting below what rung 6 can see?  |
| Admission state timeline | Rolling 20-run window per case               | Which cases can actually block, and since when? |
| Flake rate               | Batches where `0 < passes < runs`            | Which cases are unreliable rather than broken?  |
| Infra health             | `SUM(blocked+infra)/SUM(runs+blocked+infra)` | Is the harness or the fleet the real problem?   |
| Time to admit            | First line to first admitted read, per key   | How long is the gate advisory after a bump?     |

**Annotate the version key.** Every chart should break or band at a key change. A quality series
plotted across a model bump is two different experiments drawn as one line, and it will be read as
a regression. This is the single most important property of the dashboard, and the reason the key
is stored on every row rather than inferred.

**Drift is visible here even though rung 6 cannot gate on it.** A weekly judged mean pools dozens of
runs, so its standard error is small enough to show a 0.05 slide that a three-repetition margin of
0.5 will never catch. The dashboard is therefore not a nicety — it is where drift detection
actually lives, with rung 6 as the collapse alarm underneath it.

**Presentation.** Looker Studio over the BigQuery view is the lowest-effort path and needs no
service to run. A static HTML page regenerated by a periodic job is the fallback if the data should
not leave the project.

## Open items

- The bucket and its `objectCreator` grant do not exist; `kube-agents-evals` IAM is owned
  elsewhere. Until then the GCS backend is dormant and the local backend is the default.
- No postsubmit Prow job exists for `hack/ci-eval-pr.sh` (job config lives in
  `kubernetes/test-infra`). Without one, nothing ever appends and no case is ever admitted.
- A lint that a behaviour change bumped `fleet` or `verifiers`.
- The GCS listing is unbounded while the fetch is capped. The reader lists the whole prefix and
  filters afterwards, because `BaselineStore.load` does not know which key it is about to be asked
  for and `bench-gate suite` reads many cases at potentially different keys. Scoping the listing to
  the key means threading it through both, which the layout now makes worth doing but which buys
  nothing at today's volumes; see
  [Reading is capped, and says so](#reading-is-capped-and-says-so).
- The `bench/tf/fleet` drift-reconcile schedule — a drifted fixture silently changes what a
  baseline means.
- Every threshold here is a starting point. The way to tune them is to run the suite against `main`
  a few dozen times, see how much it moves when nothing changed, and set the bars above that.
