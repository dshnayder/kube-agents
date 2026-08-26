# Eval Baseline Storage

> **STATUS — design of record; partially implemented.** The record format, the version key,
> admission, reset and rung 6 are implemented in `bench/kube_agents_bench/` and covered by
> `bench/tests/`. The GCS backend is implemented and defaults **off**; it has been validated end
> to end against a real bucket in a personal dev project (see
> [What has been validated, and where](#what-has-been-validated-and-where)), but the production
> bucket and its IAM grants do not exist yet, and no postsubmit job writes to it. The dashboard's
> table and views are checked in as `bench/dashboard/` and have been run against that same bucket;
> what is not built is the Looker Studio front end over them.

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

### What the job's service account needs

The backend shells out to three `gcloud storage` verbs, and they do not all fall under one role:

| Path                               | Verb           | Permission               | Role                          |
| ---------------------------------- | -------------- | ------------------------ | ----------------------------- |
| `bench-gate case` / `suite` — read | `ls gs://…/**` | `storage.objects.list`   | `roles/storage.objectViewer`  |
| `bench-gate case` / `suite` — read | `cat`          | `storage.objects.get`    | `roles/storage.objectViewer`  |
| `bench-gate record` — append       | `cp - gs://…`  | `storage.objects.create` | `roles/storage.objectCreator` |

So the **postsubmit** needs both roles on the bucket; `objectCreator` alone cannot read back what
it wrote. Neither role carries `storage.objects.delete`, which is the property the whole layout
depends on, so the pair is still strictly weaker than `roles/storage.objectUser` or
`roles/storage.admin` — ask for the two named roles, not the convenient one.

**The presubmit needs `objectViewer` only.** A pull request is graded against the store and must
never write to it. That is already enforced twice in software — the `JOB_TYPE = postsubmit`
condition in `hack/ci-eval-pr.sh` and a refusal inside `bench-gate record` when `PULL_NUMBER` is
set — and if the two job types can run as different service accounts, withholding
`objectCreator` from the presubmit's makes it structural rather than conventional. That is the
strongest of the three guards, because it survives a careless edit to either of the others.

Both roles can be scoped to the prefix with an IAM condition on
`resource.name.startsWith("projects/_/buckets/<bucket>/objects/<prefix>/")`, so the bucket can hold
other things the eval job cannot touch.

Two bucket settings matter as much as the roles. **Uniform bucket-level access** should be on, so
IAM is the only access path and per-object ACLs cannot quietly widen it. And the bucket must carry
**no lifecycle deletion rule**: an expiry rule would delete evidence out from under admitted cases
and de-admit them for a storage-policy reason nobody would think to look for. That is one of the
four arguments against the Prow artifact bucket, and it applies just as much to a bucket of our
own.

### Provisioning it

```bash
BUCKET=kube-agents-evals-bench          # globally unique
PROJECT=kube-agents-evals
POST_SA=<postsubmit-sa>@${PROJECT}.iam.gserviceaccount.com
PRE_SA=<presubmit-sa>@${PROJECT}.iam.gserviceaccount.com

# No lifecycle rule and no versioning, deliberately: nothing may delete evidence,
# and nothing can overwrite it, so there are no versions to keep.
gcloud storage buckets create "gs://${BUCKET}" \
  --project="${PROJECT}" \
  --location=us-central1 \
  --default-storage-class=STANDARD \
  --uniform-bucket-level-access \
  --public-access-prevention

# The postsubmit reads and appends.
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${POST_SA}" --role=roles/storage.objectViewer
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${POST_SA}" --role=roles/storage.objectCreator

# The presubmit only reads. Withholding objectCreator is the guard that survives
# a careless edit to hack/ci-eval-pr.sh.
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${PRE_SA}" --role=roles/storage.objectViewer
```

To scope a grant to the prefix rather than the whole bucket, add a condition:

```bash
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${POST_SA}" \
  --role=roles/storage.objectCreator \
  --condition='title=evidence-prefix-only,expression=resource.name.startsWith("projects/_/buckets/'"${BUCKET}"'/objects/evidence/")'
```

Then point the job at it — `hack/ci-eval-pr.sh` defaults the variable to empty, so this is the
one-line change that turns the backend on:

```bash
export EVAL_BASELINE_STORE="gs://${BUCKET}/evidence"
```

Verify the grant is the one intended, rather than trusting the role names:

```bash
gcloud storage buckets get-iam-policy "gs://${BUCKET}" --format=json

# Overwrite must be refused. This is the guarantee the whole layout rests on.
OBJ="gs://${BUCKET}/evidence/<some-existing-object>.jsonl"
echo '{"tampered":true}' | gcloud storage cp - "${OBJ}" \
  --impersonate-service-account="${POST_SA}"
# expected: does not have storage.objects.delete access to the ... object
```

That last check is worth running rather than assuming, and the error text is the reason why: GCS
implements an overwrite as a delete plus a create, so it is `storage.objects.delete` that gets
refused — the permission neither role grants.

### What has been validated, and where

The backend has been exercised end to end against a real bucket
(`gs://dshnayder-gke-dev-evals-bench`, in a personal dev project, standing in for the one
`kube-agents-evals` will own). Confirmed live rather than against the test suite's fake `gcloud`:

| Claim                                                       | Result                                                            |
| ----------------------------------------------------------- | ----------------------------------------------------------------- |
| An empty prefix reads as an empty store, not an outage      | `[]`, no error                                                    |
| Objects file themselves under the version key               | path as specified above                                           |
| A `verifiers` bump starts a new directory, freezing the old | `…/v1-f1-v1/` and `…/v1-f1-v2/` side by side                      |
| `objectViewer` + `objectCreator` can list, read and create  | all three verbs succeed                                           |
| **Overwrite is refused**                                    | `does not have storage.objects.delete access`; object left intact |
| `objectViewer` alone cannot write                           | `does not have storage.objects.create`                            |
| Ten postsubmit appends accumulate to admission              | `admitted on 20/20 screening runs across 10 recorded run(s)`      |
| An admitted case that fails every repetition reds the suite | rung 4 collapse, `suite` exits 1                                  |
| A pull request cannot append                                | `refusing to record a baseline with PULL_NUMBER set`              |
| A missing bucket degrades rather than reds                  | 404 → advisory, with the banner in the markdown verdict           |

What remains unvalidated is the part no local run can reach: the postsubmit Prow job, which does
not exist yet.

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

Two independent reasons, and the weaker one is the one usually cited. The narrow reason is
self-admission: a case that wrote its own evidence could be admitted by the very diff that makes it
pass. The broader reason is that **a branch is not `main`.** The baseline answers "how does this
case behave on the merge target", and a branch's runs are a measurement of the branch — its
half-finished refactor, its deliberately-broken fixture, its expected-fail case that has not been
flipped yet. Those are all legitimate states for a branch and none of them are evidence about
`main`. So the filter is not a quality judgement about branches, which is why there is no notion of
a "good enough" branch that may contribute: the merge is what makes a run count, and nothing else
does.

### The job that writes it

This is the piece that does not exist yet, and nothing appends until it does. It is a change to
`kubernetes/test-infra`, not to this repo, which is why no amount of work here can close the loop.
What it has to be:

| Requirement                              | Why                                                                                                          |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| A **postsubmit** on `main`, not a periodic | The evidence must be attributable to a commit; `record` stamps each line with the `main` SHA it ran on.       |
| Sets `JOB_TYPE=postsubmit`, leaves `PULL_NUMBER` unset | Both guards key on exactly this. Prow sets them; anything hand-rolled must match.                  |
| Sets `EVAL_BASELINE_STORE` to the bucket | Unset, the append lands in the git checkout and dies with the workspace. This is what closes the loop.        |
| Runs as an SA with `objectCreator` **and** `objectViewer` | It appends, and it reads the store to compute its own verdict. Creator alone cannot read back. |
| **Not** `optional: true`, and not merge-blocking either | It runs after the merge, so it cannot block one. It should page someone when it fails, or the store silently stops filling. |
| Same `EVAL_REPETITIONS` as the presubmit | The baseline must be measured the way the thing compared against it is measured.                              |

Cost is the reason this is not simply "run it on every merge and forget it": at 3 repetitions per
case, a postsubmit is the same spend as a presubmit, on every merge, forever. If that proves too
expensive the lever is repetitions or a cron-style sampling of merges — **not** filtering which
merges count, which would reintroduce exactly the selection bias that recording unconditionally
exists to avoid.

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

It is **built and runnable**, not just described: `bench/dashboard/external-table.json` and
`bench/dashboard/dashboard.sql` create the table and all six views below, and both have been
executed against a real bucket. Substitute the project and bucket and run:

```bash
PROJECT=kube-agents-evals
bq --project_id=$PROJECT mk --dataset --location=us-central1 $PROJECT:eval_baselines
bq --project_id=$PROJECT mk --table \
  --external_table_definition=bench/dashboard/external-table.json \
  $PROJECT:eval_baselines.evidence
bq --project_id=$PROJECT query --use_legacy_sql=false < bench/dashboard/dashboard.sql
```

**Ingest.** Point BigQuery at the bucket as an external table over
`gs://<bucket>/<prefix>/*.jsonl` with `format = NEWLINE_DELIMITED_JSON`. No ETL job, no schedule,
no second copy — new objects appear in query results as soon as they are written. Promote to a
native table with a scheduled load only if query cost ever justifies it.

The key directories need no configuration: BigQuery's single `*` in a source URI matches across
`/`, so one wildcard covers the whole tree however deep it is filed. Hive partitioning is
deliberately **not** enabled — the segments are bare values rather than `key=value`, and every
dimension they encode is already a column on each row.

**Declare the schema; do not autodetect it.** This one is not a preference — `"autodetect": true`
produces a table that is quietly missing columns. `blocked` and `infra` are omitted when zero and a
judged metric is absent when the run did not produce it, so autodetect infers the shape from
whichever fields happen to appear in its sample and leaves out the rest. Querying `blocked` then
fails with `Unrecognized name: blocked` instead of returning zero, and a metric added later is
unqueryable until the table is recreated. The record format's absent-never-zero rule is deliberate
and correct; the consequence is that the **schema** has to be the thing that knows the full shape.
`bench/dashboard/external-table.json` declares it. Autodetect also types `recorded_at` as `STRING`
rather than `TIMESTAMP`, which silently breaks every date function downstream.

**Model.** Each line is already a fact row. The `key` object supplies the dimensions
(`setup_id`, `judge_model`, `scoring_version`, `fleet`, `verifiers`), `case` and `commit` the
grain, `recorded_at` the time axis. Read those from the row, never from the object path: the path
is an index and the record is the truth.

**The views**, all defined in `bench/dashboard/dashboard.sql`:

| View                | Answers                                                  |
| ------------------- | -------------------------------------------------------- |
| `pass_rate_weekly`  | Is the agent getting better or worse?                    |
| `judged_weekly`     | Is quality drifting below what rung 6 can see?           |
| `flakiness`         | Which cases are unreliable rather than broken?           |
| `infra_health`      | Is the harness or the fleet the real problem?            |
| `admission_state`   | Which cases can actually block a pull request right now? |
| `drift_under_green` | Which cases pass every run while quality slides?         |

`admission_state` deliberately mirrors what `baselines.py` computes at gate time — pool newest-first
at one key until the run bar is met, whole batches only — so the dashboard and the gate cannot
disagree about which cases are live. Reading it after a version bump shows every case falling back
to unadmitted until it is re-screened, which is the behaviour most likely to be reported as a bug.

`drift_under_green` is the one that justifies building this at all: it selects cases with a
**perfect pass rate** whose judged mean is lower at the end of the window than at the start. The
gate is green on every one of them by construction.

**Presentation.** Point Looker Studio at the dataset: _Create → Data source → BigQuery →_ the
`eval_baselines` views, then a time-series chart per view with `week` on the axis. Set
`version_key` as the **series breakdown** rather than a filter, so a model bump renders as a new
line beginning rather than a step in an existing one. A static HTML page regenerated by a periodic
job is the fallback if the data should not leave the project.

**Annotate the version key.** Every chart should break or band at a key change. A quality series
plotted across a model bump is two different experiments drawn as one line, and it will be read as
a regression. This is the single most important property of the dashboard, and the reason the key
is stored on every row rather than inferred.

**Drift is visible here even though rung 6 cannot gate on it.** A weekly judged mean pools dozens of
runs, so its standard error is small enough to show a 0.05 slide that a three-repetition margin of
0.5 will never catch. The dashboard is therefore not a nicety — it is where drift detection
actually lives, with rung 6 as the collapse alarm underneath it.

## Open items

- The bucket and its grants do not exist; `kube-agents-evals` IAM is owned elsewhere. Until then
  the GCS backend is dormant and the local backend is the default. See
  [What the job's service account needs](#what-the-jobs-service-account-needs).
- No postsubmit Prow job exists for `hack/ci-eval-pr.sh` (job config lives in
  `kubernetes/test-infra`). Without one, nothing ever appends and no case is ever admitted. What it
  has to look like is specified in [The job that writes it](#the-job-that-writes-it); what it costs
  is an open question for whoever owns the CI budget.
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
