# Memory TTL by distill-then-retire

**Status:** built and tested, then shelved. Nothing runs it — it is not on a
cron schedule and the script defaults to dry run. Kept because the problem is
real and will return; see [Why it is shelved](#why-it-is-shelved).

How the memory bank would be kept bounded without losing what it learned, why the
obvious approach — expire old facts — destroys more than it removes, and why the
non-obvious approach turned out to destroy things too.

Source of record:
[`agents/chat/scripts/memory_ttl_curator.py`](../../agents/chat/scripts/memory_ttl_curator.py).

## The problem

Hindsight never forgets. There is no TTL, no decay and no eviction anywhere in
its bank configuration or its API. A fact retained in 2026 is still a live row,
still an input to consolidation, and still occupying disk in 2036.

## Why plain expiry does not work

Hindsight keeps two layers in one table: the raw facts extracted from what was
said (`fact_type` `world`/`experience`), and the consolidated `observation` layer
the LLM maintains on top of them. Observations are what recall returns — the
provider asks for `types=["observation"]` and nothing else.

An observation records its provenance in `source_memory_ids`, a plain UUID array
with no foreign key, so there is no database cascade. The cascade is in
application code, and its contract is explicit
(`delete_stale_observations_for_memories`, in the engine's retain pipeline):

> For each observation referencing any of `fact_ids`: delete the observation row
> (**its text is stale once even one source memory disappears**), and reset
> `consolidated_at = NULL` on the surviving sources so they get re-consolidated.

Every removal path runs it — invalidating a fact, deleting a fact, deleting a
document, even a retain-time document replace. So _"retire the old evidence, keep
the conclusion"_ is not something the API can be asked for. Retire a fact and its
observations go with it, rebuilt afterwards from whatever facts remain. If
nothing remains, the knowledge is gone.

## The approach

**Distill, then retire.** Write the observation layer back down into the fact
layer _first_, as fresh facts, and only then retire the aged cohort. The old
observations are destroyed as always — and reconsolidated from those checkpoints,
which say the same thing with a current date. The intent: what the bank knows
survives, and only what it was holding as evidence does not.

Two properties fall out of that order:

- **Age stops being a correctness question.** On its own, age is a poor staleness
  signal: re-confirming a fact writes a _new_ row and bumps the observation's
  `proof_count` rather than refreshing the original's date, so a plain sweep
  retires claims that are still true. Under distill-then-retire that is harmless
  — the claim already survives in the checkpoint. Age only has to be a good guess
  about which _rows_ are redundant, not about which _facts_ are false.
- **Checkpoints are not privileged.** Each run retires the previous run's
  checkpoints once its own have landed, so exactly one generation is ever live.
  Without that, a weekly run against a six-month TTL would leave twenty-six
  near-identical copies of everything, inflating `proof_count` and crowding
  recall with its own exhaust.

## Checkpoints are verbatim

Checkpoints are written under the `checkpoint` retain strategy, which the
provider provisions on the bank at session start alongside `personal` and
`shared` (see
[`kube_agents_memory/__init__.py`](../../agents/chat/plugins/memory/kube_agents_memory/__init__.py)),
and which pins `retain_extraction_mode` to `verbatim`. Hindsight's default extraction would
re-summarise the observation, and re-summarising a summary every cycle is a game
of telephone that walks the bank away from what was actually said. In `verbatim`
mode `_collapse_to_verbatim` overwrites the fact text with the raw chunk, so one
checkpoint in is one fact out, unchanged. The LLM still runs — it attaches
entities and dates — so this is not free, and the run is paced accordingly.

`apply_strategy` silently falls back to `concise` (which paraphrases) for an
unknown strategy name, so the curator refuses to run against a bank whose config
lacks `checkpoint` rather than quietly producing rewritten checkpoints.

## Checkpoints keep their scope

Recall matches tags with `any_strict`, which returns tagged rows _only_. A
checkpoint written without its scope tag is therefore not merely mis-filed: it
consolidates into an untagged observation that no recall will ever match, so the
distil reports success and the knowledge disappears at the next retire.

Each checkpoint carries its source observation's full tag set and pins
`observation_scopes` to the one scope tag among them. An observation that carries
no scope tag — or more than one — aborts the run before anything is written.

## Marked by `context`

Checkpoints are identified by their `context` string, not by a tag and not by a
document id.

Not a tag, because tags are what consolidation is scoped by: a marker tag on
every checkpoint would put checkpoint-derived observations in a different scope
from the live facts on the same subject, so the two would never merge and the
bank would carry two parallel accounts of everything. Not a document id, because
the caller-supplied one is not what comes back — a unit's `document_id` is a
server-generated UUID. `context` round-trips verbatim, which is what the
post-write verification needs.

## The algorithm

```
0. GUARD     skip unless total units >= --min-units
             skip unless "checkpoint" is in the bank's retain_strategies

1. READ      candidates   = world|experience facts, state=valid     # BEFORE any write
             aged         = candidates whose mentioned_at < now - --ttl-days
             observations = the observation layer
             abort if any observation has no single scope tag
             superseded   = candidates whose context is the checkpoint marker
             doomed       = aged ∪ superseded

2. DISTIL    retain one verbatim checkpoint per observation, synchronously,
             tagged with the observation's tags and scoped to its scope tag

3. VERIFY    landed = |checkpoints now| − |superseded|
             abort before retiring anything if landed < |checkpoints sent|

4. RETIRE    invalidate each doomed unit, with a reason recording why

5. REBUILD   trigger consolidation
```

Reading `candidates` before writing anything is what makes step 4 safe: every
checkpoint in that list belongs to an earlier generation by construction, so no
timestamp comparison is needed and there is no exposure to clock skew between the
pod and the Hindsight service. Step 3 is a delta between two listings for the
same reason.

Age uses `mentioned_at` (ingestion), not `date` (the event the fact is about): a
fact can describe something from years ago and still have been learned yesterday,
and `date` may be absent entirely for content retained as timeless.
`memories/list` has no date filter, so the curator pages and filters client-side.

Step 2 checkpoints _every_ observation rather than only the affected ones,
because the list API does not expose `source_memory_ids`. The observation layer
is the compact one, so this is cheap.

## Why it is shelved

The end-to-end run took the observation layer from 22 rows to 10 to 2 over three
cycles. Each cycle rebuilds that layer by re-consolidating, and re-summarising a
summary compounds: the layer does not settle on a stable digest, it keeps
shrinking.

The checkpoints survive all of it — they are facts, written verbatim, and nothing
in the run touches them. But recall reads observations only, so a bank whose
observation layer has collapsed to two rows answers as though it had forgotten
almost everything while every checkpoint sits unread in the fact layer. The
mechanism is safe for the _store_ and lossy for what the agent can actually
_reach_, which is the opposite of what it was built for.

Two repairs were considered and both rejected:

- **Make recall read the fact layer too.** Adding `world`/`experience` to the
  provider's recall types makes the checkpoints reachable and the shrink
  harmless. It also puts raw facts in front of the model on every turn, forever,
  to compensate for a weekly job — a permanent cost in the hot path for a problem
  the hot path does not have.
- **Keep the curator, drop only the retire step.** Distillation on its own
  destroys nothing, but it also never clears the previous generation of
  checkpoints, so it is pure growth — the twenty-six near-identical copies that
  "The approach" names as the reason retirement had to be paired with
  distillation in the first place.

So nothing runs it. Hindsight has no TTL, but the bank holds facts in the tens:
unbounded growth is a problem this deployment does not have yet, and retirement is
the only part of the curator that destroys anything. What has to be settled before
it runs is what recall reads — that is what decides whether retiring the evidence
loses anything.

## Operating it

Dry run is the default; `--commit` (or `MEMORY_TTL_COMMIT=1`) makes it write.
Nothing invokes it — there is no cron entry — so any run is one an operator starts
deliberately and watches. `--ttl-days` (default 180) sets the cutoff and
`--min-units` (default 200) keeps the curator off a bank too small to have a
crowding problem; at the current size that guard alone makes it a no-op. Output
goes to stderr only.

Nothing here is irreversible. Invalidation moves a row to an archive table with
its reason and its causal edges recorded, and `PATCH {"state": "valid"}` puts it
back — one row at a time; there is no bulk revert.

## Deliberate non-goals

- **No ranking-time decay.** Both plugin recall paths flatten results to text
  before the wrapper sees them, so scoring changes would mean reimplementing both
  inside the wrapper — the fork the wrapper exists to avoid.
- **No observation-layer TTL.** Observations are derived, and the API refuses to
  curate them ("Only world/experience facts can be curated"). Consolidation
  rewrites them in place, so the layer is self-limiting — aggressively so, per
  [Why it is shelved](#why-it-is-shelved). It needs no curation of its own.
- **No fix for the compression itself.** Consolidation's summarisation ratio is
  Hindsight's, set by a prompt this deployment does not own. Nothing here changes
  it.
- **Recall precision is a separate problem.** The lever there is tags, not age;
  see [tag isolation](memory-tag-isolation.md).
