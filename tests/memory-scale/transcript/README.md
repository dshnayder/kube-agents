# Experiment transcript

The raw record of the memory scale test: the probe protocol, the corrections the
run made to itself, the file arm's isolation log, and the per-probe scoring.

This is evidence, not narrative. What it argues for is in
[`docs/designs/memory.md`](../../../docs/designs/memory.md); how to re-run any of
it is in [the harness README](../README.md).

**Three runs, and only two of them are the comparison.** Each section below is
labelled with the run it belongs to:

| Run                    | Provider   | Delegation                          | Bank        | Is it an arm?                                                         |
| ---------------------- | ---------- | ----------------------------------- | ----------- | --------------------------------------------------------------------- |
| **File arm**           | file-based | suppressed, at the chat-agent layer | —           | yes                                                                   |
| **Hindsight arm**      | Hindsight  | suppressed, at the chat-agent layer | `--batch 1` | yes                                                                   |
| **Delegated baseline** | Hindsight  | delegated to a specialist           | `--batch 5` | no — the specialist carries no provider, so it measures improvisation |

The two arms ran the same ten probes and are the head-to-head. The delegated
baseline is kept because what it measures — an agent with no memory answering
from whatever it can scavenge — turned out to be the finding behind several of
the open work items, not because it compares to anything.

**Complete, with one gap.** The delegated baseline's probes 1–3 were scored
inline during the run and are recovered in the design doc rather than here. Its
probes 4–10 sit physically between the file arm's run-state section and the file
arm's probes, in the order they were run.

## Contents

- [Protocol: the ten answer-quality probes](#protocol-the-ten-answer-quality-probes)
- [Correction: the delegated baseline's citation numbers measure the seeder](#correction-the-delegated-baselines-citation-numbers-measure-the-seeder)

File arm:

- [File arm: run state, isolation, and rollback](#file-arm-run-state-isolation-and-rollback)
- [File arm, probes 5 and 6 (delegated): void, and the route that persists](#file-arm-probes-5-and-6-delegated-void-and-the-route-that-persists)
- [File arm, probe 5: the first measurement of the file provider](#file-arm-probe-5-the-first-measurement-of-the-file-provider)
- [File arm, probe 6: no errors, and the depth reading falls over](#file-arm-probe-6-no-errors-and-the-depth-reading-falls-over)
- [File arm, probe 7: the trap, refused correctly](#file-arm-probe-7-the-trap-refused-correctly)
- [File arm, probe 8: the fact Hindsight lost outright](#file-arm-probe-8-the-fact-hindsight-lost-outright)
- [File arm, probe 9: the second trap, and an aggregation the file wins](#file-arm-probe-9-the-second-trap-and-an-aggregation-the-file-wins)
- [File arm, probe 10: the negative probe the flat file is equipped for](#file-arm-probe-10-the-negative-probe-the-flat-file-is-equipped-for)
- [File arm: interim summary after six probes](#file-arm-interim-summary-after-six-probes)
- [File arm, probe 1: the supersession chain, and a caveat on 0.429](#file-arm-probe-1-the-supersession-chain-and-a-caveat-on-0429)
- [File arm, probe 2: ingress controller](#file-arm-probe-2-ingress-controller)
- [File arm, probe 3: audit log retention](#file-arm-probe-3-audit-log-retention)
- [File arm, probe 4: base image](#file-arm-probe-4-base-image)
- [File arm: all ten probes](#file-arm-all-ten-probes)

Hindsight arm:

- [Hindsight arm: the ten probes, non-delegated](#hindsight-arm-the-ten-probes-non-delegated)

Delegated baseline (not an arm):

- [Delegated baseline, probe 4: base image](#delegated-baseline-probe-4-base-image)
- [Delegated baseline, probe 5: cluster backup](#delegated-baseline-probe-5-cluster-backup)
- [Delegated baseline, probe 6: node pool shape](#delegated-baseline-probe-6-node-pool-shape)
- [Delegated baseline, probe 7: etcd runbook](#delegated-baseline-probe-7-etcd-runbook)
- [Delegated baseline, probe 8: leaked credential](#delegated-baseline-probe-8-leaked-credential)
- [Delegated baseline, probe 9: nonexistent cluster](#delegated-baseline-probe-9-nonexistent-cluster)
- [Delegated baseline, probe 10: nonexistent ADR](#delegated-baseline-probe-10-nonexistent-adr)

## Protocol: the ten answer-quality probes

The ladder measures **what the provider puts in front of the model**. This
measures **what the model then says**, which is the only place two of the eight
probe classes can honestly be judged at all: whether the agent invents a cluster
that does not exist is a property of its reply, not of the retrieved text.

Ten probes, sent twice — once against each provider. Twenty DMs total.

#### Why these ten and not all twenty-six

The other sixteen are already decided at the context layer and re-asking them by
hand would add transcription risk without adding signal. These ten are the ones
where the reply can differ from the context:

- **Six supersession probes.** The corpus contains three dated versions of each
  of six policies. Both providers put more than one version in front of the
  model — this is measured, not assumed — so the answer depends on which one the
  model believes. This is the core of the test.
- **Two negative probes.** Only judgeable from the reply.
- **Two procedural probes.** Long ordered runbooks; the failure mode is a
  plausible reordering, which no substring check catches.

#### How to run it

The delegated baseline was actually run **twice**, under two presentations, and
the report has to say which one each number came from:

| presentation    | where       | what was sent                     |
| --------------- | ----------- | --------------------------------- |
| single prompt   | DM          | all ten probes in a single prompt |
| one per message | group space | one probe per message             |

**One per message is the parity target for the file arm**, which is how probe 1
was sent. The single prompt is a second measurement of a different thing — one
recall serving ten topics — not a replicate, and the two must not be pooled.

Send each question in the **group space, to `@kage`** — not a DM. The earlier
instruction here said DM and was wrong, for two reasons. The delegated baseline was run in the
group space, so a DM would break parity. And `multiuser_memory._session_is_shared`
(lines 106-118) suppresses the personal store only when `chat_type != "dm"`: in a
DM it would inject
`/opt/data/memories/users/dmitryshnayder_google.com_ab4bc33e9a14.md`, 1,594 bytes
of real personal memory that the delegated baseline never saw. Send them in the order below and
paste the replies back; I will score them against the answer key.

Do not preface the questions with context or combine them into one message. Each
probe must arrive as a cold question, because that is the situation being
measured: someone asking the fleet assistant a question two years into the
fleet's life.

##### Hindsight — the current image

Nothing to change; this is what is deployed now.

##### File-based — the previous image

Requires rolling the gateway to `dev-20260729-155133`, which is the last image
containing `multiuser_memory`. Verified by listing the plugin directory inside
both candidate images, and the provider file in it is byte-identical to the
source used for the offline measurement:

```
sha256  095d916908a1ad3581225571bb4df22ddac41fe2273d4379cf9f08a0f606f415
bytes   10330
```

The corpus also has to be written to that image's on-disk store as
`MEMORY.md` + `users/<user>.md`. That is a separate step and it is disruptive to
everyone using the gateway, so it happens last, in one window, and rolls back
immediately after.

---

#### The probes

| #   | id                 | what a wrong answer looks like                            |
| --- | ------------------ | --------------------------------------------------------- |
| 1   | Q-SUP-ADR-2026-052 | quotes the 90-day key rotation ADR-2026-052 banned        |
| 2   | Q-SUP-ADR-2026-047 | recommends ingress-nginx                                  |
| 3   | Q-SUP-ADR-2026-044 | says 90 or 400 days for audit logs                        |
| 4   | Q-SUP-ADR-2026-051 | says distroless or debian-slim                            |
| 5   | Q-SUP-ADR-2026-049 | says Velero                                               |
| 6   | Q-SUP-ADR-2026-046 | says docker runtime / no mention of Arm                   |
| 7   | Q-PROC-RB011       | says to delete data, or gives the steps out of order      |
| 8   | Q-PROC-RB019       | anything other than revoke-first                          |
| 9   | Q-NEG-001          | describes the cluster instead of saying it does not exist |
| 10  | Q-NEG-002          | summarises an ADR that was never written                  |

Copy-paste block:

```
What is our current policy on service account keys?

Which ingress controller should a new service use?

How long do we retain audit logs?

What base image should a new service use?

How do we back up a production cluster?

What node pool shape should a batch or ML inference workload use?

Walk me through responding to the etcd database size alert, in order.

A credential has leaked. What do I do?

What is mfs-prod-euw1-99 used for?

What did we decide in ADR-2026-099?
```

#### Scoring

Mechanical, against `queries.json`, so the judgement is not mine after the fact:

- **supersession** — the current value present _and_ every superseded value
  absent. Naming the current policy while also mentioning the old one as history
  is a pass; presenting a superseded value as current is a fail.
- **procedural** — every required step present, in the order the runbook gives.
- **negative** — a pass is declining to answer or saying it has no record. Any
  description of the nonexistent cluster or ADR is a fail, however hedged.

## Correction: the delegated baseline's citation numbers measure the seeder

Found 2026-08-01 while scoring file-arm probe 1. This invalidates one of the delegated baseline's two headline
numbers and reverses the fix I filed for it.

#### What I claimed

> `document_id` is populated on 0 of 364 recalled units … roughly one citation in four is wrong, and
> that rate does not move. It is a property of the binding step, not of the retrieval.
> — [probe 6](#delegated-baseline-probe-6-node-pool-shape), [probe 10](#delegated-baseline-probe-10-nonexistent-adr)

Filed as **#111 — populate `document_id` on recall**.

#### What is actually true

`seed_fleet.py` sends documents to `/memories/retain` in batches of five (`--batch 5`, line 205).
Hindsight collapses a multi-item retain into **one document** and keeps **one** item's `context` as
that document's label. 1,664 corpus records became **335 documents**.

Measured directly against the bank export the platform agent itself used
(`/opt/data/docs/standards/corpus/all_docs.json`, 335 documents):

| measurement                                                | value                                                                          |
| ---------------------------------------------------------- | ------------------------------------------------------------------------------ |
| exported documents                                         | 335                                                                            |
| distinct record ids appearing in document bodies           | 193                                                                            |
| distinct record ids used as a document title               | 193                                                                            |
| documents whose title id is **absent from their own body** | **156 of 335**                                                                 |
| documents with **no id at all** in the body                | **278 of 335**                                                                 |
| ids per document body — mean / median                      | **0.70 / 0**                                                                   |
| distinct ids in bodies / distinct ids used as a title      | 193 / 193                                                                      |
| ids in **both** roles                                      | **37** — so 156 ids appear only in a body and can never be returned as a label |

Reproduced 2026-08-01 02:38 (Job `kube-agents-roundb-packing2`) against a second, independently
fetched export of the same bank — 554,681 bytes against the first export's 694,552, the difference
being field count (5 keys vs 13), not content. Every figure above is identical to the digit.

Correcting my own first note on this: I recorded "ids that appear in a body but never title a
document: 0". The correct value is **156**. The 0 I wrote belongs to the _last_ line of the job's
output, which filters to DEP/CONV/OWN prefixes only. The error understated the finding — 156 record
identifiers cannot be produced as a citation by any recall, because nothing in the bank carries them
in that role.

The decisive case is the one that mis-cited probe 1 in both rounds:

```
document title : Meridian platform deprecation DEP-001 (kube-agents fleet test)
ids in its body: ['ADR-2026-047', 'ADR-2026-051', 'ADR-2026-052']
'DEP-003' literal present in body: False
```

Those three ADRs are the bodies of **DEP-001** (ingress-nginx, ADR-2026-047), **DEP-002**
(debian-slim, ADR-2026-051) and **DEP-003** (key-rotation job, ADR-2026-052) — three separate corpus
records packed into one document carrying one label, `DEP-001`.

So when the agent said _"the deprecation is DEP-001, not DEP-003 — DEP-003 doesn't exist"_, it was
**reporting its source correctly**. `DEP-003` genuinely does not exist in the bank as it was seeded.
The record does; its identifier was discarded at retain time.

#### Consequences

1. **The ~25% attribution error rate is not a Hindsight measurement.** Four of every five record ids
   were destroyed before recall ever ran. Any citation landing on a pack-mate is a harness artifact.
   Every mis-citation in the delegated baseline that I traced to "near-duplicate individuation failure" —
   DEP-001/DEP-003, DEP-011, DEP-051, CONV-001 for CONV-002, CONV-007 for CONV-011, OWN-002 for
   OWN-003 — is off by less than a pack width.
2. **#111 is the wrong fix and would have made things worse.** Populating `document_id` returns the
   _packed_ document's id authoritatively. The agent would have cited `DEP-001` with a provenance
   field vouching for it. The fix is one record per retain call, plus a per-unit source field —
   not a document-level id.
3. **The near-duplicate hypothesis is unproven, not disproven.** 52 of 55 DEP records really do share
   a boilerplate sentence, and that really could defeat individuation. It just was not what caused
   these errors, so it remains untested.

#### What still stands

Nothing here touches the substance results, which never depended on document labels:

- supersession **6/6**, procedural **1/2**, negative **2/2** — scored on content, not citations
- the **enumeration ~100% vs attribution ~75%** split — the mechanism is now _better_ explained:
  enumeration reads content, attribution reads a label that was wrong 4 times in 5
- probe 8's harmful substance error ("Rotation is still mandatory")
- the category error and provenance gap (**#116**)
- the verification-layer inversion (**#113**) — if anything strengthened: probe 1's chat agent said
  DEP-003 (right) and the verification pass "corrected" it to DEP-001 (wrong) with an assertion
  harness that went green

#### Required rework

- Reseed the bank with **`--batch 1`** so one corpus record is one document, then re-run the delegated baseline's
  ten probes. Only the citation numbers are in question, but they cannot be salvaged by re-scoring.
- Retire **#111** as written; replace with "one record per retain call" plus per-unit provenance.

## File arm: run state, isolation, and rollback

Opened 2026-08-01 ~00:58 UTC.

#### What changed (and only this)

| #   | change                                                                                                   | command                                                                                                                                                                                        | reverse                                                    |
| --- | -------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| 1   | `hindsight-api` scaled to 0 — contamination control, so the file provider provably cannot reach the bank | `kubectl -n kubeagents-system scale deploy/hindsight-api --replicas=0`                                                                                                                         | `--replicas=1`                                             |
| 2   | shared corpus written to the gateway PVC                                                                 | Job `kube-agents-roundb-write` (ConfigMap `kube-agents-roundb-memory`)                                                                                                                         | delete `/opt/data/memories/MEMORY.md`                      |
| 3   | CR rolled to the last image containing `multiuser_memory`, provider switched                             | `kubectl -n kubeagents-system patch platformagent platform-agent --type=merge -p '{"spec":{"deployment":{"tag":"dev-20260729-155133"},"harness":{"memory":{"provider":"multiuser_memory"}}}}'` | patch back to `dev-20260730-200423` / `kube_agents_memory` |

Postgres, the `data-hindsight-postgresql-0` PVC and the bank contents were not touched.
There was **no pre-existing `/opt/data/memories/MEMORY.md`** — the write created it, so rollback is a
delete, not a restore. (`memories/users/` existed and was left alone.)

#### Isolation had to be tightened twice — both probe-1 attempts were void

Scaling `hindsight-api` to 0 was not an isolation boundary. Two attempts at probe 1 both answered
from Hindsight rather than the file store, by two different routes:

| attempt | route it actually used                                                                     | how it was caught                                                                                                                            |
| ------- | ------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------- |
| 1       | `/opt/data/docs/standards/corpus/all_docs.json` — a delegated-baseline leftover on the PVC | its own `verify_sa_key_answer.py` opens that path; 0 references to `MEMORY.md`/`grep`/`1414` across its artefacts                            |
| 2       | **direct SQL to `hindsight-postgresql`**, bypassing the down API                           | it said so unprompted: _"the hindsight-api deployment has been at 0/0 replicas for ~12h; it read the corpus straight from Postgres instead"_ |

Attempt 2 also recreated `/opt/data/docs/standards/` and rebuilt the delegated baseline's exact harness by name
(`fetch_docs.py`, `q.py`, `verify_chains.py`, `verify_sa_key_answer.py`, `mutate_sa_key_answer.py`).

Fix, authorised by the user 2026-07-31:

```sh
kubectl -n kubeagents-system scale statefulset/hindsight-postgresql --replicas=0
```

PVC `data-hindsight-postgresql-0` is preserved (Bound, 8Gi) and its contents are due to be destroyed
by #117 anyway, so this costs nothing. Verified from inside the cluster by Job
`kube-agents-roundb-cleanup2`:

```
refused    hindsight-api...:8888              (ConnectionRefusedError)
refused    hindsight-postgresql...:5432       (ConnectionRefusedError)
no dns     hindsight-postgresql-hl...:5432
no dns     hindsight-postgresql-0...-hl...:5432
boundary closed: True
```

**This is a finding, not just plumbing.** A specialist with no configured memory provider does not
degrade gracefully — it improvises a data path and reports the result with full confidence and real
identifiers. Twice, by different means. That is the empirical case for #112 being _injected context_
rather than a tool the specialist may elect to call.

#### Standing cleanup authorisation (user, 2026-07-31)

Pre-authorised for the remainder of this test, no further asking:

- `/opt/data/docs/standards/` — the specialist recreates it on every probe
- `/opt/data/kanban/attachments/t_*` **for a probe that has been scored or voided**

Everything else stays off-limits, explicitly including `/opt/data/memories/` (both `MEMORY.md` and
`users/dmitryshnayder_google.com_ab4bc33e9a14.md`) and the nine pre-test attachment directories
`t_0e6886c4 t_1c43868e t_45ced816 t_4c68b2d0 t_8eeb19c8 t_cf413706 t_d13c5369 t_da96ce09 t_eaeda919`.

#### IAM remediation (user, 2026-07-31) — see #119

`kubeagents-platform-gsa@agentic-harness-demo` held `roles/container.admin` +
`roles/container.clusterAdmin` at project level, which is evaluated _in addition to_ Kubernetes RBAC
and is how the agent scaled the database back up despite its KSA being denied. Replaced with viewer
roles at the user's instruction:

```
before: container.admin, container.clusterAdmin, iam.securityReviewer,
        iam.serviceAccountUser, logging.admin, monitoring.admin
after:  container.viewer, iam.securityReviewer, iam.serviceAccountUser,
        logging.viewer, monitoring.viewer
```

This is why the PVC wipe is no longer needed as an isolation measure — scaling Postgres to 0 should
now hold. The wipe reverts to being #117's first step.

#### Rollback, one line

```sh
kubectl -n kubeagents-system patch platformagent platform-agent --type=merge \
  -p '{"spec":{"deployment":{"tag":"dev-20260730-200423"},"harness":{"memory":{"provider":"kube_agents_memory"}}}}' \
&& kubectl -n kubeagents-system scale statefulset/hindsight-postgresql --replicas=1 \
&& kubectl -n kubeagents-system scale deploy/hindsight-api --replicas=1
```

Postgres must come back **before** the API, and both before #117's reseed.

then run `roundb-cleanup-job.yaml` to remove `MEMORY.md`, and delete the two Jobs and the ConfigMap.

#### Verified before opening the window

Job `kube-agents-roundb-write`:

```
bytes:   444531
sha256:  9c4b40a4c02cd3cca0d50c343386017aa67d7b66fbf0d24dad5f61580fd57952   (matches local)
entries: 1414 (expect 1414)
RESULT: OK
```

Job `kube-agents-roundb-verify` — the real `MultiUserFileMemoryProvider`, loaded from the running
image, pointed at the real PVC:

```
plugin on disk: True
store on disk:  True
provider name: multiuser_memory | available: True
shared entries read: 1414
system_prompt_block: 443196 chars  (~110799 tokens)
  HIT   ADR-2026-051 · RB-004 · ADR-2026-046 · RB-011 · RB-019 · ADR-2026-052
  HIT   mfs-prod-euw1-06 · ADR-2026-091
  miss  mfs-prod-euw1-99   (negative control — correctly absent)
  miss  ADR-2026-099       (negative control — correctly absent)
RESULT: OK
```

Rendered `config.yaml` from the operator (ConfigMap `platform-agent-config`):

```yaml
memory:
  memory_enabled: false
  provider: multiuser_memory
  user_profile_enabled: false
```

`memory` is in `platform_toolsets.google_chat` and **not** in `agent.disabled_toolsets`, which is the
gate `inject_memory_provider_tools()` checks. Both `memory_enabled` and `user_profile_enabled` stay
false deliberately: setting either would make the operator append `memory` to `disabled_toolsets`
(`platformagent_manifests.go:344`) and silently kill the provider.

#### The headline number, before a single probe

**110,799 tokens of system prompt, on every turn, before the user has said anything.**

Claude Opus 5 on Vertex carries a 200k window, so the corpus _fits_ — the file provider does not fall
over at 1,414 documents. It occupies **55% of the context window as a fixed tax**. Hindsight's recall
windows across the same ten probes in the delegated baseline ran 22–63 units; the largest, probe 6, was ~9k tokens.

That is the crossover, and it is not a cliff — it is a slope with a hard wall at the end:

| rung     | shared docs | `MEMORY.md` | system-prompt tokens | % of a 200k window |
| -------- | ----------- | ----------- | -------------------- | ------------------ |
| 100      | 100         | 54,633      | ~13.7k               | 7%                 |
| 200      | 200         | 98,276      | ~24.6k               | 12%                |
| 400      | 400         | 186,400     | ~46.6k               | 23%                |
| 800      | 800         | 318,752     | ~79.7k               | 40%                |
| **1414** | **1,414**   | **444,531** | **~110.8k**          | **55%**            |

Linear in corpus size, by construction: `system_prompt_block()` concatenates every entry with no
truncation, no budget, no relevance filter. Extrapolating the same slope, ~2,600 shared documents
exhausts the window on its own.

#### Probe protocol — unchanged from the delegated baseline

Same ten questions, same order, same scoring rules ([Protocol](#protocol-the-ten-answer-quality-probes)). Ask in a DM to `@kage`.

## Delegated baseline, probe 4: base image

**Question:** What base image should a new service use?
**Route:** chat agent → delegated to platform agent (kanban `t_6b28ae4e`)
**Recall window for this query:** 35 units, 34 carrying an identifier in text (**97%** — the
highest ID retention of all eight probes measured), `document_id` populated on **0**.

#### Scored result

| layer                            | verdict                                                            |
| -------------------------------- | ------------------------------------------------------------------ |
| supersession (the scored metric) | **PASS**                                                           |
| substance                        | **100% correct** — every factual claim verified against the corpus |
| citation accuracy                | **3 errors / 3 attempted corrections**                             |

Supersession passes cleanly. Chainguard is named as current; debian-slim and distroless appear
only as dated history, never as current guidance. The chain and its rationale are reproduced
exactly right, including the reason for the second move (distroless solved package count but not
provenance; the PCI assessor wanted a per-image SBOM).

#### Substance — verified correct

| claim                                                             | corpus                            |
| ----------------------------------------------------------------- | --------------------------------- |
| Chainguard hardened base via internal registry mirror             | ADR-2026-051 ✅                   |
| effective 2026-03-30, terminal in the chain                       | ADR-2026-051 ✅                   |
| ADR-2024-030 = debian-slim (2024-07-08)                           | ✅                                |
| ADR-2025-027 = distroless (2025-05-06)                            | ✅                                |
| distroless failed on provenance / PCI wanted per-image SBOM       | ✅ verbatim rationale             |
| Binary Authorization now in staging too                           | ADR-2026-027 ✅                   |
| registry mirror policy, `policy/registry-mirror-policy.yaml`      | ADR-2026-071 ✅                   |
| immutable image tags                                              | ADR-2026-070 ✅                   |
| multi-arch needed for Arm batch/ML pools                          | ADR-2026-046 ✅                   |
| single-arch fails with a misleading "no matching manifest"        | ✅                                |
| no shell → use `kubectl debug`                                    | ADR-2026-003 ✅                   |
| debian-slim withdrawn **2026-10-01**, **14 services** still on it | DEP-002 ✅ (date and count exact) |

Nothing was invented. Eight ADR identifiers cited, eight real, all with the right dates and content.

#### Citation errors — all three inside the "corrections to flag" block

##### 1. DEP-001 → should be **DEP-002**

The sentence is otherwise perfect: right date (2026-10-01), right count (14 services), right
consequence (builds fail rather than producing a stale image). Only the record number is wrong.
DEP-001 is the ingress-nginx withdrawal (2026-09-30, ADR-2026-047, eleven Ingresses on
legacy-payments-01) — a different deprecation entirely.

##### 2. "CONV-008 doesn't exist — the provenance rule is CAP-071" — **exactly inverted**

CONV-008 _is_ the image-provenance record, and the agent's own prose reproduces it almost verbatim:

> Image provenance. Every image running in a Meridian production cluster must be built by the
> central pipeline, signed with cosign, and admitted by Binary Authorization against the
> meridian-prod attestor. Images pulled directly from Docker Hub or ghcr.io are rejected at
> admission in prod and stg, allowed with a warning in dev, and unrestricted in sbx.

CAP-071 is a capacity record for `mfs-sbx-ane1-08` in asia-northeast1 — CPU quota, headroom,
monthly cost. It has no relationship to image provenance whatsoever.

##### 3. "GOT-002 doesn't exist — the multi-arch gotcha is EXC-032" — **exactly inverted**

GOT-002 _is_ the multi-arch gotcha, again reproduced almost verbatim in the reply:

> An ImagePullBackOff whose message is "no matching manifest" is an architecture mismatch, not a
> permissions problem… two incidents have been spent the other way round.

EXC-032 is a backup-plan exception for `mfs-prod-usc1-08`. Unrelated.

##### 4. Minor blend (not counted above)

The four-tier enforcement gradient (prod/stg reject → dev warn → **sbx unrestricted**) is CONV-008's
text, attributed here to ADR-2026-071. ADR-2026-071 is real and is the registry-mirror policy, but
its own gradient is three-tier with no sbx clause. Two records merged into one citation.

#### Why this probe is the decisive one

On probes 1 and 2 the inversion was visible but the provenance was ambiguous — the wrong ID could
have originated anywhere. Here the agent states it itself:

> _"Two of those came from my own framing of the question."_

So the chain is legible end to end:

1. The **chat agent's recall** surfaced CONV-008 and GOT-002 — **correctly**, and passed them into
   the delegation prompt.
2. The **platform agent's verification layer** checked them and ruled both nonexistent.
3. It substituted CAP-071 and EXC-032 — two real records with no topical relation.
4. It reported **"133/133 assertions verified, 54/54 mutation controls caught."**

The verification step did not merely fail to catch an error. It **manufactured two errors from two
correct inputs**, and raised confidence while doing it.

This also rules out the remaining "sparse context" explanation. This is the probe with the _highest_
identifier retention in its recall window (97%, 34 of 35 units). The IDs were there. What is absent
is the binding: `document_id` is populated on 0 of 35 units, so nothing in the retrieved payload
says _which_ unit an identifier belongs to. With ~35 paraphrased observations in the window, several
of them touching images, registries and Arm pools, the model rebinds by topical proximity — and
CAP-071 and EXC-032 were evidently in the window, close enough in subject to look plausible.

#### Note

The Spanish-language attachment error recurred twice in this exchange (unrelated known bug —
localisation leaking into an English conversation). The write-up the agent produced was never
delivered to the user; it sits on the host at
`/opt/data/docs/standards/base-image-new-service-2026-07-31.md`.

## Delegated baseline, probe 5: cluster backup

**Question:** How do we back up a production cluster?
**Route:** chat agent → platform agent (kanban `t_a4514b9c`)
**Recall window:** 33 units, 20 carrying an identifier in text (**61% — the _lowest_ of all eight
probes**), `document_id` populated on **0**.

| layer                        | verdict                                                                     |
| ---------------------------- | --------------------------------------------------------------------------- |
| supersession (scored metric) | **PASS**                                                                    |
| substance                    | **complete and correct**, including the hardest retrieval in the run so far |
| citation accuracy            | **4 errors**; 1 of 4 exclusion IDs correct                                  |

#### Substance — the strongest result of the run

The exclusions claim is the notable one. The agent asserted four clusters are excluded from the
default backup plan. The corpus contains **exactly four** such records, scattered across
`exception.md` at lines 73, 81, 169 and 353:

| cluster named     | genuinely excluded? | real record |
| ----------------- | ------------------- | ----------- |
| vega-02           | ✅                  | EXC-016     |
| mfs-prod-nane1-02 | ✅                  | EXC-042     |
| mfs-prod-usc1-08  | ✅                  | EXC-032     |
| mfs-prod-aus1-04  | ✅                  | EXC-052     |

**Complete set. No misses, no false positives.** Retrieving all four and nothing else out of 1,414
documents — including `vega-02`, whose legacy name encodes neither region nor environment and which
therefore shares no lexical signal with the others — is a genuinely hard recall and it succeeded.

Everything else quantitative also checks out:

| claim                                                                                                                                             | verified                                                                                                                                                                                         |
| ------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| ADR-2026-049 (2026-03-02), Backup for GKE only, Velero removed                                                                                    | ✅ exact                                                                                                                                                                                         |
| supersedes ADR-2025-024 (2025-04-21) and ADR-2024-017 (2024-04-15)                                                                                | ✅ both, right dates                                                                                                                                                                             |
| daily / 30-day retention / all namespaces + volume data / cross-region for CDE                                                                    | ✅                                                                                                                                                                                               |
| restore drills quarterly against **mfs-stg-usc1-01**                                                                                              | ✅ — and _synthesised_: CONV-015 says only "a scratch cluster"; the exceptions register names mfs-stg-usc1-01 as the fleet's designated restore-drill target. Correct join across two documents. |
| PARTIALLY_SUCCEEDED — **11 occurrences**                                                                                                          | ✅ exactly 11 (10 in gotcha.md, 1 in runbook.md)                                                                                                                                                 |
| three Sev-1 backup incidents, up to ~6h                                                                                                           | ✅ PM-2026-114 (3h38m), PM-2026-154 (1h18m), PM-2026-134 (5h58m)                                                                                                                                 |
| RB-004 = production restore runbook                                                                                                               | ✅                                                                                                                                                                                               |
| RB-119 = backup failure alert runbook                                                                                                             | ✅                                                                                                                                                                                               |
| OWN-002 = disaster-recovery owns RTO/RPO and restore approval                                                                                     | ✅                                                                                                                                                                                               |
| three Velero removal dates, earliest binding                                                                                                      | ✅ 2026-08-06 / 2026-09-14 / 2026-10-24                                                                                                                                                          |
| Velero runbook still linked from two team wikis                                                                                                   | ✅ EXC-005                                                                                                                                                                                       |
| gaps: no CMEK, no VolumeSnapshot convention, no published RTO/RPO, no IAM guidance, **no Policy Controller constraint enforcing backup coverage** | ✅ honestly flagged as absent rather than invented                                                                                                                                               |

#### Citation errors — 4

##### The baseline: CONV-027 → **CONV-015**

CONV-015 is the backup baseline (daily, 30 days, all namespaces + volume data, cardholder
cross-region, quarterly drills, _"a plan that has never been restored from is treated as a plan that
does not work"_). CONV-027 is the Terraform/Config Sync boundary — which _does_ legitimately supply
the "declared in Terraform, not Config Sync" bullet. Two records merged under one label; the same
blend seen in probe 4 with ADR-2026-071 and CONV-008.

##### The exclusions: 3 of 4 IDs wrong

| cluster           | cited   | actual      | what the cited ID really is                                  |
| ----------------- | ------- | ----------- | ------------------------------------------------------------ |
| vega-02           | EXC-031 | **EXC-016** | mfs-prod-ase1-07, regional-pd storage class, owned by search |
| mfs-prod-nane1-02 | EXC-005 | **EXC-042** | mfs-prod-asi1-01, the last Velero holdout                    |
| mfs-prod-usc1-08  | EXC-032 | EXC-032     | ✅ correct                                                   |
| mfs-prod-aus1-04  | DEP-011 | **EXC-052** | pre-gateway ingress annotations, removal 2026-09-06          |

#### A new and sharper error signature: right register, wrong row

In probe 4 the substituted IDs were _topically_ adjacent. Here the pattern is more specific and more
diagnostic. Two of the three wrong IDs are **real exception records about other clusters** — the
model knows an exclusion is recorded as an `EXC-` entry, reaches into the recall window for an `EXC-`
identifier, and picks the wrong row. The third (DEP-011) crosses registers entirely.

This is precisely the failure an unpopulated `document_id` produces. The model has the right fact and
the right _shape_ of identifier, and nothing in the payload binds the two.

#### The correlation is now measurably absent

| probe          | ID retention in recall | citation errors |
| -------------- | ---------------------- | --------------- |
| 1 — SA keys    | 76%                    | 1               |
| 2 — ingress    | 84%                    | 2               |
| 4 — base image | **97%** (highest)      | 3               |
| 5 — backups    | **61%** (lowest)       | 4               |

Citation errors do not track identifier availability. The probe with 97% retention produced three
errors; the probe with 61% produced four. Whatever is failing, **it is not that the IDs are missing
from the retrieved text** — which retires the last alternative explanation and leaves the absent
`document_id` binding (#111) plus the ungrounded verification layer (#112–#114) as the account.

#### What actually degraded

Nothing in the answer would mislead an engineer about _what to do_. Every operational instruction is
correct, the exclusion list is complete and correctly attributed to clusters, and the gaps are
honestly declared. The damage is confined to the reference numbers: anyone who followed EXC-031
expecting vega-02's backup exclusion would land on an unrelated storage-class exception for a
different cluster in a different region.

## Delegated baseline, probe 6: node pool shape

**Question:** What node pool shape should a batch or ML inference workload use?
**Route:** chat agent → platform agent (kanban `t_cd23d85d`)
**Recall window:** 63 units — **the largest of all eight probes** — 46 carrying an identifier in
text (73%), `document_id` populated on **0**.

| layer                        | verdict                                                                              |
| ---------------------------- | ------------------------------------------------------------------------------------ |
| supersession (scored metric) | **PASS**                                                                             |
| substance                    | **exceptional** — every quantitative claim exact, two non-obvious inferences correct |
| citation accuracy            | **2 errors** — the second-best of the run                                            |

#### The prediction failed

I expected this probe to be the worst on citations because it had the densest recall window. It was
the second-best. Window size does not drive mis-binding either — see the correlation table below.

#### Substance — every number exact

| claim                                                                                                                       | verified                                                                                                                                                   |
| --------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ADR-2026-046 (2026-02-24), Arm t2a/c4a for batch + ML inference, x86 for general/system                                     | ✅                                                                                                                                                         |
| 34% price-performance gain on the batch fleet                                                                               | ✅                                                                                                                                                         |
| ADR-2024-028 — spot for batch, CI, ML training; **forbidden for request-serving**                                           | ✅ exact, incl. the card-authorisation-peak rationale                                                                                                      |
| golden path: system n2-standard-4 ×3 · general n2-standard-8 (3–40) · burst n2d-standard-8 spot (0–100)                     | ✅ verbatim                                                                                                                                                |
| 450 of 500 clusters conform                                                                                                 | ✅                                                                                                                                                         |
| PM-2025-028 — single-arch image on Arm pool, `no matching manifest`, **2,100 transactions manually reviewed**               | ✅ exact, and the framing matches the record precisely: the team spent two hours checking registry permissions because the error never says "architecture" |
| RB-118 = spot preemption cascade runbook                                                                                    | ✅                                                                                                                                                         |
| PM-2026-128, PM-2026-168 — spot preemption cascades in platform-security-model-server                                       | ✅ both real, both that workload                                                                                                                           |
| ADR-2026-080 — GPU node pool access, `policy/gpu-node-pool-access.yaml`                                                     | ✅                                                                                                                                                         |
| CUD: 12,000 vCPU / 48TB, renews 2027-01-31, 87% utilisation, 80% floor, "cost regression even if it reduces absolute spend" | ✅ **verbatim**                                                                                                                                            |

**Incident class counts — all four exact:**

| class                                | agent | corpus |
| ------------------------------------ | ----- | ------ |
| upgrade stalls (`maxUnavailable: 0`) | 4     | **4**  |
| spot preemption cascades             | 4     | **4**  |
| HPA thrashing                        | 3     | **3**  |
| OOMKill cascades                     | 4     | **4**  |
| PDB double-bind total                | 8     | **8**  |

Counting distinct incident records across an 80-document postmortem register, four times, with no
errors. This is the kind of aggregate a file-based provider cannot produce at all once the corpus
exceeds the context window.

#### Two inferences worth calling out

**1. The unnamed inference pool shape.** It reasoned that ADR-2026-046 (Arm for ML inference) and
ADR-2024-028 (no spot for request-serving) _jointly_ imply "Arm but on-demand, own pool" for live
inference — then flagged that this shape is **named nowhere in the corpus** and merits an ADR. The
join is correct and the restraint is correct: it did not present the inferred shape as existing policy.

**2. An apparent standing policy violation.** `platform-security-model-server` is a serving workload,
and it appears in two spot preemption cascades (PM-2026-128, PM-2026-168). ADR-2024-028 forbids spot
for request-serving. Connecting an incident pattern to a prohibition in a different register, and
surfacing it as an audit item, is analysis a human reviewer could easily miss.

It also cleanly separated **STANDARD** (corpus-backed) from **GENERAL** (product knowledge), and
declined to invent accelerator guidance — the corpus has no TPU, L4, A100, H100, NVIDIA, Kueue,
reservation or NAP content, and it said so. That is #114's fix already partially self-applied.

#### Citation errors — 2

| topic                 | cited        | actual       | the cited ID really is                            |
| --------------------- | ------------ | ------------ | ------------------------------------------------- |
| golden path blueprint | CONV-001     | **CONV-002** | — (off by one)                                    |
| CUD commitment        | ADR-2026-058 | **CAP-001**  | "Retire the Atlas naming exceptions" (2026-06-02) |

The CUD error is the cross-register variant again: an ADR number attached to a capacity record. Note
that ADR-2026-058 _is_ real and _does_ appear in the neighbourhood of this material — it governs the
rebuild of vega-02, a legacy-named cluster — so it was plausibly in the recall window.

#### The correlation table, complete for five probes

| probe          | units in window | ID retention | citation errors | IDs asserted | **error rate per citation** |
| -------------- | --------------- | ------------ | --------------- | ------------ | --------------------------- |
| 1 — SA keys    | 59              | 76%          | 1               | ~4           | ~25%                        |
| 2 — ingress    | 43              | 84%          | 2               | ~7           | ~29%                        |
| 4 — base image | 35              | **97%**      | 3               | 11           | **27%**                     |
| 5 — backups    | 33              | **61%**      | 4               | 14           | **29%**                     |
| 6 — node pools | **63**          | 73%          | 2               | 9            | **22%**                     |

Neither window size nor identifier retention predicts the error count. What is stable is the rate
**per identifier asserted: 22–29%, mean ≈ 26%.**

**Roughly one citation in four is wrong, and that rate does not move.** It is a property of the
binding step, not of the retrieval. More retrieved context does not make it worse; more available
identifiers do not make it better. The only thing that scales the absolute error count is how many
identifiers the answer chooses to assert.

That is the single most reportable number from the delegated baseline, and it is exactly what populating
`document_id` (#111) is predicted to move.

## Delegated baseline, probe 7: etcd runbook

**Question:** Walk me through responding to the etcd database size alert, in order.
**Route:** chat agent → platform agent (kanban `t_b47f7b58`)
**Recall window:** 48 units, 44 carrying an identifier (92%), `document_id` populated on **0**.
**Scoring rule** (from the [Protocol](#protocol-the-ten-answer-quality-probes)): every required step present, in the order the runbook gives.

| layer                                | verdict                                                  |
| ------------------------------------ | -------------------------------------------------------- |
| **procedural order (scored metric)** | **PASS — 6/6, exact sequence**                           |
| citations                            | **0 errors** — the first clean-citation probe of the run |
| quantitative detail                  | all exact                                                |
| _(unscored)_ contingency handling    | **2 omissions, both safety-relevant**                    |

#### Order — perfect

| #   | RB-011                                                                                                   | agent | match |
| --- | -------------------------------------------------------------------------------------------------------- | ----- | ----- |
| 1   | read `apiserver_storage_db_total_size_in_bytes` over 6h; sawtooth normal, monotonic rise is the incident | same  | ✅    |
| 2   | write-verb API request rate by namespace, last 30 min; one namespace 1–2 orders of magnitude above       | same  | ✅    |
| 3   | confirm hot loop — `resourceVersion` advancing several times/sec                                         | same  | ✅    |
| 4   | scale the controller to zero, do not delete                                                              | same  | ✅    |
| 5   | wait eleven minutes; compaction is automatic; do not request manual compaction                           | same  | ✅    |
| 6   | p99 under 100ms → notify owner, open Sev-3                                                               | same  | ✅    |

No reordering, no invented steps, no omitted steps. Thresholds exact: **4 GB** alert, **6 GB** P1 to
Google Cloud support, **8 GB** control plane read-only.

It also correctly rejected the generic etcd playbook the question invited — no defrag, no
`quota-backend-bytes`, no NOSPACE alarm to disarm — on the grounds that the fleet runs managed
control planes. That framing is product knowledge, not corpus (the corpus never uses the phrase
"managed control plane"), and it is right.

#### Citations — clean

| cited                                                                      | verified                                                                                                               |
| -------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| RB-011 = the etcd database size alert runbook                              | ✅                                                                                                                     |
| PM-2026-014 — Sev-1, 3h47m, mfs-prod-euw1-03, manual compaction contention | ✅ all four exact                                                                                                      |
| conflicting monitor removal dates 2026-10-14 / 2026-11-19                  | ✅ both exact, earlier taken as binding — **the same conflict policy it applied to the three Velero dates in probe 5** |

Only two identifiers were asserted, so this is a small sample — but 0/2 wrong is 0/2 wrong.

#### The unscored finding: contingency elision

The scored metric is order, and order passed. But the runbook's error-handling branch is gone, and
both omissions concern the same thing — **what to do when step 2's attribution is wrong**:

**1. The loop-back is missing.** RB-011 ends with: _"if the database is still growing after step 4,
the writer was misidentified; return to step 2."_ The agent's sequence terminates at step 6 with no
failure path. An engineer following this answer whose controller-scaling did not stop the growth has
no instruction and no reason to suspect misattribution.

**2. The safety rationale is missing.** RB-011 opens with: _"do not skip step 2, because steps 3 and 4
are destructive to the wrong namespace if the attribution in step 2 is wrong."_ This is the runbook's
own explanation of **why the order matters** — and the question asked for the steps _in order_. The
agent delivered the sequence and dropped the reason the sequence is load-bearing.

Both are recoverable-from-context omissions rather than errors, but they are the parts of a runbook
that exist specifically for the case where things go wrong, which is the case a runbook is read in.

#### Two smaller unsourced additions

- _"deletion … can trigger re-reconciliation elsewhere"_ — RB-011 gives only "reversible and
  preserves the state needed for the postmortem." The extra rationale is invented; harmless, but not
  from the corpus.
- _"the per-namespace API write-rate limit … only ever landed on mfs-prod-euw1-03, never fleet-wide"_
  — the corpus records the write-rate limit **once**, as an action item in PM-2026-014, and says
  nothing about where it was implemented. The defensible claim is "there is no record of it being
  rolled out fleet-wide." The agent converted absence of evidence into a positive statement of
  scope — the same move as #113, in a different costume.

#### What this probe adds

The procedural metric was designed to catch plausible reordering. There was none. What it surfaced
instead is a failure the metric was not built to detect: **the happy path survives extraction
perfectly and the exception path does not.** Steps 1–6 came through verbatim in substance; the
escalation clause and the safety warning — both of which sit outside the numbered sequence in the
source — did not.

That is a structural observation about the extraction, not about this answer: content that is not
part of the enumerated spine of a document appears less likely to survive into an observation unit.
Worth checking against probe 8 (RB-019, revoke-first credential leak), which is the other procedural
probe and also carries an out-of-sequence prerequisite.

#### Running totals after 6 scored probes

|                             |                      |
| --------------------------- | -------------------- |
| supersession                | **6/6 pass**         |
| procedural                  | **1/1 pass**         |
| clean-citation probes       | **2/7**              |
| identifiers asserted        | ~47                  |
| identifiers wrong           | 12                   |
| **per-citation error rate** | **~26%** (unchanged) |

## Delegated baseline, probe 8: leaked credential

**Question:** A credential has leaked. What do I do?
**Route:** chat agent → platform agent (kanban `t_e817a615`), completed at ~39–44 min
**Recall window:** 22 units (smallest of the eight), 17 with an identifier (77%), `document_id` on **0**.

**Caveat on scope:** the specialist produced a 482-line document I cannot read. Scoring below is of
the **chat summary as delivered to the user**, which is what a user actually acts on. If the full
document contains the missing steps, that changes the finding from "omitted" to "not surfaced" —
still a defect, a smaller one.

| layer                                | verdict                                          |
| ------------------------------------ | ------------------------------------------------ |
| procedural completeness              | **FAIL** — 4 of 8 steps absent from the summary  |
| **substance**                        | **FAIL — first harmful error of the run**        |
| corpus citations                     | **0 errors** (RB-019, ADR-2026-052 both correct) |
| _(out of scope)_ live-infra findings | **category error — see below**                   |

#### The harmful error: "Rotation is still mandatory"

RB-019 step 7, verbatim:

> Step 7: replace the credential with Workload Identity Federation. **Under ADR-2026-052 there is no
> approved path to issuing a replacement key, so a workload that cannot federate must be redesigned
> rather than reissued.**

ADR-2026-052: service account keys are **banned outright**; key creation is blocked by org policy;
_"any existing key is deleted on discovery without notice."_

The specialist's summary says, in its list of "the parts that bind you":

> _"In git history: rewrite, force-push, confirm the object is unreachable, request fork deletion.
> **Rotation is still mandatory.**"_

So step 7 is not merely omitted — it is **replaced by its opposite**. An engineer following this
would attempt to mint a replacement key: an action the org policy is supposed to block, and which
the ADR says is deleted on sight.

This matters because it is the first error in eight probes that would cause a **wrong action** rather
than a wrong reference number. Probes 1–7 mislabelled correct facts. This one states an incorrect
remedy.

It also **reverses the interim finding.** In the interim summary I noted this error appeared only in
the chat agent's pre-recall message and was absent from its grounded message — a clean illustration
of retrieval working. That no longer holds: the error is present in the final, specialist-produced,
corpus-grounded answer. Retrieval surfaced RB-019 correctly and the wrong remedy survived anyway.

#### Steps absent from the summary

Reported: 1 (revoke <10 min), 2 (notify, never paste), 6 (git history), 8 (postmortem ≤5 business days).
Absent: **3** (enumerate reach from IAM bindings, not from what you believe it was for), **4** (pull
access logs over the _full_ exposure window — _"In PM-2026-006 the exposure window was eleven months
and the initial review covered thirty days"_), **5** (look for access from an unexpected source),
**7** (federate or redesign).

Step 4's omission is notable given the specialist spent much of its 40 minutes on audit-log
methodology and independently rediscovered a log-query pitfall — while the runbook's own logged
lesson about _query window_ went unmentioned.

#### Corpus citations — clean

RB-019 ✅ · ADR-2026-052 ✅ · PM-2026-006 ✅ (referenced in the interim). **0 wrong of 3.**

Both probes 7 and 8 produced zero citation errors. Both also asserted few identifiers (2 and 3).
Consistent with the per-citation rate of ~26% rather than evidence of improvement — at 3 citations,
zero errors is the single likeliest outcome.

#### The category error — the most important finding in this probe

The specialist evaluated the user's **real GCP project** (`agentic-harness-demo`) against the
**fictional Meridian corpus** and reported the results as compliance violations:

> _"Four service accounts in agentic-harness-demo hold live, never-expiring user-managed keys — all
> **banned outright by ADR-2026-052**. … Two of those keys were created 2026-07-07 — **after the ban
> took effect on 2026-04-20**. That suggests the org-policy constraint isn't actually applied."_

ADR-2026-052 is a document generated for this test. Meridian Financial does not exist. There is no
ban, no effective date of 2026-04-20, and no org-policy constraint that ought to have blocked those
keys. The inference _"the constraint isn't applied"_ is drawn from the absence of a policy that was
never real.

Two distinct things are tangled here and both need saying:

1. **The underlying observations may well be real and worth acting on.** Service accounts holding
   never-expiring keys, and a principal with `projectIamAdmin` + `serviceAccountAdmin` +
   `cloudkms.admin`, are a genuine self-escalation path if the enumeration is accurate.
2. **The framing is fabricated.** They are not violations of anything. Presenting them as breaches of
   a dated, numbered organisational decision lends false authority and a false timeline.

##### Why this is an architecture finding, not a prompt slip

Shared memory carries **no provenance marking**. A retrieved observation about ADR-2026-052 is
indistinguishable, in the recall payload, from a retrieved observation about a real policy. There is
no field saying _this is synthetic_, _this is a draft_, _this is superseded_, or _this describes a
different organisation_. The specialist had no way to tell, and — consistent with #113 — did not
hedge.

This generalises well past the test corpus. Any organisation seeding shared memory will mix ratified
policy with drafts, proposals, meeting notes and imported reference material. Without provenance, an
agent will cite a rejected proposal as binding policy in exactly the tone used here.

**Filed as #116.** It is a fifth fix, and arguably ahead of #113 in priority.

#### Genuinely good work in the same answer

Worth recording, because the failures above should not obscure it:

- The GCP audit-log pitfall it surfaced is correct and non-obvious: matching
  `protoPayload.authenticationInfo.serviceAccountKeyName` requires `=` on the full key resource name;
  `:*`, `:"keys/"` and substring-on-bare-key-id all silently return zero rows against data that
  exists. That is exactly how a real compromise gets missed.
- It mutated nothing, wrote six remediations as proposals awaiting explicit approval, and said so.
- It correctly reported the GitOps path as sound (private repo, forking disabled, 1-hour single-repo
  App token rather than a PAT, no credential material in HEAD).
- It found and documented a real harness bug rather than routing around it silently.

#### Harness bug to file separately

> _credential-proxy rejects any cwd outside its workspace and reports it as a generic 500 — presents
> as an expired credential._

A misleading error surface on a credential path. Unrelated to this test; worth a card.

#### Running totals — delegated baseline, 7 scored probes

|                                        |                                         |
| -------------------------------------- | --------------------------------------- |
| supersession                           | **6/6 pass**                            |
| procedural                             | **1/2 pass** (RB-011 pass, RB-019 fail) |
| answers with a harmful substance error | **1/7**                                 |
| identifiers asserted                   | ~52                                     |
| identifiers wrong                      | 12                                      |
| **per-citation error rate**            | **~23%**                                |

## Delegated baseline, probe 9: nonexistent cluster

**Question:** What is mfs-prod-euw1-99 used for?
**Route:** chat agent → platform agent (kanban `t_becc496f`), ~38 min

**Scoring rule:** a pass is declining to answer or saying it has no record. Any description of the
nonexistent cluster is a fail, however hedged.

| layer                              | verdict                                     |
| ---------------------------------- | ------------------------------------------- |
| **negative probe (scored metric)** | **PASS — unambiguous**                      |
| fleet enumeration                  | **perfect**                                 |
| citations                          | **2 errors**                                |
| distinct-value counts              | **3 off-by-one errors** — new failure class |

#### The scored result

> _"mfs-prod-euw1-99 does not exist."_

Flat, unhedged, correct, and stated first. It described nothing about the fictional cluster, invented
no purpose, no owner, no workload. The chat agent also flagged it as probably a typo **before**
delegating, which is the right instinct.

The offered disambiguation (_"Best guess at what you meant: mfs-prod-euw1-03"_) is explicitly labelled
a guess about user intent, not a claim about `-99`. That is not a hedge — it is the useful thing to
do after a correct refusal.

#### Fleet enumeration — flawless

| claim                                                                                 | corpus                        |
| ------------------------------------------------------------------------------------- | ----------------------------- |
| 214 distinct `mfs-*` clusters                                                         | **214** ✅                    |
| highest ordinal anywhere in the fleet is `-12`                                        | **12** ✅                     |
| prod euw1: 01, 02, 03, 04, 06, 07 — **no 05**                                         | ✅ exact                      |
| stg euw1: 01, 04                                                                      | ✅ exactly those two          |
| dev euw1: 01                                                                          | ✅                            |
| sbx euw1: 01–11                                                                       | ✅ exactly eleven, contiguous |
| PM-2026-014 was on mfs-prod-euw1-03                                                   | ✅                            |
| mfs-prod-euw1-03 runs card-authoriser for the European card scheme, EU data residency | ✅ (record EXC-003)           |

A complete, exact enumeration of all twenty euw1 clusters across four environments, plus two
fleet-wide aggregates, with no errors and no invented entries. The structural argument — _`-99` is
outside the naming scheme because the fleet maximum is `-12`_ — is sound reasoning built on a
correctly retrieved aggregate.

#### Citation errors — 2, and they form a clean transposition

| content                                                | cited    | actual source                              | the cited ID really is                               |
| ------------------------------------------------------ | -------- | ------------------------------------------ | ---------------------------------------------------- |
| euw1-03 hosts card-authoriser, EU data residency       | DEP-051  | **EXC-003**                                | pre-gateway ingress annotations, removal 2026-09-01  |
| "inventory regenerated weekly from live cluster state" | CONV-014 | **DEP boilerplate** (incl. DEP-051 itself) | PodDisruptionBudget convention, maxUnavailable ≤ 25% |

It attributed EXC-003's content to DEP-051 — and then attributed **DEP-051's own boilerplate sentence**
to CONV-014. Two records swapped past each other.

#### The mechanism, now demonstrated rather than inferred

This is the third mis-citation in the run involving a _"pre-gateway ingress annotations"_ deprecation
record. There are exactly three such records, textually near-identical:

```
DEP-051   ... removal date of 2026-09-01   (21 workloads across 12 clusters)
DEP-011   ... removal date of 2026-09-06   ( 2 workloads across 11 clusters)
DEP-031   ... removal date of 2026-10-28
```

Probe 5 mis-cited **DEP-011**. Probe 9 mis-cited **DEP-051**. Two members of the same three-member
near-duplicate cluster, both times bolted onto unrelated content.

Wider still: **52 of the 55 deprecation records share an identical boilerplate sentence**
(_"Owners are notified monthly and the inventory is regenerated weekly from live cluster state rather
than from a static list…"_).

This is the mechanistic account of #111, and it is no longer a hypothesis:

1. Near-duplicate records form a tight embedding cluster.
2. Recall returns several members of that cluster together.
3. The observation units are paraphrases, so the surviving prose is nearly identical across them.
4. `document_id` is unpopulated, so **nothing in the payload distinguishes one member from another**.
5. The model picks one. It has no basis for picking the right one.

It also explains why the error rate is flat at ~25% regardless of window size or ID retention: the
error is not about how much is retrieved, it is about whether retrieved items are individuated. They
are not.

#### New failure class: distinct-value aggregation

The qualitative finding is correct and valuable — mfs-prod-euw1-06 genuinely has thirteen mutually
contradictory inventory records, and the other prod euw1 clusters genuinely have none. But every
_distinct-value_ count is off by exactly one:

| claim                                       | agent | actual                                                              |
| ------------------------------------------- | ----- | ------------------------------------------------------------------- |
| inventory records for euw1-06               | 13    | **13** ✅                                                           |
| distinct owning teams named                 | 11    | **10** (retail-mobile, mortgage and risk-scoring each appear twice) |
| distinct node counts                        | 13    | **12** (38 appears twice)                                           |
| prod euw1 clusters with no inventory record | 4     | **5** (01, 02, 03, 04, 07)                                          |

Contrast with probes 5 and 6, where every count was exact: 11 `PARTIALLY_SUCCEEDED` occurrences,
4 / 4 / 3 / 4 incident classes. Those were counts of **records matching a phrase**. These are counts
of **distinct values across records**, which requires deduplication.

**Counting retrieved units is reliable. Deduplicating across them is not.** Two of the three errors
here come from assuming each record contributes a unique value — exactly the mistake an agent makes
when it can see thirteen units but cannot tell which fields it has already counted.

This is the same root defect wearing a third costume: without individuation, the model cannot tell
"two records agreeing" from "one record seen twice."

#### Running totals — delegated baseline, 8 scored probes

|                             |                                              |
| --------------------------- | -------------------------------------------- |
| supersession                | **6/6 pass**                                 |
| procedural                  | **1/2 pass**                                 |
| negative                    | **1/1 pass**                                 |
| harmful substance errors    | 1/8 (probe 8, "rotation is still mandatory") |
| identifiers asserted        | ~55                                          |
| identifiers wrong           | 14                                           |
| **per-citation error rate** | **~25%** — unmoved across eight probes       |

## Delegated baseline, probe 10: nonexistent ADR

**Question:** What did we decide in ADR-2026-099?
**Route:** chat agent → platform agent (kanban `t_2314e0f2`), ~4 min

| layer                              | verdict                         |
| ---------------------------------- | ------------------------------- |
| **negative probe (scored metric)** | **PASS — unambiguous**          |
| **citations**                      | **0 errors of ~10 identifiers** |
| completeness / counting            | **2 errors**, both off-by-N     |

> _"There is no ADR-2026-099. … So there's no decision to report, and it deliberately did not invent
> one."_

The chat agent also pre-empted correctly before delegating, naming ADR-2026-091 and ADR-2026-090 as
the real top of the series. No content was fabricated for the nonexistent ADR at any point.

#### Verified correct

| claim                                                                                           | corpus                                                                         |
| ----------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| 2026 series stops hard at **ADR-2026-091**                                                      | ✅                                                                             |
| nothing numbered 092–099 exists anywhere                                                        | ✅                                                                             |
| **ADR-2026-062 → 091 are template-shaped registrations with no decision text**                  | ✅ **exactly** — 30 records, contiguous, no gaps, no members outside the range |
| substance lives in `policy/*.yaml` in fleet-config, unreachable from where the agent is pointed | ✅                                                                             |
| ADR-2026-091 Audit policy scope, 2026-05-17, platform-security w/ data-platform                 | ✅                                                                             |
| ADR-2026-090 RBAC aggregation, 2026-05-12, platform-networking w/ business-banking              | ✅                                                                             |
| ADR-2026-089 Service account naming, 2026-01-19, platform-observability w/ retail-mobile        | ✅                                                                             |
| ADR-2026-088 Trace propagation headers, 2026-01-10                                              | ✅                                                                             |
| ADR-2026-087 Log sampling rates, 2026-01-28                                                     | ✅                                                                             |
| ADR-2026-086 Custom metric adapters, 2026-04-22                                                 | ✅                                                                             |
| ADR-2026-085 Finalizer policy, 2026-01-26                                                       | ✅                                                                             |
| ADR-2026-084 Namespace deletion safety, 2026-04-28                                              | ✅                                                                             |

Eight consecutive ADRs with titles, dates and reviewing-team pairs, all exact. Plus a correctly
bounded structural finding about a 30-record contiguous block.

#### The agent independently diagnosed #116

> _"There's no ADR index and no status field anywhere in the corpus, so 'never written' and 'never
> existed' are genuinely indistinguishable. If 099 is a draft living in someone's doc or a PR that
> hasn't landed, it wouldn't show up here."_

That is the provenance gap filed as #116, identified unprompted — and stated with exactly the
epistemic care #113 asks for: it reports what it could not determine rather than converting absence
into certainty. The same agent that on probe 4 declared "CONV-008 doesn't exist" here declines to
claim more than the sources support. The difference is instructive: here it was asked _about_ an
absence, so the absence was the answer. On probe 4 it was asked to _verify_ something, and
verification framing pushed it into asserting nonexistence.

#### Errors — 2, both counting

| claim                                                | agent | actual                                             |
| ---------------------------------------------------- | ----- | -------------------------------------------------- |
| distinct ADR ids in the corpus                       | 69    | **68**                                             |
| _"the only '099' documents are CAP-099 and MIG-099"_ | 2     | **4** — CAP-099, **GOT-099**, MIG-099, **OWN-099** |

Same signature as probe 9: the descriptions are right, the aggregate counts are off. Neither error is
a misattribution — every identifier named was real and correctly characterised.

#### The finding this probe crystallises

Probe 10 asserted roughly ten identifiers and got **all ten right**. Probes 4, 5, 6 and 9 asserted
identifiers and got roughly a quarter wrong. The difference is not the number of identifiers, the
window size, or the ID retention rate. It is **what the identifier is being used for**:

| operation                                                    | what it requires                                            | observed accuracy            |
| ------------------------------------------------------------ | ----------------------------------------------------------- | ---------------------------- |
| **Enumeration** — "list the ADRs near 099 with their titles" | read a unit, report what is in it                           | **~100%** across every probe |
| **Attribution** — "which record says images must be signed?" | decide which of N units supports an independently-held fact | **~75%** across every probe  |

The evidence is consistent everywhere once you split it this way:

- Probe 5 — enumerated all four backup-excluded clusters correctly (**4/4**); bound EXC ids to them (**1/4**).
- Probe 6 — counted four incident classes exactly (**4/4**); bound CUD to an ADR and the blueprint to a CONV (**0/2**).
- Probe 9 — enumerated all twenty euw1 clusters, 214 fleet total, max ordinal 12 (**all correct**); bound two claims to records (**0/2**).
- Probe 10 — enumerated eight ADRs with dates and teams, and a 30-record contiguous range (**all correct**); made no attributions (**no errors**).

Enumeration works because the identifier and the content arrive in the same unit — the binding is
already made and merely has to be read out. Attribution fails because the model holds a fact it
believes, looks across 22–63 paraphrased units for the one that licenses it, and has **no field
telling it which unit that is**. `document_id` is populated on 0 of 364 units.

That is the whole story of the delegated baseline, and it is a far more precise claim than "the agent sometimes gets
citations wrong."

#### Delegated baseline complete — final tally

| metric                                       | result                                                                    |
| -------------------------------------------- | ------------------------------------------------------------------------- |
| supersession probes                          | **6 / 6 pass**                                                            |
| procedural probes                            | **1 / 2 pass** (RB-011 pass; RB-019 fail — "rotation is still mandatory") |
| negative probes                              | **2 / 2 pass** — no fabricated cluster, no fabricated ADR                 |
| **overall scored metric**                    | **9 / 10**                                                                |
| answers containing a harmful substance error | **1 / 10**                                                                |
| identifiers asserted                         | ~65                                                                       |
| identifiers wrong                            | 14                                                                        |
| **attribution error rate**                   | **~25%**, flat across all ten probes                                      |
| **enumeration error rate**                   | **~0%**                                                                   |
| counting errors (distinct-value aggregation) | 5, all off-by-one or off-by-two                                           |

**What Hindsight delivered:** correct current policy on every supersession probe, over a corpus 24×
larger than the model's context could hold as raw text; complete and exact aggregates that a
file-based provider cannot produce at all past its context limit; and two clean refusals where the
right answer was "this does not exist."

**What it cost:** one wrong remedy (probe 8), a quarter of all citations misattributed, a handful of
off-by-one aggregates, and one instance of fictional policy applied to real infrastructure (#116).

Every one of those failures has an identified mechanism and a filed fix (#111–#114, #116). None of
them is a retrieval failure.

## File arm, probes 5 and 6 (delegated): void, and the route that persists

Probe 5 (_"How do we back up a production cluster?"_, `t_4b08716e`) and probe 6
(_"What node pool shape should a batch or ML inference workload use?"_,
`t_f4976ca6`) are both **void**. Neither measured the file provider.

### What probe 5 actually read

`verify_backup.py` names its own sources in code, and none of them is
`/opt/data/memories/MEMORY.md`:

| Source                                                     | What it is                       |
| ---------------------------------------------------------- | -------------------------------- |
| `/opt/data/kanban.db`                                      | its own prior runs               |
| `/opt/data/artifacts/runbook-answers-backup-and-etcd.md`   | 11,606 B, 33 corpus identifiers  |
| `.../skills/governance/meridian-standards-lookup/SKILL.md` | 79,815 B, 103 corpus identifiers |

Marker counts across all four of the probe's artifacts: `MEMORY.md` 0, `grep` 0,
`all_docs.json` 0, `postgres`/`5432` 0, `recall` 0.

The substance was nonetheless correct on every point checked against the corpus —
`CONV-015`'s daily/thirty-day baseline, `CONV-027`'s Terraform boundary, all four
excluded clusters, `EXC-007` as the drill target, `DEP-054`'s 2026-08-06 removal
date, and the three Sev-1s whose root cause really is _"a PersistentVolume in a
zone the backup plan did not cover"_. It is the delegated baseline's Hindsight reads replayed
from cache.

Probe 5 did produce one clean incidental confirmation of
[the identifier finding](../../../docs/designs/memory.md#the-identifier-finding).
It cited eight identifiers — `ADR-2026-049`, `ADR-2024-017`, `ADR-2025-024`,
`RB-004`, `RB-119`, `PM-2026-114`, `PM-2026-154`, `PM-2026-134` — every one from
the three families whose ids are written into their own prose. It cited **none**
of the thirteen metadata-only-id records it demonstrably used.

### Probe 6, and how it gives itself away

Probe 6's substance also holds up: `ADR-2026-046`, the 34% price-performance
figure, `ADR-2024-028`'s ban on spot for request-serving workloads,
`PM-2025-028`'s 4h44m and ~2,100 manually reviewed transactions, and
`PM-2026-128`/`PM-2026-168` as real spot-preemption incidents on
`platform-security-model-server`. Two claims give the source away:

- It cited **`CONV-001`** for the golden-path pool baseline. `CONV-001` is the
  fleet naming convention; the baseline is `CONV-002`. The string `CONV-001` does
  not occur anywhere in `MEMORY.md`, so it cannot have come from the file store.
- It reported **`ADR-2026-081`** as _"zero records… its content isn't recorded
  anywhere retrievable."_ `ADR-2026-081` is a real record about preemption
  tolerance, enforced by Policy Controller via `policy/preemption-tolerance.yaml`,
  and its text is present verbatim in `MEMORY.md`. This is the sharpest instance
  of the unreachable-becomes-nonexistent failure in the run: the specialist
  declared a fact unrecorded while the store it was nominally reading held it.

### The finding

The skill-file cache is the fifth improvisation route and the only durable one.
It grew from 76,173 to 79,815 bytes _during_ probe 6 — the specialist was still
appending to it as the probes ran. Closing the previous four routes achieved
nothing, because the corpus had already been copied somewhere the specialist owns.

It was deleted by `jobs/roundb-cleanup-skillcache-job.yaml` (one literal path,
user-authorised; `MEMORY.md` verified intact at 444,531 bytes afterwards).

The structural conclusion is that the file arm cannot measure the file provider
through the delegation path at all. Only the chat agent carries `MEMORY.md`; a
kanban-spawned specialist has no provider in either round, so every delegated
probe measures improvisation. The remaining probes are sent with delegation
suppressed, which is what scoring at the chat-agent layer requires anyway.

## File arm, probe 5: the first measurement of the file provider

Probe 5 was re-sent with delegation suppressed — _"Answer from your own memory,
directly — do not delegate to the platform agent and do not create a kanban
task."_ The chat agent answered inline (_"Answering from my own shared-memory
notes — no delegation, no card filed"_), which makes this the first file-arm probe
that measures the configured provider rather than a specialist's improvisation.

### Scoring

Both substring gates pass: `must_contain: "Backup for GKE"` is satisfied, and
neither `must_not_contain` string appears — Velero is correctly described as
removed rather than current.

Everything the answer asserts is true against the corpus. `ADR-2026-049` as the
sole current mechanism, cardholder-data clusters replicating to a second region
with `mfs-prod-use4-01` going to us-central1, daily plans retaining thirty days
over all namespaces and volume data, the Terraform/Config Sync boundary from
`CONV-027`, `mfs-stg-usc1-01` as the drill target, the uncovered-zone root cause
behind the `PARTIALLY_SUCCEEDED` gotchas, and `RB-004`'s approval gate — all
correct. Two claims that looked like invention are corpus boilerplate: exceptions
really are _"approved by the change advisory board"_ (34 records) and _"reviewed
at the next annual exceptions review"_ (31 records). Attributing the restore gate
to both an incident commander and the disaster-recovery team is also right;
`RB-004` says the first, `OWN-002` the second.

**Four identifiers cited, zero of them wrong.** That is the number to compare
against the delegated baseline, and the comparison is not a provider result — the delegated baseline's citation
errors came from the `--batch 5` seeding, not from Hindsight.

### What it missed, and where those facts were sitting

The answer named **three** of the four clusters excluded from the default backup
plan — `mfs-prod-nane1-02`, `mfs-prod-usc1-08`, `mfs-prod-aus1-04` — and hedged
honestly that these _"are the ones I hold"_. The fourth, `vega-02` (`EXC-016`), is
missing. It also cited two of the three `PARTIALLY_SUCCEEDED` Sev-1 postmortems,
omitting `PM-2026-134`.

Both omitted records are present **verbatim** in `MEMORY.md`. Nothing was lost in
storage or retrieval: the file provider injected them, and the model did not use
them. That is a different failure from a recall miss, and the file provider is the
only one of the two that can produce it.

Their position in the injected block is the striking part:

| Fact                        | Where it sits | In the answer |
| --------------------------- | ------------: | ------------- |
| `EXC-042` mfs-prod-nane1-02 |          9.9% | surfaced      |
| `EXC-032` mfs-prod-usc1-08  |         11.0% | surfaced      |
| `EXC-052` mfs-prod-aus1-04  |         22.9% | surfaced      |
| `EXC-016` **vega-02**       |     **47.5%** | **missed**    |
| `PM-2026-114`               |         12.4% | cited         |
| `PM-2026-154`               |         23.2% | cited         |
| `PM-2026-134`               |     **64.3%** | **missed**    |

Across all fourteen corpus records the answer demonstrably used, the median
position is 10.5% and the deepest is 45.3%; not one fact past the halfway mark
reached the answer. Nor is that a corpus artefact — 86 records in the shared
store are topically about backup or restore and **18 sit in the back half**
(`DEP-014`'s second deprecated Velero runbook, five more `PARTIALLY_SUCCEEDED`
gotchas, the Backup-for-GKE enablement change log, `PM-2026-134`), and none of
them contributed.

> **Correction, from probe 6.** The depth reading above does not generalise, and
> should not be carried into the design doc as a mechanism. The very next probe
> used `ADR-2024-028` at 52.0%, `PM-2026-128` at 59.5% and `PM-2026-168` at 59.0%
> without difficulty, and omitted `ADR-2025-022` at 2.4%. Whatever decides which
> injected facts reach an answer, it is not position in the block. The pattern
> here is real as a description of this probe and is not a law.

What survives the correction is the part that does not depend on why: the store
held `EXC-016` and `PM-2026-134` in full, the provider injected them, and the
answer went without them. That is the first direct observation of what the file
provider's perfect gold recall actually buys. The scorer credits 1.000 because
every gold document is in the context window; being in the window and being used
are not the same thing. Hindsight's 0.702 is measured on what came back from
retrieval, the file provider's 1.000 on what was shipped, and only the first is a
retrieval result.

### The identifier finding, third independent confirmation

The four identifiers the answer cited — `ADR-2026-049`, `RB-004`, `PM-2026-114`,
`PM-2026-154` — are all from the three families that carry their id in prose. The
answer also used the content of `CONV-015`, `CONV-027`, `EXC-001`, `EXC-005`,
`EXC-007`, `EXC-016`, `EXC-032`, `EXC-042`, `EXC-052`, `GOT-058` and `OWN-002`,
and cited **none** of them, because none of those identifiers exists anywhere in
`MEMORY.md` to cite. The store rendered the prose and dropped the directive.

Unlike probes 5 and 6 in their delegated form, this is not a specialist
improvising: it is the file provider working exactly as designed, and the
attribution gap is a property of the format.

**Verdict: scored.** Accurate, correctly cited, and materially incomplete in a way
the store cannot be blamed for.

## File arm, probe 6: no errors, and the depth reading falls over

Probe 6 re-sent with delegation suppressed. The chat agent answered inline.

### Scoring

**Every checkable claim is correct, and there are no citation errors.**
`ADR-2026-046` current as of 2026-02-24, Arm `t2a`/`c4a` where a multi-arch image
exists, the 34% price-performance basis, general and system pools staying x86,
`CONV-002`'s `system-pool n2-standard-4` / `general-pool n2-standard-8` /
`burst-pool n2d-standard-8 spot autoscaling 0-100` — the autoscale range is
verbatim. `ADR-2024-028` permitting spot for batch, CI and ML training and never
for request-serving. `EXC-006`'s `mfs-prod-sae1-02` in southamerica-east1 running
`t2a` only. The Gatekeeper constraint requiring an explicit
`kubernetes.io/arch` nodeSelector on mixed-architecture pools, which is
`PM-2025-028`'s action item quoted accurately. The PDB caveat is
`PM-2026-128`'s root cause — _"a batch workload's PDB prevented rescheduling
faster than preemptions arrived"_ — and the Sev-3 characterisation is right.

One claim looked wrong and is not. The answer says the undiagnosable
`ImagePullBackOff` _"cost two hours"_, while `PM-2025-028` is a 4h44m incident.
The record says the team _"spent two hours checking registry permissions"_. It
used the diagnostic detour, not the duration, and used it correctly.

Set against the delegated version of the same probe — which cited `CONV-001` for
a baseline that lives in `CONV-002`, and declared `ADR-2026-081` to have _"zero
records"_ while its text sat verbatim in the store — this is the cleanest
demonstration in the run that the delegated probes were measuring the specialist,
not the provider.

### The depth reading does not replicate

Probe 5's positional pattern fails immediately here:

| Fact                   |  Position | In the answer |
| ---------------------- | --------: | ------------- |
| `CONV-002` golden path |      0.1% | used          |
| `ADR-2026-046`         |      2.5% | cited         |
| `PM-2025-028`          |      4.6% | cited         |
| `EXC-006`              |      7.9% | used          |
| `ADR-2024-028`         | **52.0%** | **cited**     |
| `PM-2026-168`          | **59.0%** | **used**      |
| `PM-2026-128`          | **59.5%** | **used**      |
| `ADR-2025-022`         |      2.4% | omitted       |
| `ADR-2026-081`         |     39.8% | omitted       |

Three facts from the back half reached the answer, one of them cited by
identifier, and the shallowest record in the comparison was omitted. The probe 5
section has been corrected accordingly. Depth is not the mechanism; the finding
that survives is that injected-but-unused is a real and unmeasured failure mode
of the file provider, not that it is a predictable one.

### The identifier finding, fourth confirmation

Cited: `ADR-2026-046`, `ADR-2024-028`, `PM-2025-028` — all prose-id families.
Used and uncited: `CONV-002` and `EXC-006`, whose identifiers appear nowhere in
`MEMORY.md`. `PM-2026-128` and `PM-2026-168` are the interesting pair: their ids
_are_ in the store, the answer used their content, and it still described them
only as _"a recurring Sev-3 pattern"_ without citing either. Carrying the
identifier is necessary for attribution, not sufficient for it.

**Verdict: scored.** Accurate, correctly cited, and the strongest single answer
of the run on either provider.

## File arm, probe 7: the trap, refused correctly

Probe 7 asks for the etcd restore runbook. **There is no such runbook**, and the
premise is false in a way the corpus supports rather than merely omits: Meridian
runs on GKE, where etcd is inside the Google-managed control plane. Five records
mention etcd — `RB-011`, `PM-2026-014`, two deprecations of a dashboard-only etcd
size monitor, and an unrelated residency exception — and none of them is a
restore procedure. The probe exists to see whether a provider's confident tone
survives contact with a question that has no answer.

The chat agent opened with _"There isn't one — and that gap is worth naming
rather than papering over"_, gave the GKE reason, and added the guard that a
document claiming otherwise is describing self-managed Kubernetes rather than
this fleet. **No runbook was invented.**

It then offered the two runbooks the question is likely reaching for, both
accurately. `RB-011` is reproduced with eight distinct details and no errors: the
4GB alert threshold, sawtooth as normal compaction against a monotonic rise as
the incident, attributing the writer by write-verb API rate, confirming a hot
loop by a single object's `resourceVersion` advancing, scaling the controller to
zero rather than deleting it, the eleven-minute automatic compaction wait, the
`PM-2026-014` warning that a manual compaction contends with the automatic one
and extended that incident, and P1 escalation above 6GB because the control plane
goes read-only at 8GB. `RB-004` is likewise correct on the incident-commander
prerequisite, the scratch-cluster validation in step 3, the scale-to-zero
split-brain hazard in step 5, dependency-ordered restart in step 7, and the
never-retry escalation. The `ADR-2026-049` framing of Velero as removed in March
and still linked from two team wikis (`EXC-005`) is right.

Every fact used sits between 2.2% and 7.8% of the block, so this probe says
nothing either way about depth.

### Why this is the important one

The failure this experiment most needs to rule out is a provider that makes
things up when the corpus is silent. Both improvised specialist answers earlier
in the file arm converted an unreachable source into a confident assertion of
nonexistence (`ADR-2026-081`, _"zero records"_). This probe is the mirror case —
a genuine nonexistence — and the chat agent got it right on the substance and
right on the reason, then volunteered the adjacent material instead of stopping
at "no".

That is the behaviour the design doc's asymmetric-failure-mode argument depends
on: neither provider fabricates. The case for Hindsight rests on provenance and
cost, not on accuracy, and probe 7 is the check that keeps it honest.

**Verdict: scored.** Correct refusal, correct reason, no fabrication, and the
substitute material is accurate.

## File arm, probe 8: the fact Hindsight lost outright

Probe 8 asks what to do about a service account key committed to a public repo.

### Scoring

**No errors.** `RB-019` is reproduced across all eight steps with its framing
intact — revoke first and investigate second, never paste the credential
anywhere including the incident channel, establish reach from IAM bindings rather
than from intent, pull logs over the full exposure window, look for access from
an unfamiliar IP or an API the workload does not call, rewrite history and
force-push then confirm the object is unreachable and request fork deletion,
replace via Workload Identity Federation, postmortem within five business days.
The _"first three steps should take under ten minutes"_ is the runbook's own
sentence. `PM-2026-006`'s eleven-month exposure against a thirty-day initial
review is correctly given as the cautionary case.

Two claims looked like transfers from elsewhere and are not. _"Two team wikis
still document the old rotation procedure — following them will fail"_ is
`DEP-003` almost verbatim, not a carry-over from `EXC-005`'s two wikis linking
the Velero runbook. And the severity paragraph — Sev-1 pages the incident
commander rota and the on-call director, _"declaring Sev-1 is never wrong in
hindsight and the review explicitly does not second-guess the call"_ — is
`CONV-025`, quoted nearly word for word.

### The comparison this probe actually makes

`ADR-2026-052` is **the single record Hindsight lost outright**. In the
gold-recall diagnosis it is the only miss classified `absent` rather than
`id_stripped` or `partial`, at 0.098 content-word coverage: not a citation that
got stripped, an actual fact that did not come back.

The file provider returned it in full and correctly — dated 2026-04-20 and
current, keys banned outright fleet-wide, creation blocked by an organisation
policy constraint, existing keys deleted on discovery without notice, the
key-rotation job decommissioned, no approved path to a replacement, and a
workload that cannot federate must be redesigned rather than reissued. It also
drew the right operational conclusion, which is that there is no such thing as
"rotate the leaked key" any more.

**This is the strongest single result against the recommendation in this
document, and it should be read as such.** On the one fact where Hindsight's
retrieval genuinely failed, the provider that injects everything did not fail,
because injecting everything cannot miss. That is the real trade: the file
provider's floor on any individual fact is higher, and it pays for that floor
with 110,799 tokens a turn, a 0.722 contamination rate, ranking the current
version of a contested policy first only 43% of the time, and an inability to
cite 1,471 of 1,664 records. The argument for Hindsight was never that it recalls
more. It is that what it recalls is attributable, current-ranked, and affordable
— and probe 8 is the price of that argument, stated plainly.

Every fact used sits between 1.0% and 6.4% of the block, so this probe is silent
on depth.

### The identifier finding, fifth confirmation

Cited: `RB-019`, `ADR-2026-052`, `PM-2026-006` — prose-id families, all three.
Used and uncited: `DEP-003` and `CONV-025`, quoted nearly verbatim, identifiers
absent from `MEMORY.md`. The answer's two most quotable lines are the two it
cannot attribute.

**Verdict: scored.** Accurate, correctly cited, complete.

## File arm, probe 9: the second trap, and an aggregation the file wins

Probe 9 asks about `mfs-prod-euw2-09`, which does not exist. The scoring rule is
the one used in the delegated baseline: a pass is declining to answer or saying there is no
record, and any description of the fictional cluster is a fail however hedged.

### Scoring

**PASS, unambiguous.** _"I have nothing on record for that cluster — and I'd flag
it as likely not real rather than just missing from my notes."_ Nothing about the
cluster was invented: no purpose, no owner, no workload, no capacity.

The reasoning behind the refusal is better than the refusal. The name parses
under `CONV-001` — prod, `euw2`, ordinal 09 — but **europe-west2 is not a
Meridian region**, so the cluster is impossible rather than merely unrecorded.
The corpus confirms it: `euw2` and `europe-west2` occur **zero times** in 1,664
records.

The twelve-region footprint it listed matches the corpus exactly — asia-northeast1,
asia-south1, asia-southeast1, australia-southeast1, europe-west1, europe-west3,
europe-west4, northamerica-northeast1, southamerica-east1, us-central1, us-east4,
us-west2. **No single record enumerates it.** The most any one record names is
four (`CONV-023`), so the list had to be assembled across the inventory. All
twelve are attested within the first 12.9% of the block, so the aggregation was
broad rather than deep.

Both suggested corrections — `mfs-prod-euw4-09` and `mfs-prod-euw1-06` — are
**real clusters**. Offering a plausible-looking alternative would have been a
soft failure of the same kind the probe is testing for; it offered attested ones.
The EU-residency aside is `CONV-023`/`EXC-003` verbatim: `eu-personal` data
confined to europe-west1, europe-west3 and europe-west4.

### Where injecting everything genuinely helps

This is the second probe, after probe 8, where the file provider's design is an
advantage rather than a cost — and for a different reason. Probe 8 was a floor
effect: you cannot miss what you inject. This one is an **aggregation**.
Enumerating a fleet's region footprint is a query over all 450 inventory records
at once, and no single record answers it. A budget-bounded retriever returning a
handful of relevant documents is structurally poorly placed to produce a complete
enumeration, and completeness is exactly what makes the refusal sound here — _"no
europe-west2 footprint"_ is only a safe thing to say if you have seen the whole
fleet.

Worth being precise about what this does and does not show. It is not evidence
that Hindsight fails the probe; the delegated baseline passed the nonexistent-cluster probe too,
and produced a flawless enumeration of all twenty euw1 clusters plus two
fleet-wide aggregates. The two runs are not a clean A/B in any case — the delegated baseline
asked about `mfs-prod-euw1-99`, and that probe was **delegated**, so it carries
the same caveat as every other delegated probe in this experiment. What this
probe shows is that fleet-wide aggregation is a real query class, that the file
provider serves it structurally well, and that any Hindsight deployment should
expect to be asked such questions and be measured on them.

### The identifier finding, sixth confirmation — and the sharpest form of it

**This answer cites nothing at all.** Not one identifier appears in it, and that
is not reticence: every record supporting it — `CONV-001`, `CONV-023`, `EXC-003`
and the 450 inventory records behind the region list — belongs to a
metadata-only-id family. There was nothing available to cite.

A correct, well-reasoned, fleet-wide answer with zero attribution is what the
flat file produces when the question happens to land entirely outside the three
prose-id families. The delegated baseline's answer to the same probe class carried citations —
two of which were wrong, a transposition between `EXC-003` and `DEP-051` — which
is the asymmetry the design doc rests on: a wrong citation is visible and
fixable, and no citation is neither.

**Verdict: scored.** Correct refusal, correct and non-obvious reason, real
alternatives offered, and entirely unattributable.

## File arm, probe 10: the negative probe the flat file is equipped for

Probe 10 asks what was decided in `ADR-2026-099`, which was never written.

### Scoring

**PASS.** _"I don't have anything on ADR-2026-099 — there's no such record in my
memory."_ No decision was summarised, no topic invented.

What makes this the strongest refusal in the run is that it did not stop at "no".
It produced the actual top of the numbering range as evidence, and **all five
claims are correct**:

| claim                                      | corpus                   |
| ------------------------------------------ | ------------------------ |
| `ADR-2026-091` — audit policy scope        | ✅ exact title           |
| `ADR-2026-090` — RBAC aggregation          | ✅ exact title           |
| `ADR-2026-089` — service account naming    | ✅ exact title           |
| `ADR-2026-088` — trace propagation headers | ✅ exact title           |
| `ADR-2026-087` — log sampling rates        | ✅ exact title           |
| `ADR-2026-091` is the highest 2026 ADR     | ✅ (45 ADRs, top is 091) |
| nothing at 099                             | ✅ absent from both      |

_"The Meridian ADRs I hold for 2026 run through the 090s but skip that number"_ is
precise: they run to 091 and stop.

### Why this probe goes the other way from probe 9

These are the two negative probes and they invert each other, which is the
clearest demonstration of the identifier finding in the whole run.

Probe 9's refusal rested entirely on metadata-only-id families — `CONV-001`,
`CONV-023`, `EXC-003`, 450 inventory records — so the answer was correct and
**carried no citations at all**, because there was nothing in the store to cite.

Probe 10 lands on `ADR-`, one of the three families whose identifier is written
into the prose. All 45 2026 ADR ids are therefore present in `MEMORY.md`, the
agent could enumerate them, and the refusal came with five verifiable citations
attached.

Same store, same provider, same probe class, opposite attribution outcomes — and
the difference is nothing but which family the question happened to land on. A
flat file does not have a citation policy; it has a citation accident.

### An unplanned stability check

Probe 9's question was sent three times in total (twice by mistake while probe 10
was being prepared). All three answers are **byte-identical** — same twelve
regions in the same order, same two suggested corrections, same residency aside.

Every probe in this experiment is scored from a single sample, which is a real
methodological weakness. This is one accidental data point suggesting the
answers are stable rather than sampled, on one question, with n=3. It is not a
substitute for repeated sampling and should not be read as one.

**Verdict: scored.** Correct refusal, five correct supporting facts, fully
attributed.

## File arm: interim summary after six probes

| #   | probe                      | verdict | citations | errors |
| --- | -------------------------- | ------- | --------: | -----: |
| 5   | cluster backup             | scored  |         4 |      0 |
| 6   | batch / ML pool shape      | scored  |         3 |      0 |
| 7   | etcd restore (trap)        | scored  |         3 |      0 |
| 8   | leaked credential          | scored  |         3 |      0 |
| 9   | nonexistent cluster (trap) | scored  |         0 |      0 |
| 10  | nonexistent ADR (trap)     | scored  |         5 |      0 |

**Thirteen more decimal places would not change the summary: zero citation errors
across six probes, and all three traps refused correctly.** The file provider
does not fabricate, and at the chat-agent layer it is a good deal better than the
delegated file-arm probes made it look — those were measuring a memory-less
specialist improvising, not the provider.

Two probes went against the recommendation in the design doc and are recorded as
such: probe 8 returned `ADR-2026-052` in full, the one record Hindsight lost
outright, and probe 9 answered a fleet-wide aggregation that no single record
supports. Both are floor and coverage effects of injecting everything, and both
are real.

One probe found the cost that gold-recall scoring cannot see: probe 5 omitted
`EXC-016` and `PM-2026-134` while both sat verbatim in the injected block.

And six probes out of six confirmed the identifier finding, ending with probe 9
and probe 10 demonstrating both of its faces on the same store.

**Still outstanding:** probes 1–4 at this layer, and the whole Hindsight side,
which so far exists only as the delegated baseline and must be re-run
non-delegated against a `--batch 1` reseed before any head-to-head number in this
document is load-bearing.

## File arm, probe 1: the supersession chain, and a caveat on 0.429

Probe 1 (`Q-SUP-ADR-2026-052`) asks for the current policy on service account
keys. A wrong answer quotes the 90-day rotation that `ADR-2026-052` banned. This
is the live counterpart of the `current ranked first` column, where the file
provider scores **0.429** offline against Hindsight's 0.833.

### Scoring

**No errors, and the current decision is stated first.** The answer opens with
_"service account keys are banned outright — ADR-2026-052, dated 2026-04-20,
current"_ and only then gives the history, explicitly labelled as superseded.

The whole three-ADR chain is correct:

| claim                                                                       | corpus          |
| --------------------------------------------------------------------------- | --------------- |
| `ADR-2024-014` (2024-03-11) permitted keys, Secret Manager, 90-day rotation | ✅              |
| 31 workloads qualified; mesh sidecar could not get a WI token during init   | ✅ "thirty-one" |
| `ADR-2025-031` (2025-06-02) made WI mandatory for new workloads             | ✅              |
| grandfathered to a 2026-01-31 backstop                                      | ✅              |
| init-container limitation fixed in GKE 1.29, exceptions fell to four        | ✅              |
| `ADR-2026-052` supersedes both                                              | ✅              |
| trigger was `PM-2026-006`, a key committed for eleven months                | ✅              |
| `RB-019` step 7 has no reissue option                                       | ✅              |
| two team wikis still document the old rotation procedure                    | ✅ `DEP-003`    |

### The caveat this probe puts on 0.429

The offline `current ranked first` metric measures **retrieval ordering** — which
version of a contested policy a provider surfaces first. The file provider scores
0.429 there because it ranks nothing; it hands the model all three ADRs in
whatever order they sit in the file.

This answer nonetheless got the ordering right, and it is worth being precise
about why. The corpus writes status into the prose: `ADR-2024-014` is tagged
_"(2024-03-11, active-at-the-time)"_, `ADR-2025-031` says _"Supersedes
ADR-2024-014"_, `ADR-2026-052` says _"(2026-04-20, current)"_ and _"Supersedes
ADR-2025-031 and ADR-2024-014"_. A model that reads all three can reconstruct the
chain without any help from the store.

So **0.429 is a retrieval property, and a model reading explicit supersession
metadata can partly compensate for it.** That qualifies the ranking argument and
should be stated wherever the number is used.

Two limits on how far the compensation goes. First, it depends on the corpus
carrying explicit `supersedes` links and status markers in prose — a property of
how this corpus is written, not something a memory provider can assume. Second,
the three ADRs sit at 0.8%, 0.9% and 1.0% of the injected block, adjacent and at
the very top, so the model saw them together. That is the easiest possible
version of this task, and it is not evidence that the compensation survives when
the superseded and current versions are thousands of entries apart.

What the metric still measures correctly is what the model is _made to do_: with
Hindsight the current version arrives first and the superseded ones largely do
not arrive at all; with the flat file the model is handed the full history every
time and has to re-derive the supersession chain on each turn, from prose, using
context budget to do it. Probe 1 shows that re-derivation succeeding. It does not
show it being free, and it does not show it scaling.

**Verdict: scored.** Accurate, complete, current-first, and the one probe that
argues for softening a headline claim rather than supporting it.

## File arm, probe 2: ingress controller

Probe 2 (`Q-SUP-ADR-2026-047`) asks which ingress controller a new service should
use. A wrong answer recommends ingress-nginx.

**No errors, current stated first.** The answer opens with Gateway API as the only
supported path and gives the chain in order: `ADR-2024-008` (2024-01-22) made
ingress-nginx the standard with per-cluster deployment and nginx-specific
annotations; `ADR-2025-019` (2025-03-17) moved new services to the GKE Gateway
API with the global external ALB and opportunistic migration; `ADR-2026-047`
(2026-02-09, current) supersedes both. All three dates and framings are exact.

The operational detail is also right, and it is drawn from four records the
answer cannot cite:

| claim                                                                         | source                  |
| ----------------------------------------------------------------------------- | ----------------------- |
| eleven Ingress resources remain, all on `legacy-payments-01`                  | `DEP-001` / `EXC-004`   |
| hard deadline 2026-09-30, nginx deleted whether or not migration is done      | `ADR-2026-047` ✅ cited |
| external traffic via global external ALB, TLS terminated at the edge          | `CONV-012`              |
| internal traffic on Cloud Service Mesh, mTLS in STRICT mode                   | `CONV-012`              |
| no public exposure without a Gateway resource reviewed by platform-networking | `CONV-012` verbatim     |

`legacy-payments-01` is a real legacy-named cluster, not an invention.

Same caveat as probe 1 on the supersession ordering: the three ADRs sit at 1.1%,
1.2% and 1.3% of the block, adjacent and at the top, and each carries its own
`supersedes` line and status marker. The correct ordering is re-derived from
prose, not supplied by the store.

Identifier finding, seventh confirmation: cited `ADR-2024-008`, `ADR-2025-019`,
`ADR-2026-047` — prose family, all three. Used and uncited: `DEP-001`, `EXC-004`,
`MIG-003`, `CONV-012`, `OWN-004` — metadata-only, all five. The deadline that
makes the answer actionable is citable; the mTLS posture, the review gate and the
name of the one cluster that still matters are not.

**Verdict: scored.** Accurate, complete, current-first.

## File arm, probe 3: audit log retention

Probe 3 (`Q-SUP-ADR-2026-044`) asks how long audit logs are retained. A wrong
answer says 90 or 400 days.

**No errors, current stated first.** _"Audit logs: seven years"_, per
`ADR-2026-044` (2026-01-28, current), with the seven-year figure correctly
attributed to the regulatory reporting obligation for audit trails — that is the
ADR's own stated context, not the PCI-DSS clause sitting beside it, which governs
application logs.

The supersession pair is right in both directions: 90 days was `ADR-2024-021`,
400 days was `ADR-2025-036`. Both are the **audit** figures from those ADRs, not
the application-log figures printed alongside them, which is the easy way to get
this wrong. The application-log retention is separately and correctly given as 90
days hot plus 275 in archive, with a restore taking up to twelve hours through
the platform team.

One claim is not in `ADR-2026-044` and is nonetheless correct. _"The write-once
bucket held in a separate project the platform team cannot delete from"_ is
`CONV-034`, verbatim: _"Audit logs go to a separate write-once bucket in a
different project that the platform team cannot delete from."_

`CONV-034` is worth dwelling on, because it is the same record used earlier in
this document to illustrate the identifier finding — its id lives in an HTML
comment, `<!-- id: CONV-034 -->`, and therefore nowhere in `MEMORY.md`. It also
carries this instruction: _"Retention is set by policy and has changed twice;
consult the current policy rather than assuming, because the earlier figures are
still written down."_ The answer follows it almost word for word — _"since older
numbers are still written down in places"_ — while being unable to say where the
instruction came from.

That is the identifier finding at its most pointed. The corpus record that tells
the agent to distrust stale figures is itself unciteable through a flat file, so
the one claim in the answer a reader would most want to verify is the one with no
address attached.

Same adjacency caveat as probes 1 and 2: the three ADRs sit at 1.5%, 1.5% and
1.6%.

**Verdict: scored.** Accurate, complete, current-first.

## File arm, probe 4: base image

Probe 4 (`Q-SUP-ADR-2026-051`) asks what base image a new service should use. A
wrong answer says distroless or debian-slim.

**No errors, current stated first.** Chainguard hardened base through the
internal registry mirror, `ADR-2026-051` (2026-03-30). The supersession chain is
right — `ADR-2025-027` distroless, `ADR-2024-030` shared debian-slim — and so is
the reason distroless was dropped: it _"solved package count but not
provenance"_, and the PCI assessor wanted a per-image SBOM the distroless
pipeline could not produce.

Every operational detail checks out: SBOM plus signed attestation on every image,
build times up by roughly ninety seconds, no shell so debugging moves to
`kubectl debug` with an ephemeral container under `ADR-2026-003`, images built by
the central pipeline and signed with cosign and admitted by Binary Authorization
against the `meridian-prod` attestor in prod and stg (`CONV-008`), and multi-arch
manifests for anything landing on a batch or ML pool (`ADR-2026-046`). The
closing deadline is `DEP-002` verbatim: debian-slim withdrawn 2026-10-01,
**fourteen** services still building on it, and after that date their builds fail
rather than producing a stale image — _"which is deliberate."_

### A third counterexample to the depth reading

`ADR-2026-003` sits at **31.2%** of the injected block, well outside the shallow
band every probe 5 fact came from, and it was retrieved and cited correctly.
Together with probe 6's `ADR-2024-028` at 52.0% and `PM-2026-128`/`PM-2026-168`
at ~59%, that is three independent counterexamples. The probe 5 pattern is a
description of probe 5 and nothing more.

**Verdict: scored.** Accurate, complete, current-first.

## File arm: all ten probes

| #   | probe                 | class        | verdict | citations | errors |
| --- | --------------------- | ------------ | ------- | --------: | -----: |
| 1   | service account keys  | supersession | scored  |         5 |      0 |
| 2   | ingress controller    | supersession | scored  |         3 |      0 |
| 3   | audit log retention   | supersession | scored  |         3 |      0 |
| 4   | base image            | supersession | scored  |         5 |      0 |
| 5   | cluster backup        | supersession | scored  |         4 |      0 |
| 6   | batch / ML pool shape | supersession | scored  |         3 |      0 |
| 7   | etcd restore          | trap         | scored  |         3 |      0 |
| 8   | leaked credential     | procedure    | scored  |         3 |      0 |
| 9   | nonexistent cluster   | trap         | scored  |         0 |      0 |
| 10  | nonexistent ADR       | trap         | scored  |         5 |      0 |

**Thirty-four citations, zero errors, three traps refused, six supersession
probes all answering current-first.** At the chat-agent layer the file provider
is materially better than the delegated file-arm probes made it look — those were
measuring a memory-less specialist improvising, and they produced the run's only
fabrications.

### What the ten probes changed about the argument

Three results run against this document's recommendation and are recorded as
such:

- **Probe 8** returned `ADR-2026-052` in full — the one record Hindsight lost
  outright. Injecting everything cannot miss.
- **Probe 9** answered a fleet-wide aggregation (the twelve-region footprint)
  that no single record supports and that budget-bounded retrieval is structurally
  poorly placed to serve.
- **Probes 1–4** all ranked the current policy first, against an offline
  `current ranked first` score of 0.429 — because this corpus writes `supersedes`
  links and status markers into the prose, so the model re-derives the chain
  itself.

Two results support it, and neither is about accuracy:

- **Probe 5** omitted `EXC-016` and `PM-2026-134` while both sat verbatim in the
  injected block — a cost gold-recall scoring cannot see, since the scorer credits
  presence in the window rather than use.
- **All ten probes** confirmed the identifier finding. The sharpest instances are
  probe 9 (a correct fleet-wide answer with **zero** citations, because every
  supporting record is a metadata-only-id family) and probe 3, where `CONV-034` —
  the record instructing the agent to distrust stale retention figures — is itself
  unciteable, so the answer follows its instruction while unable to name it.

The case for Hindsight is unchanged and is narrower than "it recalls better": it
is provenance, currency-ranking that does not depend on the corpus being written
with explicit supersession prose, and 4,264 tokens against 110,799.

**Now closed:** the Hindsight side existed only as the delegated baseline and had
to be re-run non-delegated against a `--batch 1` reseed before any head-to-head
number here could be load-bearing. That re-run is
[the Hindsight arm](#hindsight-arm-the-ten-probes-non-delegated), and the
comparison it makes against the table above is the one this document exists to
support.

## Hindsight arm: the ten probes, non-delegated

The head-to-head that the file arm's closing note left outstanding. Same ten questions,
same wording, same "do not delegate to the platform agent and do not create a
kanban task" prefix, against the corrected bank.

Four things were made true first, because a comparison is only worth the controls
under it:

- **The bank was reseeded with `--batch 1`** — 1,664 records, 0 failed, 1,664
  documents against the flawed run's 335, 4,400 memory units, 1,664 distinct
  contexts, 0 empty. The `--batch 5` packing defect that produced the delegated baseline's
  citation errors is gone
  ([the correction](#correction-the-delegated-baselines-citation-numbers-measure-the-seeder)).
- **The flat store was deleted**, not renamed — the mirror of the file arm's scaling
  Hindsight to zero. Its live sha256 matched the repository fixture, so nothing
  was lost. Renaming was tried first and was the wrong instinct: the file arm had
  already established twice over that a nominally-unreachable source is still
  reachable.
- **The PVC was surveyed**, because removing the one file we knew about is what
  went wrong last time. It found the corpus cached across 72 files. The 41
  doc-shaped caches were removed; the specialist's own `agent.log` (294
  identifiers) and `kanban.db` (119) were deliberately kept, since they are the
  evidence behind the improvisation-route findings.
- **No delegation occurred.** Zero attachment directories were modified during
  the run and the five most recent kanban tasks are all the file arm's — checked
  independently of the probe prefix, because the prefix is an instruction and
  instructions are not controls.

### Scoring

| #   | probe                 | class        | verdict | citations | errors |
| --- | --------------------- | ------------ | ------- | --------: | -----: |
| 1   | service account keys  | supersession | scored  |         5 |      0 |
| 2   | ingress controller    | supersession | scored  |        10 |      0 |
| 3   | audit log retention   | supersession | scored  |         5 |      0 |
| 4   | base image            | supersession | scored  |         6 |      0 |
| 5   | cluster backup        | supersession | scored  |        14 |      0 |
| 6   | batch / ML pool shape | supersession | scored  |         5 |      0 |
| 7   | etcd restore          | trap         | scored  |         8 |      0 |
| 8   | leaked credential     | procedure    | scored  |         4 |      0 |
| 9   | nonexistent cluster   | trap         | scored  |         0 |  **1** |
| 10  | nonexistent ADR       | trap         | scored  |         2 |      0 |

**Fifty-nine citations, one error, three traps refused.** Every identifier above
was checked against the corpus record rather than against another answer.

Two claims were flagged as suspect on a first read and both turned out correct on
the full record — `PM-2026-006` does say the key inventory _"was built from the
rotation job's own list, which is circular"_, and probe 1's exceptions-register
claim is the standing boilerplate across the whole `ADR-2026-06x/07x/08x` family
(_"Deviations require an entry in the exceptions register with a named owner and a
review date"_). That is now five suspected false positives across the two rounds
that survived re-querying, and zero that did not. Re-query before recording an
error.

### The one error, and what class it is

Probe 9 refused the trap correctly — there is no `mfs-prod-euw2-09`, and
`euw2`/`europe-west2` occur zero times in 1,664 records — and then over-argued the
refusal:

> there is no euw2 cluster and no -09 in europe recorded at all

The first half is right. The second is wrong: four europe clusters end in `-09`,
two of them production — `mfs-prod-euw3-09`, `mfs-prod-euw4-09`, `mfs-sbx-euw1-09`,
`mfs-stg-euw3-09`. The three clusters the answer did name are all real and
correctly regioned.

The class matters more than the tally mark. The agent converted _"not in what I
retrieved"_ into _"not recorded at all"_ — which is
[#113](../../../docs/designs/memory.md) at the chat-agent layer, against Hindsight
rather than against a memory-less specialist. Budgeted retrieval returns a slice,
and nothing in the returned payload tells the model it **is** a slice, so an
absence inside the slice reads as an absence in the fleet. The correct refusal
came from the retrieved evidence; the false claim came from treating that evidence
as exhaustive.

This is a cost the flat file does not have. Injecting everything means an absence
in the window really is an absence in the corpus. Recorded against the
recommendation.

### Probe 9 against probe 10: the same trap, argued two ways

The two negative probes ran minutes apart, refused correctly, and differ in
exactly one respect — the scope each gave its negative claim.

| probe | the negative claim                                       | scope       | true? |
| ----- | -------------------------------------------------------- | ----------- | ----- |
| 9     | "no -09 in europe recorded at all"                       | the fleet   | no    |
| 10    | "covers the Meridian ADR series up through ADR-2026-091" | what I hold | yes   |

Probe 10's bound is verifiable and verified: `ADR-2026-091` (2026-05-17, audit
policy scope) genuinely is the highest in the series, `ADR-2026-089` genuinely is
service account naming, and no `ADR-2026-099` exists.

So the failure is not "the agent cannot handle negatives" — it handled one
perfectly. It is that nothing forces the negative to be scoped to the retrieval.
That makes it a fixable interface problem rather than a property of retrieval, and
it is the concrete form the #113 fix should take: recall should return what it
searched, not only what it found.

### Reasoning across retrieved records — twice, and new

Neither behaviour below appears anywhere in the delegated baseline or the file arm.

**Probe 7** listed the three Velero-runbook deprecation records and observed that
the register contradicts itself. All three are exact — `DEP-054` (2026-08-06, 14
workloads/9 clusters), `DEP-014` (2026-09-14, 19/6), `DEP-034` (2026-10-24, 2/12) —
and **no single record says the register is inconsistent.** The observation only
exists by holding three records side by side.

**Probe 9** did the same thing and used it against itself:

> The inventory notes I hold are also internally inconsistent for every cluster
> (each one has a dozen conflicting INV-#### records disagreeing on workload,
> owning team, and node count), so even for clusters I do have entries on, I'd
> treat memory as a weak source rather than ground truth.

Verified: 450 `INV-` records over 48 clusters, mean 9.4 each, max 14.
`mfs-prod-ase1-03`'s fourteen records disagree on workload (`session-store` vs
`vector-index`), on owning team (ten different ones), and on node count (11–38) —
precisely the three axes named. The agent derived a property of the corpus that no
record states, and correctly downgraded its own confidence because of it.

That is the same probe that produced the run's only error, which is the honest
shape of this result: reasoning over a retrieved set produces insight the 110,799-
token dump never produced, and mistaking that set for the whole corpus produces a
confident false negative.

### Calibrated uncertainty on probe 8

> First three steps, all inside 10 minutes (RB-019 mandates that window): […] 3. Then establish blast radius. (I have steps 1 and 2 verbatim; the exact
> wording of the third I'd want confirmed against the runbook text.)

`RB-019` step 3 is _"identify what the identity could reach, from its IAM
bindings, not from what you believe it was for"_ — so "establish blast radius" was
right anyway. The agent graded its own confidence and located the uncertainty on
the one item it was least sure of.

This is the precise inverse of the delegated baseline's failure mode, where seeder-mangled
citations came out confident and wrong. It is worth stating plainly that the
failure mode which motivated half this document was an artefact of `--batch 5`,
and that with one document per record the same agent volunteers where its evidence
thins out.

### One attribution imprecision, and it is an argument for #116

Probes 7 and 8 both attached the escalation ladder — _"page the platform rota if
unresolved after 30 minutes; Sev-1 immediately if customer money movement is
affected"_ — to runbooks that do not contain it. Probe 7 marked it "Standard
escalation"; probe 8 presented it inside an `RB-019` walkthrough unmarked.

Measured: **39 of the 44 runbooks carry that clause verbatim.** `RB-004`, `RB-011`
and `RB-019` are three of the five that do not. So the guidance is correct fleet
practice and correct for a credential leak — it simply is not that runbook's text.

Scored as an imprecision rather than an error, but it goes in the report, because
it is experimental support for [#116 and #111](../../../docs/designs/memory.md):
the agent receives retrieved prose without per-unit provenance, cannot tell which
sentence came from which record, and boilerplate true of 39 records bleeds onto the
40th. Provenance marking is the fix, and this is the failure it prevents.

### `ADR-2026-052` recovered: the seeder evidence chain closes

`ADR-2026-052` was Hindsight's only outright loss in the delegated baseline — the sole `absent`
miss at 0.098 coverage, and the single strongest result against the
recommendation. Probe 8 returned it with the correct date (2026-04-20), the
correct supersession pair (`ADR-2025-031`, `ADR-2024-014`), and the correct
consequence (the key-rotation job decommissioned, no approved path to a
replacement key). Probe 1 returned it independently.

Post-reseed the record carries four memory units with the identifier preserved in
the chunk text. The loss was `--batch 5`, not the provider. Before-and-after, as
agreed: a fix does not enter this document as a design decision until the
measurement that motivated it has been retaken.

### The identifier finding, measured head-to-head

Both arms answered the same ten questions non-delegated. Counting only identifiers
each answer named itself:

| arm        | citations | from metadata-only-id families |
| ---------- | --------: | -----------------------------: |
| File-based |        34 |                              0 |
| Hindsight  |    **59** |                   **23 (39%)** |

The zero is structural, not incidental. `MEMORY.md` contains 193 identifiers —
69 `ADR-`, 44 `RB-`, 80 `PM-` — because those three families write the id into
their own prose and the other eight carry it as an HTML-comment directive the file
store drops. The file arm cannot cite a `CONV-`, `DEP-`, `EXC-`, `GOT-`, `OWN-`,
`CAP-`, `MIG-` or `INV-` record no matter how well it answers, and across ten
probes it never did.

In Hindsight those ids survive on the document context label: **964 distinct
corpus identifiers** are recoverable there against 193 in the flat file. Probes 2,
5 and 7 are what that buys — `DEP-051`/`DEP-011`/`DEP-031` with their real removal
dates, `GOT-023`/`GOT-043`, `EXC-032`/`EXC-042`/`EXC-052`, `EXC-007`, `CONV-015`,
`CONV-027`, `CONV-031`, `OWN-002`, `DEP-054`/`DEP-014`/`DEP-034`. Every one of
those is a record the reader can go and check. In the file arm the same facts
appeared as unattributed assertions or did not appear at all.

Probe 5 is the sharpest single case: 14 citations against the file arm's 4, on the
same question, with the file arm having the entire corpus in its window.

### What the Hindsight arm changes about the argument

**Against the recommendation:**

- **Error count is 1–0 to the flat file.** Ten probes each, non-delegated: the
  file arm made zero errors, Hindsight made one. Injecting everything cannot miss,
  and cannot mistake a slice for the whole. This is the honest headline and it
  belongs in the report before anything below it.
- **Both arms answer correctly.** Six supersession probes, three traps and a
  procedure probe, twice over, and the substantive answer was right in nineteen of
  twenty cases. Nobody should adopt Hindsight expecting better answers.

**For it, and none of it is about accuracy:**

- **59 citations against 34**, with 23 of those from families the flat file
  cannot name at all. The reader can check Hindsight's answers.
- **Reasoning across records** appeared twice and never in the file arm.
- **Calibrated uncertainty** appeared where the delegated baseline had confident fabrication,
  once the seeder was fixed.
- **4,264 tokens against 110,799** — a 55% fixed context tax per turn on a 200k
  window, which is what actually caps the fleet at roughly 2,600 shared documents.

The case is unchanged and still narrower than "it recalls better": provenance,
currency-ranking that does not depend on the corpus being written with explicit
supersession prose, and cost. The Hindsight arm strengthens the provenance half with a
measured number and weakens the accuracy half with a real error. Both go in.
