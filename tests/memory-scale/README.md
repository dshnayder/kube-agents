# Memory scale test

The A/B that decided the memory provider: a synthetic 500-cluster fleet's worth of
shared knowledge, put in front of the same agent twice — once through Hindsight,
once through the file-based `multiuser_memory` provider it replaced.

The findings and what they argue for are in
[`docs/designs/memory.md`](../../docs/designs/memory.md). This directory is the
apparatus and the evidence: everything needed to re-run it or to check a number
in the design doc against its source.

## Layout

| Path            | What it is                                                                             |
| --------------- | -------------------------------------------------------------------------------------- |
| `harness/`      | Corpus generator, seeder, scorer, and the offline context-cost measurement             |
| `queries.json`  | The 26 scored probes — gold documents, `must_contain`, `must_not_contain`, probe class |
| `manifest.json` | What the generator produced: 1,664 records, per-category and per-rung counts, the seed |
| `fixtures/`     | The exact artefacts measured, so a re-run can be diffed against the original           |
| `jobs/`         | The Kubernetes Jobs that seeded, measured and audited against the live cluster         |
| `results/`      | Scorer output, one file per provider per rung                                          |
| `transcript/`   | One document: the probe protocol, the run's corrections and state, and the scoring     |

## The corpus

`harness/gen_fleet_corpus.py` writes eleven category files under `corpus/` from a
fixed seed (`20260731`, recorded in `manifest.json`), so the corpus is
reproducible rather than a one-off artefact:

| Category                   | Records | Identifier lives in |
| -------------------------- | ------: | ------------------- |
| `inventory`                |     450 | metadata only       |
| `user`                     |     250 | metadata only       |
| `gotcha`                   |     200 | metadata only       |
| `ownership`                |     180 | metadata only       |
| `migration`                |     130 | metadata only       |
| `capacity`                 |     110 | metadata only       |
| `postmortem` (`PM-`)       |      80 | **the prose**       |
| `adr` (`ADR-`)             |      68 | **the prose**       |
| `deprecation`, `exception` | 55 + 55 | metadata only       |
| `runbook` (`RB-`)          |      44 | **the prose**       |
| `convention`               |      42 | metadata only       |

That last column is load-bearing and was not designed in deliberately — it was
found while scoring. A record's id is either written into the sentence
(`"ADR-2026-044 (2026-01-28, current). Decision: …"`) or carried beside it as a
directive (`<!-- id: CONV-034 -->`). **193 of 1,664** records are in the first
group, and only those 193 identifiers survive into a flat file. See
[`docs/designs/memory.md`](../../docs/designs/memory.md) for why that decides the
comparison.

Of the 1,664, **1,414 are `scope: shared`** and 250 are per-user. The five rungs
(100 / 200 / 400 / 800 / 1414) are nested subsets of the shared set, which is
what makes the context-cost curve a curve rather than five unrelated points.

## Re-running it

Generate the corpus and check it against the manifest:

```sh
python3 harness/gen_fleet_corpus.py --out corpus/
```

Seed a Hindsight bank. **Use `--batch 1`.** The default is 5, and Hindsight
collapses a multi-item retain into one document keeping one item's `context` as
the label — which is how the first run destroyed four identifiers in five before
recall ever ran ([the correction](transcript/README.md#correction-the-delegated-baselines-citation-numbers-measure-the-seeder)):

```sh
python3 harness/seed_fleet.py --bank kube-agents-memory --rung 1414 --batch 1
```

Score a rung against either backend:

```sh
python3 harness/eval_fleet.py --backend hindsight --rung 1414
python3 harness/eval_fleet.py --backend file --rung 1414
```

Measure what the file provider costs in context, offline, with no cluster:

```sh
python3 harness/measure_file_based.py
```

That last one loads `fixtures/multiuser_memory.py` — the provider as it shipped
in image `dev-20260729-155133`, the last build containing it
(sha256 `095d916908a1ad3581225571bb4df22ddac41fe2273d4379cf9f08a0f606f415`,
10,330 bytes) — and calls its real `system_prompt_block()`. It is vendored here
because the plugin no longer exists in the tree, and the measurement is only
worth anything if it runs against the actual code.

## Fixtures

`fixtures/MEMORY.md.gz` is the file arm's shared store exactly as it sat on the
gateway PVC: 444,531 bytes, 1,414 entries separated by `\n§\n`,
sha256 `9c4b40a4c02cd3cca0d50c343386017aa67d7b66fbf0d24dad5f61580fd57952`.

```sh
gzip -dc fixtures/MEMORY.md.gz | shasum -a 256
```

It is kept rather than regenerated because it is the measured object: the
110,799-token figure in the design doc is `system_prompt_block()` over _this
file_, and a regenerated corpus that differed by a byte would quietly move the
number.

## Jobs

Every interaction with the live cluster went through a Job rather than
`kubectl exec`, mounting the gateway PVC. They are kept because several of them
are the evidence for a finding rather than just plumbing. The filenames carry the
run labels the transcript used before it was relabelled: `roundb-*` are the
**file arm's** controls, `rounda2-*` the **Hindsight arm's**.

| Job | What it establishes |

| ------------------------------- | -------------------------------------------------------------------------- |
| `seed-job-r*.yaml` | Seeded each rung; `roundb-write` wrote the file-provider store |
| `seed-job-r1414-batch1.yaml` | The corrected reseed, one record per retain call (#117) |
| `roundb-verify.yaml` | The real provider, loaded from the running image, against the real PVC |
| `roundb-packing*.yaml` | The seed-packing measurement, and its independent reproduction |
| `roundb-probe1{c,d}-*.yaml` | Which data path a specialist actually used, recovered from its own scripts |
| `roundb-cleanup2.yaml` | That the Hindsight boundary was closed — API and Postgres both refusing |
| `rounda2-sever-file-store` | The flat store's live sha256, matching the repository fixture |
| `rounda2-purge-file-store` | The flat store deleted, and the PVC survey: 72 corpus-bearing files |
| `rounda2-provision-bank` | A bare bank provisioned with the provider's own constants, read via `ast` |
| `rounda2-cleanup-answer-caches` | The doc-shaped caches removed; logs and databases deliberately kept |

The `rounda2-*` jobs are the mirror of the file arm's controls. The file arm
scaled Hindsight to zero so the file provider could not reach it; these delete
the flat store so the Hindsight arm cannot reach _that_. Both arms have to be
severed the same way or the A/B is not one.

The survey job is worth reading before writing another control. It found the
corpus cached across 72 files, the densest being the specialist's own
`agent.log` (294 identifiers) and `kanban.db` (119) — and those are kept, because
they are the evidence behind the improvisation-route findings and because the
Chat Agent has no file tools with which to reach them. Only the subset that reads
like documentation was removed. A control that destroys the audit trail is not a
better control.

`roundb-cleanup*.yaml` delete with a literal allowlist and no globs, which is
worth copying if you write another one against a shared volume.

## Caveats on the numbers

Stated here so nobody lifts a figure without them:

- **The delegated baseline's citation counts measure the seeder, not Hindsight.** The bank was
  seeded with the default `--batch 5`.
  [The correction](transcript/README.md#correction-the-delegated-baselines-citation-numbers-measure-the-seeder)
  has the measurement.
- **The answer-quality probes must be scored at the chat-agent layer.** The
  platform specialist has `memory_enabled: false` in both arms
  (`agents/platform/config.yaml`), so it carries no memory provider either way —
  it is a constant, not the variable under test.
- **`queries.json`'s substring checks are a first pass, not the verdict.** They
  score `"7 years"` as a miss against `must_contain: "seven years"`, and score a
  correct _"ADR-2025-036's 400 days is superseded"_ as a hit against
  `must_not_contain: "400 days"`. The scoring rules in
  [the probe protocol](transcript/README.md#protocol-the-ten-answer-quality-probes) govern.
