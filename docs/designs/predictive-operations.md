# Predictive operations: acting on a forecast threshold breach

**Status:** proposal for review; nothing built. The [Scope](#scope) section names what an install
already has; everything after it is a direction for kube-agents, not a build plan with dates. The
one experiment it asks for is a backtest, and the document says what result would change it.

## TL;DR

kube-agents has already evolved from a **reactive** agent, which fixes what a person reports,
to a **proactive** one, whose scheduled audits and event triggers find and fix problems nobody has
reported yet. This document proposes the next step: a **predictive** mode, in which kube-agents
forecasts a failure and resolves it before it happens.

## In short

kube-agents has two operating modes today. In the **reactive** mode a person sees a problem and asks
the agent in chat. In the **proactive** mode the agent finds a problem nobody has reported yet: the
scheduled governance audits find drift, skew and posture gaps, and the event watcher answers a
warning event the moment it lands. Both modes act on a condition that is already true.

This document proposes a third mode, **predictive**: the agent acts on a condition that is not true
yet. The evidence for a predictive finding is a forecast, and the finding says _this series will
cross this limit at about this time, with this confidence_. A PersistentVolumeClaim filling at its
current rate, a node pool a week from its autoscaler maximum, a container whose working set is
climbing toward its memory limit, a namespace approaching its ResourceQuota: each is a boring number
today and an incident later, and the only difference between the two is when someone looks.

The mechanism is a **forecast threshold breach**. The agent already knows the limits, because they
are declared objects it reads today. Cloud Monitoring already holds the history, and the credential
proxy already relays read-only Monitoring calls. What is missing is the forecaster in between, and
the finding shape and remediation path that turn a forecast into work. The forecaster proposed is
[TimesFM 2.5](https://github.com/google-research/timesfm), Google's open-weight time-series
foundation model, run as a credential-free service inside the install. The action a predictive
finding produces is the same declarative path every finding takes: a ledger entry and a pull
request, with a human merging. What changes is only _when_ it fires — which is exactly the property
the [workflow model](../architecture/04-workflow-model.md) gives a trigger: it changes when an agent
wakes, never what it may do.

The vocabulary is deliberate. _Proactive_ is already taken by the audits and the site's
[Proactive autonomy](../site/src/content/docs/overview/proactive-autonomy.md) page, and it means
"finds an existing problem unprompted". _Predictive_ names the mechanism, a forecast, and reads as
the next rung on the ladder reactive, proactive, predictive. The action a predictive finding takes
before the breach is _preemptive remediation_.

## How to read this document

Each section goes a level deeper than the one before it, so a human reader can stop as soon as they
have what they came for.

| Section                                                       | What it gives you                                                     |
| ------------------------------------------------------------- | --------------------------------------------------------------------- |
| [Scope](#scope)                                               | what an install already has, and what this document adds              |
| [The three modes](#the-three-modes)                           | the definition of predictive, and the shape of problem it owns        |
| [What a predictive finding is](#what-a-predictive-finding-is) | the forecast threshold breach, its fields, and the first series       |
| [The forecaster](#the-forecaster)                             | why TimesFM 2.5, what it is not, and the alternatives weighed         |
| [Where the series come from](#where-the-series-come-from)     | the metrics source contract, and why no new credential path is needed |
| [How it fits the pipeline](#how-it-fits-the-pipeline)         | code forecasts, the agent judges; ledger, pull request, event path    |
| [Honesty about the future](#honesty-about-the-future)         | calibration, the hit-and-miss record, and the ways a series lies      |
| [Evaluation](#evaluation)                                     | the red case the loop requires, and why a replay fixture comes first  |
| [Order of work](#order-of-work)                               | phases with the decision gate that could stop them                    |
| [Out of scope](#out-of-scope)                                 | what this is not                                                      |
| [Open questions](#open-questions)                             | what only a build or a backtest can answer                            |

## Scope

Most of the machinery a predictive mode needs is on `main` under some other name. This table is
the boundary between what an install already has and what this document asks for.

| Piece                      | Already on `main`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | What this document adds                                                                                                                                                                                                                  |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Scheduled proactive work   | The governance roster in `agents/platform/cron/jobs.json`, each job an SOP under `agents/platform/governance/`, reporting through the `fleet-audit` ledger ([autonomous watchdogs](../site/src/content/docs/concepts/autonomous-watchdogs.md), [`fleet-audit-issue-ledger.md`](fleet-audit-issue-ledger.md)).                                                                                                                                                                                                                                     | One more audit-shaped capability on that roster, whose findings rest on a forecast rather than a current reading.                                                                                                                        |
| Threshold checks on limits | `stockout-prevention` flags regional quota at or above 90% and autoscaler out-of-resources events in the last 24 hours ([SOP](../../agents/platform/governance/stockout_prevention_sop.md) §3.7, §3.11); `fleet-wide-cost-analysis` samples `kubectl top` three times ([SOP](../../agents/platform/governance/fleet_wide_cost_analysis_sop.md) step 2); `inventory_prioritize` already scores "a quota trend crossing" as a dated finding ([SOP](../../agents/platform/governance/inventory_prioritize_sop.md)).                                  | The same limits checked against where the series is going, not where it is. The instant checks stay where they are; a predictive finding cites the same limit and adds a breach time.                                                    |
| Event path                 | The Kubernetes event watcher stamps `kind: k8s-event` on every inject, with the comment that other signal sources "would use different constants when they ship" ([`types.go`](../../k8s-operator/cmd/k8s-event-watcher/types.go)); the Pub/Sub platform adapter turns Cloud Logging alerts into routed agent work ([README](../../agentplugins/pubsub-platform/README.md)).                                                                                                                                                                      | A `forecast-breach` inject kind for a breach whose lead time has fallen inside the acute horizon, so the short-notice case takes the incident path rather than waiting for the next scheduled run.                                       |
| Metrics source             | The credential proxy's GCP API relay permits three read-only Monitoring shapes — `timeSeries` list, `metricDescriptors` list, and Managed Prometheus `query`/`query_range`/`series`/`labels` ([`api_policy.py`](../../agents/platform/scripts/api_policy.py), [`gcp-api-relay.md`](gcp-api-relay.md)); the install grants `roles/monitoring.viewer`. The relay design names a follow-up collector as its first consumer and states the contract that consumer must meet. Several SOPs list Prometheus and BigQuery among their forbidden sources. | Nothing on the credential side. The forecaster's collector is that follow-up consumer. The per-SOP prohibitions stand for the SOPs that carry them; this capability is written against the relay from the start, with its own red lines. |
| Capability lifecycle       | The [capability delivery vehicle](capability-delivery-vehicle.md): pre-defined, scheduled, triggerable from chat and event, customizable through a gated criteria store, self-learning under a policy (R1–R7, requirements not yet built).                                                                                                                                                                                                                                                                                                        | The mapping of this capability onto R1–R7, including what its criteria are and what its learning evidence is (a forecast that came true or did not).                                                                                     |
| Requirements neighbours    | [Fleet anomaly detection checks](fleet-anomaly-detection-checks.md) asks for usage trends and states that "the source is part of the requirement"; its expiry section covers certificates, keys and CA rotation. [Drift detection](drift-detection.md) splits its problem into computing the diff (code) and judging it (the agent).                                                                                                                                                                                                              | The same split applied to forecasting: code computes the forecast, the agent judges it. The deadline class (expiry) is left to the anomaly checks; this document covers series, not countdowns.                                          |
| A forecasting model        | Nothing. No model, no dependency, no image.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | TimesFM 2.5 as a credential-free in-cluster service, an `images.json` entry, and an `enabled:` toggle in the chart, on the pattern the memory store's pods already follow.                                                               |
| Fixtures and cases         | The seeded fleet plants static defects only ([`bench/tasks/DRAFTS.md`](../../bench/tasks/DRAFTS.md)); nothing on it trends.                                                                                                                                                                                                                                                                                                                                                                                                                       | A replay fixture for the first case, and a proposal for the one live fixture that would make the fleet carry a trend.                                                                                                                    |

Out of scope here, as for the vehicle: file names, tool names, and which change lands first beyond
the order of work below. Those belong to the change that implements it.

## The three modes

| Mode           | The condition when the agent acts | What wakes it                                        | Ships today                                                             |
| -------------- | --------------------------------- | ---------------------------------------------------- | ----------------------------------------------------------------------- |
| **Reactive**   | True, and a person noticed        | A chat request                                       | Yes: the Chat Agent and its specialists                                 |
| **Proactive**  | True, and nobody noticed          | A schedule, or an event that already happened        | Yes: the governance audits, the event watcher, the Pub/Sub alert routes |
| **Predictive** | Not yet true                      | A forecast that it will become true within a horizon | No                                                                      |

The line between proactive and predictive is the tense of the evidence. An `OOMKilled` event is
proactive work: the kill happened, and the watcher's job is to make sure someone looks. A container
whose working set has climbed from 40% to 85% of its limit over nine days, on a slope that reaches
the limit on Thursday, is predictive work: nothing has failed, and nothing in the cluster will say so
until it does.

That distinction also says what shape of problem the predictive mode owns. Failures come in two
shapes. The **acute** shape is a step change: a bad deploy, a node lost, a dependency down. The
watcher and the alert routes own it, and no forecast helps, because there is no trend to read. The
**slow** shape is a resource consumed against a declared limit: disk, memory, node count, quota,
object count. Every one of those has a history that says where it is going, and every one of them
ends in the same way when nobody reads it — a `FailedScheduling` storm, a `Pending` pod, a write
error on a full volume. The predictive mode owns the slow shape, and only it.

Two consequences follow. First, the predictive mode is not anomaly detection. An anomaly is a value
that is unusual now; the [fleet anomaly checks](fleet-anomaly-detection-checks.md) own that, and a
value can be perfectly usual on its way to a limit. Second, it is not autoscaling. A predictive
finding never scales anything; it proposes a change to a declared limit through a pull request, or
tells an owner their consumption is on course for one. Keeping a controller's job out of the
agent's hands is what makes the finding safe to raise.

## What a predictive finding is

A predictive finding is a **forecast threshold breach**: a claim that a named series will cross a
named limit within a stated horizon, with the confidence and lead time attached.

### The two halves of a series

Every series the mode watches has the same structure: a **consumed quantity** the agent reads from
a metrics source, and a **declared limit** the agent reads from an object it already audits. The
finding is the pair, and the agent forecasts only the first half. This matters for two reasons.
Forecasting a ratio a controller holds flat is the most common way to produce a confident wrong
answer: a node pool's CPU utilization stays near its target precisely because the autoscaler keeps
adding nodes, so the series that carries the risk is the node count against the autoscaler maximum,
not the utilization. And a limit is a fact, not a forecast; reading it from the object keeps the
finding's threshold reproducible in the way every audit's red lines require.

### The finding's fields

A finding carries enough that a reader can check it without re-running the model:

- **Series.** Cluster, namespace, object, container where relevant, and the metric name as the
  source names it. Its id must be stable across runs for the same object, or the ledger's delta turns
  one slow trend into a stream of new findings.
- **Limit.** The value and where it was read: the PVC's `spec.resources.requests.storage`, the node
  pool's `autoscaling.maxNodeCount`, the container's memory limit, the ResourceQuota's `hard`.
- **Last observation.** The most recent value and its timestamp, so a reader can see how far from
  the limit the series is now.
- **Breach.** The median crossing time, the earliest plausible crossing time (the outer quantile
  in the direction of the limit), and the probability of a crossing within the horizon, expressed as
  the share of the forecast's quantile paths that cross. A finding is raised only when that share
  clears the criteria's minimum.
- **Lead time.** Now to the earliest plausible crossing. It sets the severity and the path.
- **Trend.** The slope over the recent window and whether the lead time got shorter or longer since
  the last run, the "better or worse since last week" the anomaly checks require of every finding.
- **Provenance.** Model name, version and image digest; the context length used and the span it
  covers; the criteria revision, as the vehicle's R4 requires of any report.
- **Owner.** From the team label, as every fleet finding names one.

### Severity and path, by lead time

| Lead time                                | Severity | Path                                                                                                                                      |
| ---------------------------------------- | -------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| Inside the acute horizon (24 h default)  | critical | A `forecast-breach` inject opens an incident through the existing session path, so a person sees it today, not at the next scheduled run. |
| Inside the action horizon (14 d default) | major    | A ledger finding with a remediation pull request where the limit is declared in the GitOps repository, `kind: manual` otherwise.          |
| Inside the horizon (30 d default)        | minor    | A ledger finding, advisory. No pull request until it climbs a tier.                                                                       |

All three defaults are criteria (R4), tunable per cluster family, never by the agent on its own for
the narrowing direction (R5).

### The first series

Five series, chosen because each has a declared limit the agent already reads, a metric a stock GKE
cluster already exports, and a remediation the existing declarative path already knows how to make.

| Series                     | Consumed quantity                                                                                 | Declared limit                    | Remediation                                                                                                                                            |
| -------------------------- | ------------------------------------------------------------------------------------------------- | --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| PersistentVolumeClaim fill | `kubernetes.io/pod/volume/used_bytes` per mounted claim                                           | The claim's requested storage     | `kind: manifest`: a larger request where the StorageClass allows expansion; `manual` where it does not, since that migration is an owner's decision    |
| Node pool headroom         | Node count per pool, derived from the per-node series' resource labels                            | The pool's autoscaler maximum     | A pull request against the pool's declaration raising the maximum, or the fallback shapes the stockout SOP already proposes                            |
| Container memory to limit  | `kubernetes.io/container/memory/used_bytes` (working set) per container                           | The container's memory limit      | `kind: manifest` for a limit raise when the owner confirms growth is legitimate; otherwise a finding for the owner, because a leak is not a sizing bug |
| Namespace quota            | `kube_resourcequota` used, through Managed Prometheus, where the kube-state-metrics package is on | The quota's `hard`                | `kind: manifest`: a quota change in the tenant's declaration, or an owner finding                                                                      |
| Project quota              | `serviceruntime.googleapis.com/quota/allocation/usage` per region and metric                      | The matching `quota/limit` series | `kind: manual`: a quota increase request; the stockout SOP's instant 90% check stays as it is                                                          |

Two classes are deliberately absent. **Deadlines** — certificate expiry, key age, CA rotation,
a maintenance exclusion's end — are countdowns, not forecasts; they need no model and belong to the
anomaly checks' expiry section, though a deadline finding should carry the same lead-time field so
the two read alike. **Control-plane load** — etcd object count, API server latency — is a good
candidate for a second wave and is left out of the first because its limits are not declared
objects but published GKE bounds, which changes how the limit half is read.

## The forecaster

### Why a foundation model at all

The forecast most operators run today is `predict_linear` in PromQL: a straight line through the
last few hours, extrapolated. For a volume filling at a steady rate it is right, cheap, and already
present. It fails on the series that matter at fleet scale because those series are not straight
lines. A working set that grows on weekdays and falls at the weekend, a batch namespace whose quota
use spikes nightly, a node pool that breathes with traffic: a linear fit through any of those either
cries wolf every Friday or misses the crossing by a week. A model that reads the shape of the
series is what turns a forecast into something a person will still trust after the third finding.

Training a model per series is out of the question at fleet scale, and it is what time-series
foundation models exist to avoid. They are pretrained once on a large corpus of series and forecast
a new series **zero-shot**, from its own history alone.

### Why TimesFM 2.5

[TimesFM](https://github.com/google-research/timesfm) is Google Research's decoder-only time-series
foundation model. Version 2.5 is the one this document proposes, for reasons that are each
sufficient on its own:

- **Licence.** The code, and the model weights up to and including 2.5, are Apache 2.0. TimesFM 3.0
  adds native multivariate forecasting, but its pretrained weights are under a non-commercial
  licence that forbids production use; the repository says so in its README. A component that ships
  in an install cannot carry that restriction, so 3.0 is not an option until its weights are
  relicensed, and the design must not depend on the multivariate features only 3.0 has.
- **Size.** About 200 million parameters and roughly 800 MB of weights. It runs on CPU; community
  measurements put resident memory at around 1.5 GB, which is a modest Deployment, not an
  accelerator. The install's memory store already runs two pods of comparable weight.
- **Input contract.** A univariate series of at least 32 points and up to 16,384, no frequency flag
  and no feature engineering. That is exactly the shape a Monitoring `timeSeries` call returns after
  alignment, and the collector's whole job is to produce it.
- **Output contract.** Ten quantiles per horizon step, from q10 to q90, with a continuous quantile
  head up to 1,000 steps. The breach probability in the finding is read straight off those paths,
  and the earliest plausible crossing is the outer quantile. A point forecast alone could not
  produce either.
- **Ecosystem.** Checkpoints on Hugging Face (`google/timesfm-2.5-200m-pytorch`, and a transformers
  port), PyTorch, Flax and MLX backends, a covariate extension, a fine-tuning path, and an official
  agent `SKILL.md` in the repository — the same artifact shape this harness ships its own skills in.
  Google also serves the same model as BigQuery's `AI.FORECAST` and on Vertex AI Model Garden,
  which keeps a managed path open (below) without changing the model.

### What it is not

TimesFM does not know what a PersistentVolumeClaim is. It sees numbers and returns numbers. It does
not detect anomalies, though its prediction intervals can be used that way; it does not explain a
trend; it does not decide whether growth is legitimate. Every one of those is the agent's job, and
the [pipeline section](#how-it-fits-the-pipeline) keeps them there. A published evaluation of
foundation models on real operational series also finds cases where zero-shot forecasts do poorly
even with tuned context and horizon, which is why the [order of work](#order-of-work) puts a
backtest on the fleet's own series before any finding reaches a ledger.

### Alternatives weighed

| Alternative                                | What it offers                                                                                                                                            | Why not the primary                                                                                                                                                                                                                                                                                                                                               |
| ------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `predict_linear` in Managed Prometheus     | Already there, free, right for monotone series.                                                                                                           | Blind to seasonality, and only reaches series that are in Prometheus. It is the **baseline the backtest compares against**, and if it wins on a series class, that class ships on it (see [Order of work](#order-of-work)).                                                                                                                                       |
| Cloud Monitoring forecast conditions       | A metric-threshold alerting policy with `forecastOptions` predicts a crossing within a window of 1 hour to 2.5 days, trained per series, no model to run. | The horizon caps at 2.5 days, which covers the acute tier and none of the others; each series needs a policy provisioned in advance, and the output is an alert, not an attributed finding with a remediation. It is a fine **input** for the acute tier through the Pub/Sub adapter's existing alert route, and a candidate remediation the agent could propose. |
| BigQuery `AI.FORECAST`                     | The same TimesFM model, managed, forecasting millions of series in one SQL statement, with `AI.DETECT_ANOMALIES` alongside.                               | Needs the metrics exported into BigQuery first, a second data path with its own cost and retention, and BigQuery is a source several SOPs forbid. The right answer for a very large fleet once the mode has earned its place; not the first build.                                                                                                                |
| Vertex AI Model Garden endpoint            | Managed serving of the same weights.                                                                                                                      | Per-call cost and egress for a model small enough to run beside the agent. Kept as the option for an install that forbids new in-cluster workloads.                                                                                                                                                                                                               |
| TimesFM 3.0                                | Native multivariate forecasting and past-and-future covariates.                                                                                           | Non-commercial weights. Revisit if relicensed.                                                                                                                                                                                                                                                                                                                    |
| Tabular foundation models (TabPFN and kin) | Classification over features, which is the shape of "will this pod fail" rather than "when does this series cross".                                       | A different question, with licence terms that restrict the current weights; a later document, if a failure-classification mode is ever wanted.                                                                                                                                                                                                                    |

### Where it runs

The forecaster is a **separate Deployment with no credentials**, on the pattern the memory store's
pods already follow: its own image pinned in [`images.json`](../../images.json), an `enabled:`
toggle in the chart alongside the existing optional components in
[`values.yaml`](../../charts/kube-agents/values.yaml), a manifest under the operator's integrations
tree, weights baked into the image so nothing reaches out to a model hub at start, and a
NetworkPolicy that admits only the agent's sandbox. It takes arrays and returns quantiles. It
cannot read Monitoring, cannot reach a cluster, and holds nothing worth stealing.

The alternative, loading the model inside the sandbox image, was rejected: 800 MB of weights
against the image layer budget, and a model process sharing a pod with the agent's shell, for no
gain in trust. The split keeps the credentialed read where the relay's policy already governs it
and the model where a policy has nothing to govern.

## Where the series come from

The relay design anticipated this consumer. Its route list permits, GET only, the three Monitoring
shapes a collector needs: `timeSeries` list for GKE system metrics, `metricDescriptors` list so the
model can name a metric it has not seen, and the Managed Prometheus query family for anything only
kube-state-metrics or the kubelet exports. Its "what follows" section states the contract: the
collector obtains its session from `ApiSession()`, keeps the real endpoint URLs, and treats a relay
403 — whose body names the `gcp.api.*` rule refused — as a `limitations` note for that cluster
rather than as zero usage. The forecaster's collector is written to that contract and adds nothing
to the route list.

What the sources give:

- **GKE system metrics** (`kubernetes.io/`) are on by default on every GKE cluster, sampled every
  60 seconds. Cloud Monitoring keeps them at full resolution for six weeks and downsampled to
  ten-minute points after that, for up to 24 months. Six weeks at 60 s is far more context than the
  model can take; a week at five-minute alignment is about 2,000 points and is the working default,
  with a month at ten-minute alignment for series whose cycle is weekly.
- **Managed Prometheus** carries the kube-state-metrics and kubelet packages where a cluster has
  them enabled. They are not on everywhere, so a series that needs them is checked for presence
  first, and absence is recorded as a limitation on the cluster, never as a clean result — the same
  rule the cost SOP applies when `kubectl top` is unavailable.
- **The declared limits** come from the objects the audits already read with `kubectl get` and
  `gcloud container node-pools describe`, under the same read-only command policy.

The relay is project-scoped, which is the Platform Agent's scope. A series is always tagged with
its cluster from the resource labels, so one collection pass covers the fleet the agent manages
and the ledger groups findings per cluster family and region, as the anomaly checks require.

## How it fits the pipeline

Drift detection split its problem into two jobs: computing the diff, which is mechanical, and
judging it, which is where an agent earns its place. Forecasting splits the same way, and for the
same reason.

**Job A: compute the forecast.** Code, not a prompt. A sweep enumerates the enabled series per
cluster, reads each through the relay, aligns and gap-fills it, sends it to the forecaster, and
compares the returned quantile paths against the declared limit. What comes out is a candidate list
with every field in the finding shape filled from data. A model call should never be inside the
agent's reasoning loop for this; a `kubectl top` sampled three times is what the cost SOP does
today and the relay's own rationale for replacing it applies with more force to a forecast.

**Job B: judge the forecast.** The agent, in the audit's session. For each candidate it asks what
code cannot: is the growth legitimate (a StatefulSet that is meant to accumulate) or a defect (a
log directory nobody rotates)? Is the limit the thing to change, or the consumer? Is there a
declaration in the GitOps repository to change, and what should it say? Is the finding new, or the
same trend as last week with a shorter lead time? The answers set the remediation kind and write
the recommendation. This is also where the agent applies the declared-intent rule the
obtainability audit already has: a repository that declares a claim's growth expected reclassifies
the finding rather than raising it.

**The result takes the existing paths.** Findings go to the audit's ledger issue through the same
helper every audit uses, with the delta computed against the previous run, and a `kind: manifest`
remediation becomes a narrow pull request that a human merges. Nothing new is built for reporting,
per the vehicle's R2. The halt-and-flag gates in the workflow model stand unchanged: a predictive
finding that would raise a project quota, resize a fleet-wide pool, or touch a data volume is a
recommendation, never a pull request the agent opens on its own.

**The acute tier takes the event path.** When a sweep finds a breach inside the acute horizon, it
emits an inject with `kind: forecast-breach` into the session path the event watcher already uses.
The watcher's own source comment reserves that mechanism for signal sources beyond Kubernetes
events; this is one. The incident thread then carries the finding, and a person replying in it is
answered by a session that saw the forecast. The same sweep can run between scheduled runs on that
tier alone — a short-horizon check every few hours, the full sweep daily — because its cost is
dominated by the number of series, not by the horizon.

### On the vehicle

The capability is audit-shaped and maps onto the vehicle's requirements without special cases:

| Requirement      | For this capability                                                                                                                                                                                                                                                                                                 |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| R1 Pre-defined   | The five series, the three horizons, a 90% limit fraction and a 0.7 breach probability ship as defaults. Quiet rather than thorough: a fresh install should see a handful of findings, not a page.                                                                                                                  |
| R2 Scheduled     | One roster job for the daily sweep; the ledger is its record; a clean sweep is silent.                                                                                                                                                                                                                              |
| R3 Triggerable   | "When will `orders-db-0`'s volume fill?" is a scoped run answered in the thread. "Run the capacity runway now" is the shipped stream marked due. The acute tier is the event path above, and a Cloud Monitoring forecast alert arriving through the Pub/Sub adapter is another event that wakes it.                 |
| R4 Customizable  | Criteria: the series enabled, the limit fraction, the three horizons, the minimum probability, per-family exclusions, the alignment window. The procedure, the red lines and the model are image-owned.                                                                                                             |
| R5 Self-learning | The evidence class this capability has that no other audit does: every finding is a prediction that is later true or false. A finding whose breach did not arrive, or arrived when the forecast said it would not, is a precision signal the agent can propose criteria changes from. Narrowing stays propose-only. |
| R6 Durability    | As the vehicle specifies; nothing here needs more.                                                                                                                                                                                                                                                                  |
| R7 Write path    | As the vehicle specifies. The model's outputs are never written to criteria; only a person's confirmation moves a threshold.                                                                                                                                                                                        |

The standing state the A2A bus provides is a natural home for the current forecast per series: a
state topic on the pattern the [payload spec](spec-a2a-payloads.md) already sketches for
upgrade-readiness verdicts, so a later question in chat starts from the last sweep rather than a
cold collection. That is an enhancement, not a dependency.

## Honesty about the future

A finding about the present can be checked by re-reading the object. A finding about the future
cannot, and a mode built on forecasts is only as useful as its record of being right. Four rules
keep it honest.

**Backtest before the first finding.** On the fleet's own series, a rolling-origin backtest: cut
each series at points in the past, forecast forward, and compare against what happened. Report
precision and recall of the breach decision per series class, against the linear baseline. This is
the experiment the [order of work](#order-of-work) gates on, and its result is recorded in this
document's successor before the capability reaches a roster.

**Keep the hit-and-miss record.** Every finding carries its predicted crossing; every later sweep
records whether the series crossed, when, and whether a remediation changed the limit in between.
The ledger already computes a run-over-run delta; this adds an outcome to each closed finding. Per
series class, the capability then reports its own precision in each ledger rewrite, so a reader
sees "12 of 14 PVC forecasts in the last quarter crossed within the predicted window" next to the
findings that rest on the same model.

**Quiet defaults, conservative quantiles.** The breach decision uses the outer quantile toward the
limit for lead time and the share of paths for probability, and the shipped minimum probability
is high. The first weeks on a fleet are for tuning down, not up; the vehicle's R1 says why.

**Know the ways a series lies.** The collector handles each of these before the model sees the
series, and the finding says when one applied:

- **Gaps and dead time.** Prometheus-fed series drop to zero or vanish while a pod is rescheduled.
  Gaps are masked, not zero-filled, and a series with too little unmasked context is skipped with a
  limitation, not forecast.
- **Regime changes.** A deploy changes a slope; a PVC expansion resets a fill; a limit raise moves
  the threshold. The collector cuts the context at the most recent change in the declared limit or
  in the object's generation, and forecasts from the segment that reflects the current regime.
- **Controllers that hide the trend.** Covered above: forecast the quantity a controller consumes
  (node count), not the ratio it defends (utilization).
- **Hard caps.** A series that has already reached its cap is flat and forecasts flat. The instant
  checks catch the cap; the forecaster is for the approach.
- **Series too short or too young.** Fewer than 32 points is below the model's floor and, more to
  the point, a day-old claim has no trend worth reading. The minimum context is a criterion, with a
  floor the image owns.
- **Counters.** A cumulative counter is differenced before forecasting; the limit is compared to the
  level, not the rate.

## Evaluation

The [eval-driven development rule](../../.agents/rules/eval_driven_development.md) makes a
failing case the start of any change to agent behaviour, and the seeded fleet is where planted
defects live. A forecast presents a difficulty no existing case has: a planted defect is a state, but
a trend is a history, and the fleet is read-only for evals and static by design. Two fixtures answer
it, in this order.

**A replay fixture first.** The collector reads its series through a session; a bench fixture
substitutes a recorded series for that session's answer, on the pattern the obtainability specs use
for the Compute Advice API. A case then plants a recorded PVC fill with a known crossing date, and its
deterministic checks assert that the agent's report names that claim, that the breach time falls
within a tolerance of the truth, that the remediation is `kind: manifest` against the claim's
declaration in the case's GitOps fixture, and that no mutating call was made. This runs today, needs
no waiting, and is the red case the first implementation must turn green three times.

**A live fixture second.** One workload on a seeded cluster that writes into a claim at a fixed
rate, sized so the claim never actually fills inside the eval cadence and reset on a schedule, gives
the fleet a real trend after about a week of history. It is a day-N fixture in the sense the fleet
catalogue already uses: a case that reads it cannot pass on a fresh apply, and says so. It belongs in
the same conversation as the other fixtures the fleet is being asked to grow, and its cost is one
small claim per eval project.

The case lands in the existing `capacity` domain; a new domain needs a presubmit seat before it
can exist, which a first case cannot earn. Registration follows the nightly-first rule the eval
rule sets out, and the case is never added to the blocking roster in the change that makes it pass.

## Order of work

Each phase is a separate change with its own live validation. The first has a decision gate that can
stop the rest.

1. **The collector, as a library and a sandbox command.** Written to the relay's contract, producing
   aligned series with limits attached for the five series classes, with a `limitations` record per
   cluster. No model yet: its output is the linear baseline's input as well as the forecaster's.
   This phase is also the metrics source the anomaly checks say they need, built once.
2. **The forecaster service and the backtest.** The TimesFM 2.5 image, the chart toggle, and the
   rolling-origin backtest on a real fleet's series against `predict_linear`. **Decision gate:** a
   series class ships on TimesFM only where the backtest shows it beats the baseline on breach
   precision at equal recall. Where it does not, that class ships on the baseline, and the finding
   shape is unchanged — the model is a provenance field, not the design. If no class clears the
   gate, the mode still ships on the baseline and this document records why.
3. **The capability.** The SOP, the roster job, the ledger stream, the remediation kinds per series,
   the replay-fixture case run red then green, and the criteria on the vehicle once its store
   exists (image-owned defaults until then).
4. **The acute tier.** The `forecast-breach` inject kind, the short-horizon sweep, and the session
   path's handling of a finding that is a forecast rather than an event.
5. **Chat.** "When will X run out" as a scoped run, and the state topic so the answer starts from
   the last sweep.

Phases 1 and 2 produce no agent behaviour and need no case; phases 3 to 5 each start from one.

## Out of scope

- **Anomaly detection on the present value.** The fleet anomaly checks own it.
- **Predictive autoscaling.** A controller's job (HPA, KEDA, the cluster autoscaler); the agent
  proposes limits, it does not scale.
- **Failure classification.** "Will this pod fail" from features is a different model family and
  a different licence conversation.
- **Deadlines.** Expiry and rotation are countdowns; they share the lead-time field and nothing else.
- **Multivariate forecasting and covariates.** The features that need TimesFM 3.0 or its extension
  packages; the first series are univariate by construction.
- **Fine-tuning.** Zero-shot is the premise. A fleet whose series defeat it is a reason to revisit
  the model choice, not to run a training pipeline inside an install.

## Open questions

- **Precision on a real fleet.** The whole proposal rests on the backtest in phase 2. Nothing here
  should be quoted as a result until it has run.
- **Sweep cost.** A fleet of a hundred clusters with a few thousand mounted claims is a few thousand
  series of two thousand points each; on CPU that is minutes, not hours, by the published
  throughput figures, but the figure that matters is the one measured on the install's own node.
- **Metric packages that are off.** The kube-state-metrics and kubelet packages are per-cluster
  choices. Whether the capability should recommend enabling them as its own finding, or stay silent
  on a cluster that lacks them, is a criteria decision the first fleet will settle.
- **Autopilot.** System metrics are present; node pools are not the operator's to size. The node
  pool series does not apply, and the finding shape needs a way to say so per cluster mode.
- **Where the hit-and-miss record lives.** The ledger issue's hidden marker carries findings across
  runs; whether it can carry outcomes too, or whether the record wants the criteria store or a state
  topic, is a question for the change that builds phase 3.
- **The relay's POST shapes.** MQL `timeSeries:query` is refused today, by design. The GET shapes
  suffice for the five series; a later series that needs MQL reopens the relay design's second
  question, not this one.

## Related

- [`capability-delivery-vehicle.md`](capability-delivery-vehicle.md) — the lifecycle this
  capability rides on.
- [`fleet-anomaly-detection-checks.md`](fleet-anomaly-detection-checks.md) — the neighbouring
  requirements; the usage section's metrics source is the one built in phase 1.
- [`drift-detection.md`](drift-detection.md) — the compute-then-judge split this document copies.
- [`gcp-api-relay.md`](gcp-api-relay.md) — the read path, and the contract the collector meets.
- [`fleet-audit-issue-ledger.md`](fleet-audit-issue-ledger.md) — where findings go.
- [`../architecture/04-workflow-model.md`](../architecture/04-workflow-model.md) — why a new
  trigger changes when the agent wakes and nothing else.
- [TimesFM repository](https://github.com/google-research/timesfm) — licence terms per version,
  the 2.5 API, and the official agent skill.
- [Cloud Monitoring forecast conditions](https://docs.cloud.google.com/monitoring/alerts/metric-forecast)
  — the managed alternative for the acute tier.
- [BigQuery `AI.FORECAST`](https://docs.cloud.google.com/bigquery/docs/reference/standard-sql/bigqueryml-syntax-ai-forecast)
  — the same model as a managed function, for a very large fleet.
