# Capability-scoping A/B

The experiment behind [`docs/designs/context-scoped-capabilities.md`](../../../docs/designs/context-scoped-capabilities.md)
§7. It asks one question: when the Platform Agent sees only the skills a turn needs, with the
rest by name, does it pick the right one more often and cost less than when it sees everything?
The design document reads the results; this directory holds the method, the code, and under
`results/` the scored tables (`<model>.md`), the raw metrics (`<model>.json`) and one row per run
(`<model>.csv`) that `make_results.sh` writes from a run directory.

## Arms and rungs

An **arm** is a harness configuration; a **rung** is a catalogue size. Every cell is the same 22
probes ([`scenarios.json`](scenarios.json)), three repetitions each, one prompt per session, on the
same install, image and model.

| Arm          | Skill index in the system prompt              | Per-turn injection                          | Tool array                    |
| ------------ | --------------------------------------------- | ------------------------------------------- | ----------------------------- |
| `stock`      | every skill, description cut at 60 characters | none                                        | as shipped                    |
| `fulldesc`   | every skill, full description                 | none                                        | as shipped                    |
| `scoped-all` | every skill by name only                      | top-6 skills by BM25 with full descriptions | pinned set plus top-6 by BM25 |

| Rung      | Catalogue                                                                                                                                                    |
| --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `shipped` | the 44 skills in `agents/platform/skills/`                                                                                                                   |
| `grown`   | those plus 60 real skills from `google/skills` `skills/cloud/` that the sync does not ship (5 `gke-*` it will ship next run, 55 other Google Cloud products) |

`stock` versus `fulldesc` isolates the description fix from scoping; `fulldesc` versus `scoped-all`
isolates scoping. The design document calls the `scoped-all` arm `scoped`. A `scoped-skills` arm
(skills only, tools untouched) exists in `switch_arm.sh` but is not in the default matrix: under
the API server the profile exposes 21 tools and the filter hides one (`web_extract`, in every
recorded turn), so it would not differ from `scoped-all`. The measured `scoped-all` arm carried no
shelf for hidden tools; the plugin gained one after the run.

## What is measured

From each run's response and session row, and from the plugin's per-turn record:

- **First skill loaded**: gold, acceptable, wrong, or none, per probe. Gold and acceptable are in
  `scenarios.json`; on the grown rung an added upstream skill that is a defensible answer counts as
  acceptable (`acceptable_grown`).
- **Any gold loaded** during the run, and **spurious loads** on the two control probes.
- **Working-set misses**: on every arm, skill loads whose skill was not in the ranker's top-6.
  On the stock and full-description arms that is the shadow measurement phase 0 asks for; on the
  scoped arm it is the case the names-only shelf had to rescue.
- **Tokens**: input and output per run, cache-read share, prompt tokens of the first model call.
- **Time**: wall time per run and time to first tool call.
- **Incomplete runs**: the profile is capped at 10 model iterations per turn
  (`spec.harness.tuning.platform.maxTurns: 10` on the CR; the config value wins over the
  `HERMES_MAX_ITERATIONS` environment variable, which the API server re-derives from it). A run
  that hits the cap without a final message returns 502 `agent_incomplete` with its session id;
  it is kept for the selection metrics and counted.

`analyze.py` prints the table; `--json` gives the raw numbers.

## Results

The matrix ran on 2026-09-21 on Gemini 3.1 Pro and again on Gemini 3.5 Flash: 792 runs. Each
probe asks for something one skill is written for, its gold skill. A run is scored by the first
skill the agent read with `skill_view`. The full tables, per-probe picks, and the reading of them
are in `results/` and in the design document's §8.

Both models pooled, 120 runs per cell. The p-values are a two-proportion test of `scoped-all`
against the arm named.

| Share of runs where the agent...  | stock | fulldesc | scoped-all | vs stock  | vs fulldesc |
| --------------------------------- | ----- | -------- | ---------- | --------- | ----------- |
| 44 skills, read gold skill first  | 62.5% | 68.3%    | 72.5%      | p = 0.10  | p = 0.48    |
| 44 skills, read no skill          | 35.0% | 29.2%    | 20.8%      | p = 0.014 | p = 0.14    |
| 104 skills, read gold skill first | 59.2% | 60.8%    | 75.8%      | p = 0.006 | p = 0.013   |
| 104 skills, read no skill         | 34.2% | 25.8%    | 17.5%      | p = 0.003 | p = 0.12    |

Cost per first model call, Gemini 3.1 Pro medians:

| Prompt tokens, first model call | stock | fulldesc | scoped-all |
| ------------------------------- | ----- | -------- | ---------- |
| 44 skills                       | 28.4k | 31.3k    | 28.3k      |
| 104 skills                      | 30.4k | 38.6k    | 29.7k      |

- With 44 skills, scoping reliably stopped the agent skipping skills. Its gain in picking the gold
  skill first points the same way but is not significant at this sample.
- With 104 skills, scoping raised gold-skill-first picks by about a sixth against both other arms.
  Full descriptions for every skill did not.
- Wrong picks were rare in every arm. One control run out of 72 read a skill it should not have.
- Scoping cost what `stock` costs. Full descriptions added 3k to 8k tokens a call and doubled the
  time to the first tool call.
- Picks were scored, not outcomes, every probe was one turn, and the filter hid only one tool.
  The design document's §8.7 lists what the run does not show.

## How it runs

The prototype is the `capability_scope` plugin (`agents/platform/plugins/capability_scope/`) plus
`deploy/docker/patches/apply_capability_scope.py`, both in the tree and both inert unless the
`KA_*` variables are set and the plugin is enabled. One image serves every arm.

1. Build the image from this branch and install kube-agents with it (the run used a dedicated
   cluster with `platformFrontDoor: true`, so the API server serves the Platform Agent directly,
   and `--memory=off`). Building needs the hermes-otel plugin install in the Dockerfile pinned to
   a manifest-v1 commit (`b7ece46a5c883a619686b6c698b0316d9fd50962`); this branch does not carry
   that pin, so apply it in the build tree first.
2. Copy the distractor skills onto the agent's data volume at `/opt/data/distractor-skills`.
3. Enable the plugin in the profile's `plugins.enabled` list on the data volume; it is not enabled
   in the image. In front-door mode the profile's `config.yaml` on the data volume is not re-synced
   from the image, so edit the file there once.
4. `run_all.sh <out-root> 3 3` keeps a port-forward to the agent API alive and runs
   `run_matrix.sh`, which for each cell calls `switch_arm.sh <arm> <rung>` and then `run_ab.py`.

`switch_arm.sh` writes the arm into `/opt/data/capability_scope.env` and restarts the gateway;
the plugin loads that file into the environment at start-up. The operator does not pass
`spec.deployment.env` to the gateway container, which is why the arm lives in a file.

The first attempt ran uncapped: probes ran up to 52 tool calls and 430 seconds, one consuming
1.7M cached tokens, and the cell pace was two hours. The cap was added and the matrix restarted
from zero.

Every probe is prefixed with the same sentence ("Work only on the cluster you run on and keep
the investigation brief.") on every arm, so a probe about a workload that does not exist does not
turn into a sweep of every cluster in the project.

## Limits of the setup

- Two models (Gemini 3.1 Pro, then a repeat on Gemini 3.5 Flash), one persona, one operator
  scoring. Enough to decide direction; not a benchmark.
- The ranker is v1: BM25 over skill name and description with a crude stemmer. It has no trigger
  phrases or domain tags yet, so its misses are part of what is measured, not tuned away.
- Probes are single-turn, so the sticky working set and the turn-boundary rule are exercised but
  not tested.
- The grown rung adds real skills but their names and descriptions were written for other
  products; a catalogue that grew through the sync would carry more `gke-*` near-duplicates.
