# Honcho against the Meridian bank tests

The same 26 probes, the same 1,664-document corpus (generator seed `20260731`,
so it is byte-identical to the run in [`../README.md`](../README.md)), scored by
the same code. Hindsight and file-based numbers are not re-run here — they are
the existing `results/hindsight-r*.json` and `results/file-based-r*.json`.

**Per-user isolation is deliberately not implemented for Honcho.** The two
isolation probes are expected to fail, and they do. What the run added is the
size of that failure, which was not the expected part.

## The ladder

| rung |   provider |  gold | must | contam | current-first | tok/turn | tag leaks |
| ---: | ---------: | ----: | ---: | -----: | ------------: | -------: | --------: |
|  100 |  hindsight | 0.718 | 0.79 |  0.407 |         0.833 |    4,588 |         0 |
|  100 | **honcho** | 0.952 | 1.00 |   0.87 |         0.857 |    4,353 |       476 |
|  100 | file-based | 1.000 | 1.00 |  0.722 |         0.429 |   13,780 |         0 |
|  200 |  hindsight | 0.718 | 0.79 |  0.407 |         0.833 |    4,544 |         0 |
|  200 | **honcho** | 0.937 | 1.00 |   0.87 |         0.857 |    4,383 |       294 |
|  200 | file-based | 1.000 | 1.00 |  0.722 |         0.429 |   24,661 |         0 |
|  400 |  hindsight | 0.718 | 0.79 |  0.407 |         0.833 |    4,468 |         0 |
|  400 | **honcho** | 0.937 | 1.00 |  0.833 |         0.857 |    4,383 |       174 |
|  400 | file-based | 1.000 | 1.00 |  0.722 |         0.429 |   46,633 |         0 |
|  800 |  hindsight | 0.718 | 0.74 |  0.407 |         0.833 |    4,322 |         0 |
|  800 | **honcho** | 0.937 | 1.00 |  0.833 |         0.857 |    4,384 |       145 |
|  800 | file-based | 1.000 | 1.00 |  0.722 |         0.429 |   79,616 |         0 |
| 1414 |  hindsight | 0.702 | 0.74 |  0.407 |         0.833 |    4,264 |         0 |
| 1414 | **honcho** | 0.937 | 1.00 |  0.833 |         0.857 |    4,403 |       143 |
| 1414 | file-based | 1.000 | 1.00 |  0.722 |         0.429 |  110,907 |         0 |

Context size is held equal by construction, not by luck: Honcho returns whole
documents where Hindsight returns short extracted observations, so the
evaluator takes hits in rank order until an 18,000-character budget is spent.
That budget was chosen to bracket the 4,264–4,588 tok/turn Hindsight actually
consumed at `budget=mid`. Comparing on result count instead would have handed
Honcho several times the context and made every other number meaningless.

## Read the recall number with the caveat attached

`gold_recall` 0.937 against Hindsight's 0.702 is **not** Honcho retrieving
better. Honcho's search returns messages verbatim, so the document ID always
survives into the context; Hindsight stores paraphrases and only a measured
45 of 82 units retain the source identifier, so a document whose substance
survived but whose ID was stripped scores as a miss. This is the same asymmetry
that gives file-based a mechanical 1.000.
The harness docstring already states it for file-based; it applies identically
here.

The metrics that survive the asymmetry are contamination and ordering.

## What the run actually found

### Honcho ranks the current answer first, then ships the stale one anyway

`current_ranked_first` is 0.857 for Honcho against 0.833 for Hindsight — the
ranking is fine, marginally better. Contamination is where they part:

| class            | honcho | hindsight |
| ---------------- | -----: | --------: |
| **procedural**   |  1.000 |     0.000 |
| **supersession** |  0.917 |     0.611 |
| isolation        |  0.500 |     0.000 |

At rung 1414 Honcho carries a superseded value into the context on **every
single procedural probe**, where Hindsight carries none. Retrieval does not
make old content disappear, and Honcho has no step that suppresses it: a
message about the retired runbook version is still a message, still embedded,
still a legitimate hit. Hindsight's extraction and consolidation pass is what
removes it — the cost being the paraphrasing that depresses its `gold_recall`.

So the two systems fail in opposite directions, and the choice is between them:
Hindsight loses citations to keep the context clean, Honcho keeps citations and
hands the model a contradiction.

### The isolation failure is far larger than the two isolation probes

The expected result was two failing probes. The measured result at rung 100 is
**476 cross-user tag leaks across 24 of the 26 probes**. Workspace-wide search
has no notion of who is asking, so any query at all can return another user's
personal message — at the small rung a leak is not something the isolation
probes provoke, it is the default behaviour of almost every query.

As the corpus grows the leaks concentrate rather than disappear:

| rung | leaks | probes affected | `Q-ISO-001` |
| ---: | ----: | --------------: | ----------: |
|  100 |   476 |         24 / 26 |         100 |
|  400 |   174 |         10 / 26 |         100 |
| 1414 |   143 |          3 / 26 |         100 |

The personal document count is fixed at 250 while the shared corpus grows to
1,414, so shared documents progressively crowd personal ones out of the
top-100 budget. That dilution cleans up the _incidental_ leaks on unrelated
probes. It does nothing whatever to the probe that actually asks: `Q-ISO-001`
("What do you know about my preferences?") returns 100 personal messages
belonging to other users at **every rung**, filling the entire budget. Run as
`probe-operator`, an identity with no documents of its own, every single result
is somebody else's.

So the headline number improving from 476 to 143 is an artefact of corpus
composition, not progress. Where isolation is tested directly it fails
identically at every scale.

Honcho does have the primitive to fix this — the Hermes provider already passes
`runtime_user_peer_name` from `user_id`
(`plugins/memory/honcho/__init__.py:477`), and the peer mapping in
`seed_honcho.py` puts every document on the right peer. What is missing is
query-time scoping. Note that the derived surfaces force the issue:
`conclusions/query` **requires** `observer` and `observed` filters and will not
run unscoped, so any move to the conclusions surface implements isolation as a
side effect.

### Settle time is real but not the bottleneck

Honcho derives asynchronously, so ingest returning is not the same as the
corpus being queryable. Measured separately from seeding, via
`GET /queue/status`:

| rung | messages written |  seed | drain after seeding | work units (cumulative) |
| ---: | ---------------: | ----: | ------------------: | ----------------------: |
|  100 |              350 | 1.7 m |               0.3 m |                     351 |
|  200 |              100 | 1.0 m |               0.4 m |                     453 |
|  400 |              200 | 1.0 m |               1.5 m |                     663 |
|  800 |              400 | 1.2 m |               4.3 m |                   1,085 |
| 1414 |              614 | 1.3 m |               6.0 m |                   1,731 |

Zero failed batches at any rung. These are **delta** figures — each rung writes
only what the previous one did not, so walking the whole ladder cost 6.2 m of
seeding and 12.5 m of draining, about 19 minutes end to end. A single-shot seed
of all 1,664 documents was not measured and is not the sum of these rows.

The drain column is the part with no Hindsight equivalent: Hindsight pays its
extraction cost inline during `retain`, so its seeding is slower and its
queryable-at wall clock is the moment seeding returns. The deriver ran
with `FLUSH_ENABLED = true` and `thinking_effort = "minimal"`, both of which
favour Honcho here — see [`README.md`](README.md).

Dreams (Honcho's consolidation analogue, and the mechanism most likely to
address the contamination result) did **not** run: the shipped defaults are
`MIN_HOURS_BETWEEN_DREAMS = 8` and `IDLE_TIMEOUT_MINUTES = 60`, so no ladder run
is long enough to trigger one. The contamination numbers above are therefore
pre-consolidation, and a fair follow-up would force a dream via
`POST /schedule_dream` and re-score.

## Reproducing

```sh
python3 harness/gen_fleet_corpus.py --out /tmp/scaletest/v2
kubectl -n kubeagents-system port-forward svc/honcho-api 18800:8000 &
bash harness/ladder_honcho.sh 100 200 400 800 1414
```

`ladder_honcho.sh` seeds, drains and scores each rung in turn. The rungs are
nested and the seeder resumes, so each rung writes only its delta.

## What this does not settle

- **Contamination after a dream.** The single most likely thing to change the
  verdict, and it was not measured.
- **The derived surfaces.** Only message search was scored, because it is the
  path the Hermes `honcho_search` tool takes and the only one that needs no
  per-peer scoping. Conclusions and the dialectic answer are untested at scale;
  the dialectic answered the smoke probe correctly, which is one data point.
- **Answer-layer quality.** Two probes are marked `scored_at: answer` and are
  recorded but unrated at this layer, exactly as in the Hindsight run.
- **Postgres availability.** Unchanged by this experiment. Honcho runs one
  Postgres, as Hindsight does.
