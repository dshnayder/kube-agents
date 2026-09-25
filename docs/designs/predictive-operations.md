# Predictive operations: warning before a resource runs out

**Status:** proposal for review; nothing is built. A first backtest has run on real clusters, and
the [Experiment](#experiment) section reports it. The [Scope](#scope) section names what an install
already has; everything after it is a direction for kube-agents, not a build plan with dates.

**Authors:** Dmitry Shnayder; Gari Singh — his [prediction-plane design](https://gist.github.com/mastersingh24/ac4cce73bc57ae4a6d8e04a4ad2cb0e7) is merged here.

## How to read this document

The document starts short and gets more detailed as it goes. Stop reading once you have what you
need.

| Section                                      | Read it if you want                                                                     | Length       |
| -------------------------------------------- | --------------------------------------------------------------------------------------- | ------------ |
| [TL;DR](#tldr)                               | what this is about, in one paragraph                                                    | a minute     |
| [Summary](#summary)                          | how it works and what the evidence says, without the detail                             | five minutes |
| [Detailed design](#detailed-design)          | how it is enabled, what it forecasts, how a warning reaches people, how it is evaluated | the rest     |
| [Experiment](#experiment)                    | what was measured on real clusters, with links to the full results                      | ten minutes  |
| [Related](#related), [Prior art](#prior-art) | the neighbouring designs, and what other products ship today                            | as needed    |

The detailed design is in this order:

| Subsection                                                               | What it gives you                                                                   |
| ------------------------------------------------------------------------ | ----------------------------------------------------------------------------------- |
| [Scope](#scope)                                                          | what an install already has, and what this document adds                            |
| [The three modes](#the-three-modes)                                      | the definition of predictive, and the shape of problem it owns                      |
| [Three layers](#three-layers-forecast-prediction-action)                 | forecast, prediction and action, and where kube-agents draws its boundary           |
| [Enabling predictive mode](#enabling-predictive-mode-opt-in-per-cluster) | opt-in, the per-cluster probe, and when a cluster stops or starts predicting        |
| [What a predictive finding is](#what-a-predictive-finding-is)            | the breach, its reference types, its fields, the first series, and the use-case map |
| [The forecaster](#the-forecaster)                                        | why TimesFM 2.5, what it is not, covariates, and the alternatives weighed           |
| [Where the series come from](#where-the-series-come-from)                | the metrics source contract, and why no new credential path is needed               |
| [How a prediction reaches people](#how-a-prediction-reaches-people)      | triage, forecast, decision rule, agent judgement; ledger, pull request, incident    |
| [Honesty about the future](#honesty-about-the-future)                    | baselines, calibration, the intervention problem, and the ways a series lies        |
| [Evaluation](#evaluation)                                                | the metrics, the red case the loop requires, and why a replay fixture comes first   |
| [Order of work](#order-of-work)                                          | phases and prerequisite spikes, with the decision gate that could stop them         |
| [Success measures and risks](#success-measures-and-risks)                | the targets a phase is judged against, and what could sink it                       |
| [Out of scope](#out-of-scope)                                            | what this is not                                                                    |
| [Open questions](#open-questions)                                        | what only a build or a backtest can answer                                          |

## TL;DR

This document proposes and evaluates a **predictive mode** for kube-agents. Today the agent is
**reactive**, fixing what a person reports, and **proactive**, finding problems that already exist
before anyone reports them. In predictive mode it warns before the problem exists. It uses
[TimesFM](https://github.com/google-research/timesfm), Google's open time-series forecasting model,
to forecast series such as CPU, memory, disk and node count. When a forecast shows a resource
running out, the agent raises an early warning with a proposed fix, before the outage.

The feature is **opt-in and disabled by default**. Once enabled, a cluster uses predictions only
after a probe, a backtest on the cluster's own history, shows its load can be forecast. It stops
if its predictions start going wrong.

The evidence so far: on bursty test clusters, forecasts a day ahead are too imprecise to act on.
On a real customer staging cluster, 8-hour forecasts of memory, requested CPU and node count were
accurate enough. Forecasting costs seconds per request on CPU. Nothing is built yet.

## Summary

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

The defect this fixes is the same everywhere: the cluster has a rich history of a signal and every
control that watches it uses only the most recent value. A disk alert at 85% is minutes of warning
on a fast-filling volume and three weeks of ignored ticket on a slow one. The information needed to
act earlier is already in the series; nothing reads it.

The mechanism is a **forecast threshold breach**. The agent already knows the limits, because they
are declared objects it reads today. Cloud Monitoring already holds the history, and the credential
proxy already relays read-only Monitoring calls. What is missing is the forecaster in between, and
the finding shape and remediation path that turn a forecast into work. The forecaster proposed is
[TimesFM 2.5](https://github.com/google-research/timesfm), Google's open-weight time-series
foundation model, run as a credential-free service inside the install. What makes a foundation
model the right tool, rather than Holt-Winters or Prophet, is that it forecasts **zero-shot**: no
per-series fitting, no per-series hyperparameters, and no warm-up history, so a claim created
yesterday is treated the same as one a year old, and the model is small enough to run on CPU beside
the agent. The action a predictive finding produces is the same declarative path every finding
takes: a ledger entry and a pull request, with a human merging. What changes is only _when_ it
fires — which is exactly the property the [workflow model](../architecture/04-workflow-model.md)
gives a trigger: it changes when an agent wakes, never what it may do.

**Opt-in, and earned per cluster.** Predictive mode ships disabled. When an operator enables it,
each managed cluster runs a probe: the forecaster replays the cluster's recent history and checks
how often it would have been right, above all how often it would have warned about something that
never came. Only a cluster that passes gets predictions, and only for the series types that passed.
Every prediction is later scored against what really happened. A cluster whose score drops stops
predicting, and the probe re-runs on the rest from time to time.
[Enabling predictive mode](#enabling-predictive-mode-opt-in-per-cluster) has the states and bars.

**Why a false alarm costs more than a miss.** A forecast that is too high makes the agent act on a
problem that never arrives: a pull request someone reviews, a person interrupted. Enough of them
and people learn to ignore every warning the agent raises, real ones included. A forecast that is
too low only loses the head start, because the proactive mode still catches the problem when it
arrives. Every threshold in the design leans toward silence for this reason.

**How a warning reaches people.** Code computes the forecasts and the agent judges them: is the
growth legitimate, and is the limit or the consumer the thing to change? A breach days away becomes
a finding in the audit ledger and, where the limit is declared in Git, a pull request a human
merges. A breach inside the next 24 hours is injected into the incident path the event watcher
already uses, so a person sees it the same day.
[How a prediction reaches people](#how-a-prediction-reaches-people) has the detail.

**What the experiment found.** On 11 bursty test clusters, day-ahead forecasts landed within
−10%/+5% of the real value 57% of the time: better than repeating yesterday (45%), far from good
enough to act on. Forecasting 8 hours ahead halved the typical miss. On a real customer staging
cluster with steady load, the bad-day overshoot of an 8-hour memory forecast fell from 20% to 4%,
but only because the load barely moved: repeating the last hour's value did as well, and TimesFM
missed the rises a warning is for.
One CPU container forecasts 64 series in about 20 seconds from 7 days of history, so forecasting
never lags the horizon. The [Experiment](#experiment) section has the numbers.

**What gets built first.** A metrics collector, then the forecaster service with a backtest on a
real fleet, then the capability itself with every series class in shadow until its record earns
promotion. [Order of work](#order-of-work) lists the phases and the gates between them.

The vocabulary is deliberate. _Proactive_ is already taken by the audits and the site's
[Proactive autonomy](../site/src/content/docs/overview/proactive-autonomy.md) page, and it means
"finds an existing problem unprompted". _Predictive_ names the mechanism, a forecast, and reads as
the next rung on the ladder reactive, proactive, predictive. The action a predictive finding takes
before the breach is _preemptive remediation_.

**Where the design comes from.** It merges two designs. The kube-agents half — the finding shape,
the compute-then-judge split, the ledger and pull-request path, the capability lifecycle and the
eval loop — is Dmitry Shnayder's. The platform-agnostic half comes from Gari Singh's [Zero-Shot
Forecasting for Predictive
Operations](https://gist.github.com/mastersingh24/ac4cce73bc57ae4a6d8e04a4ad2cb0e7): the forecast,
prediction and action layers, the reference-type taxonomy and use-case catalogue, the
context-builder rules, the calibration layer, the cost-derived decision rule, the triage cascade,
the intervention problem, and the prerequisite spikes. Where the two disagreed, this document says
so and says how it resolves.

## Detailed design

Each subsection below can be read on its own; the [table above](#how-to-read-this-document) says
what each one covers.

### Scope

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

### The three modes

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
error on a full volume. The predictive mode owns the slow shape first.

Two consequences follow. First, the predictive mode is not anomaly detection. An anomaly is a value
that is unusual now; the [fleet anomaly checks](fleet-anomaly-detection-checks.md) own that, and a
value can be perfectly usual on its way to a limit. Second, it is not autoscaling. A predictive
finding never scales anything; it proposes a change to a declared limit through a pull request, or
tells an owner their consumption is on course for one. Keeping a controller's job out of the
agent's hands is what makes the finding safe to raise.

### Three layers: forecast, prediction, action

Gari Singh's design describes a _prediction plane_ in three layers, and the product boundary can be
drawn after any of them:

| Layer               | Output                                                                      | In kube-agents                                                                  |
| ------------------- | --------------------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| **L1 — Forecast**   | Quantile trajectories for a registered series                               | The forecaster service: arrays in, quantiles out, no credentials, no opinion    |
| **L2 — Prediction** | A typed claim: "series X crosses reference R in about T, with confidence C" | The predictive finding, written by the sweep and judged by the agent            |
| **L3 — Action**     | Actuation with guardrails                                                   | A pull request against the declaration, merged by a human; never a direct write |

Most of the value is at L2. L1 alone is a chart, and it has a second consumer: the fleet anomaly
checks can read the same quantile bands as a dynamic normal without owning a model. L3 is where the
risk is, and this is where the two source designs diverged. Gari's design allows an `enforce` mode
per policy, in which the plane raises a memory limit or pre-warms a node itself behind rate limits,
concurrency caps and a circuit breaker. The kube-agents workflow model does not let an agent write
to a cluster outside the declarative path, so this document keeps L3 at `recommend`: every action is
a pull request, and the halt-and-flag gates stand. `enforce` is out of scope, not deferred.

Four principles from the prediction-plane design carry over unchanged, because each is about the
finding rather than the actuator:

- A forecast is a proposal, never a command. The agent can veto, delay or downgrade it.
- Shadow first. A new series class runs with its findings recorded but not reported until its
  record justifies promotion, and promotion is a recorded criteria change (R4, R7).
- Asymmetric by default. A cheap, reversible remediation (a larger PVC request on an expandable
  StorageClass) gets an aggressive threshold; an expensive or irreversible one (a project quota, a
  data-volume migration) gets a conservative one. The [decision rule](#the-decision-rule) derives
  both from costs rather than hand-picked quantiles.
- No silent staleness. A forecast built on a gappy or stale window is labelled as such and cannot
  produce a pull request.

### Enabling predictive mode: opt-in, per cluster

The [experiment](#experiment) found that forecast error belongs to the cluster, not the model. The
same forecaster that overshot memory by 20% on a bad day on the bursty evaluation hosts overshot by
4% on a customer staging cluster with steady load. A low error alone does not earn a prediction,
though: on that steady cluster, repeating the last hour's value did as well as TimesFM, and that is
what the proactive mode already sees. A fleet-wide yes or no would be wrong, and so would a bar on
error alone. The mode is therefore gated per cluster, and a cluster earns predictions by showing, on
its own history, that they would have been right.

Predictive mode is opt-in and off by default. When an operator enables it, each cluster under
management moves through three states:

- **`probing`.** The probe runs the experiment on the cluster's own recent history: a rolling-origin
  backtest, cut at points in the past, forecasting forward only from what was known then, and
  scoring against what happened, as in the experiment. It checks the costly side first: how far
  forecasts overshoot on a bad day at the horizons the mode uses, and whether TimesFM beats both the
  last value repeated and seasonal-naive on the peak of each window there. A series class that only
  matches the last value has nothing to predict. A cluster that clears the bar moves to
  `predicting`, and one that does not moves to `unpredictable`. Nothing is acted on while probing.
- **`predicting`.** The sweep computes the cluster's future values and acts on them through the
  normal finding path. Every prediction is stored with its horizon. When the period it covers has
  passed and the real values are available, it is scored against them. That rolling score is the
  same record [calibration](#calibration) keeps. If it drops below the bar, the cluster stops
  predicting and moves to `unpredictable`. It does not wait for an operator.
- **`unpredictable`.** No predictions are computed or acted on. The proactive mode still watches
  current values, which is why a forecast that is not trusted costs a head start and not an
  incident. The probe reruns on a schedule. When the cluster's series have become predictable, for
  example after a workload settles or a noisy tenant leaves, it moves back to `predicting`.

The bars are asymmetric on purpose. Entering `predicting` takes a stricter score than staying there,
and a cluster stays in a state for a minimum time before it may leave it, so a cluster near the line
does not flap between the two. Both bars, the reprobe interval and the horizons are criteria on the
[vehicle](#on-the-vehicle), with quiet defaults. They are not constants. Over-forecasts are the
costly miss, since each one is a pull request and an interrupted person, and enough of them teach
people to ignore the agent. So the bars are set on overshoot, and undershoot counts for less.

The decision is per cluster, and the probe records it per series class within the cluster. A cluster
whose memory forecasts well and whose CPU does not predicts memory only. Within a `predicting`
cluster, the per-group states in [Calibration](#calibration) still apply.

Live scoring meets [the intervention problem](#the-intervention-problem): a prediction someone acted
on may never come true. The live score therefore uses only predictions no finding acted on. That is
most of them, since most forecasts raise no finding. A series with a remediated finding leaves the
score until its next unacted window.

The probe is cheap enough to run routinely. Forecast time is set by the padded context, not the
horizon or batch size. One 11-core CPU container forecasts 64 series from 7 days of history in about
20 seconds per request. A probe over four weeks with two cut points a day and 50 series is 56
requests, about 20 minutes. On the staging cluster, the experiment's 7-day run, 129 cut points over
50 series, took 50 minutes.

### What a predictive finding is

A predictive finding is a **forecast threshold breach**: a claim that a named series will cross a
named reference within a stated horizon, with the confidence and lead time attached.

#### The two halves of a series

Every series the mode watches has the same structure: a **consumed quantity** the agent reads from
a metrics source, and a **declared limit** the agent reads from an object it already audits. The
finding is the pair, and the agent forecasts only the first half. This matters for two reasons.
Forecasting a ratio a controller holds flat is the most common way to produce a confident wrong
answer: a node pool's CPU utilization stays near its target precisely because the autoscaler keeps
adding nodes, so the series that carries the risk is the node count against the autoscaler maximum,
not the utilization. And a limit is a fact, not a forecast; reading it from the object keeps the
finding's threshold reproducible in the way every audit's red lines require. Where the limit itself
moves — a VPA rewriting requests — the limit becomes a second series, and the breach is a
trajectory intersection rather than a line crossing; that doubles the cost for those series and is
left to a later phase.

#### Reference types

The organising insight of the prediction-plane design is that a forecast becomes useful the moment
there is something to compare it against, and that the use cases differ only in what that is:

| Reference      | Meaning                                                         | Use cases        |
| -------------- | --------------------------------------------------------------- | ---------------- |
| Static line    | A declared limit, quota or capacity                             | UC-1, UC-5, UC-8 |
| Self (band)    | The forecast of the signal itself, as a dynamic normal          | UC-3, UC-10      |
| Counterfactual | A forecast from pre-change history, against post-change reality | UC-7             |
| Deadline       | A required value at a required time                             | UC-6             |
| Budget         | An integral of the signal over a window                         | UC-9             |
| Derived trend  | A decomposition of the signal, such as its trough envelope      | UC-4             |

The first build supports only the static line, for the reason the prediction-plane design gives
for its own first phase: the reference already exists in the system, so the first shipped thing is
verifiable. The other types are the later phases in the [use-case map](#the-use-case-map).

#### The finding's fields

A finding carries enough that a reader can check it without re-running the model:

- **Series.** Cluster, namespace, object, container where relevant, and the metric name as the
  source names it. Its id must be stable across runs for the same object, or the ledger's delta turns
  one slow trend into a stream of new findings.
- **Limit.** The value and where it was read: the PVC's `spec.resources.requests.storage`, the node
  pool's `autoscaling.maxNodeCount`, the container's memory limit, the ResourceQuota's `hard`.
- **Last observation.** The most recent value and its timestamp, so a reader can see how far from
  the limit the series is now.
- **Breach.** The crossing time per quantile — q90 at 8 days, q50 at 17, q10 never — which _is_ the
  distribution of the breach time, reported as such rather than collapsed to a point. The earliest
  plausible crossing is the outer quantile toward the limit.
- **Lead time.** Now to the earliest plausible crossing. It sets the severity and the path.
- **Trend.** The slope over the recent window and whether the lead time got shorter or longer since
  the last run, the "better or worse since last week" the anomaly checks require of every finding.
- **Calibration and quality.** The series group's calibration state and any quality flags the
  collector raised (see [Know the ways a series lies](#know-the-ways-a-series-lies)).
- **Provenance.** Model name, version and image digest; the context length used and the span it
  covers; a digest of the context window, so the forecast can be reproduced exactly weeks later;
  the criteria revision, as the vehicle's R4 requires of any report.
- **Owner.** From the team label, as every fleet finding names one.

#### Severity and path, by lead time

| Lead time                                | Severity | Path                                                                                                                                      |
| ---------------------------------------- | -------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| Inside the acute horizon (24 h default)  | critical | A `forecast-breach` inject opens an incident through the existing session path, so a person sees it today, not at the next scheduled run. |
| Inside the action horizon (14 d default) | major    | A ledger finding with a remediation pull request where the limit is declared in the GitOps repository, `kind: manual` otherwise.          |
| Inside the horizon (30 d default)        | minor    | A ledger finding, advisory. No pull request until it climbs a tier.                                                                       |

All three defaults are criteria (R4), tunable per cluster family, never by the agent on its own for
the narrowing direction (R5). The horizon for a series class is set by its **remediation lead
time**, not by the model: a volume expansion may need a maintenance window, and a quota increase
takes days, so storage and quota need long horizons while memory needs short ones.

#### The first series

Five series, chosen because each has a declared limit the agent already reads, a metric a stock GKE
cluster already exports, and a remediation the existing declarative path already knows how to make.

| Series                            | Consumed quantity                                                                                 | Declared limit                    | Remediation                                                                                                                                            |
| --------------------------------- | ------------------------------------------------------------------------------------------------- | --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| PersistentVolumeClaim fill (UC-5) | `kubernetes.io/pod/volume/used_bytes` per mounted claim                                           | The claim's requested storage     | `kind: manifest`: a larger request where the StorageClass allows expansion; `manual` where it does not, since that migration is an owner's decision    |
| Node pool headroom (UC-8)         | Node count per pool, derived from the per-node series' resource labels                            | The pool's autoscaler maximum     | A pull request against the pool's declaration raising the maximum, or the fallback shapes the stockout SOP already proposes                            |
| Container memory to limit (UC-1)  | `kubernetes.io/container/memory/used_bytes` (working set) per container                           | The container's memory limit      | `kind: manifest` for a limit raise when the owner confirms growth is legitimate; otherwise a finding for the owner, because a leak is not a sizing bug |
| Namespace quota (UC-8)            | `kube_resourcequota` used, through Managed Prometheus, where the kube-state-metrics package is on | The quota's `hard`                | `kind: manifest`: a quota change in the tenant's declaration, or an owner finding                                                                      |
| Project quota (UC-8)              | `serviceruntime.googleapis.com/quota/allocation/usage` per region and metric                      | The matching `quota/limit` series | `kind: manual`: a quota increase request; the stockout SOP's instant 90% check stays as it is                                                          |

The memory series needs care the others do not. A fast-allocating service goes from 60% to dead in
ninety seconds, which no daily sweep catches; a JVM or Go service idles at 92% of its limit by
design. The forecast distinguishes "high" from "rising toward the wall", but on a short horizon and
at native resolution; the daily sweep's version of this series is the slow climb, and the fast one
is the acute tier's.

#### The use-case map

The prediction-plane design catalogues ten use cases. Each is the same three primitives — signal,
quantile trajectory, reference — in a different arrangement. This table places each in kube-agents.

| Use case                               | Signal, reference                                                                                        | In kube-agents                                                                                                                                                                                                                                                                                                                 |
| -------------------------------------- | -------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| UC-1 OOM                               | Working set against `limits.memory`                                                                      | First series.                                                                                                                                                                                                                                                                                                                  |
| UC-5 Storage exhaustion                | Volume used bytes against capacity; time-to-full as a distribution ("q10 says 4 days, q90 says 31")      | First series. A slope break in the fill rate is itself worth flagging: a retention regression shows there long before capacity does.                                                                                                                                                                                           |
| UC-8 Capacity and quota                | Node count, requested against allocatable, quota usage against quota                                     | First series (node pool, namespace and project quota). The long-horizon regime (reservations, committed use) is a later report, not a finding.                                                                                                                                                                                 |
| UC-10 Suppression and window selection | An existing alert annotated with the band forecast before it fired; the quietest window in the next 72 h | Later. Suppression becomes a "was this expected?" field on the acute tier's incidents, and severity is downgraded, never the signal. Window selection answers "when should this node pool upgrade run?" for the upgrade-readiness work.                                                                                        |
| UC-4 Slow leak                         | The per-period minima (trough envelope) of memory or file descriptors                                    | Later. It answers "leak or load?", the question Job B asks of every memory finding; forecasting the trough rather than the raw series separates the two.                                                                                                                                                                       |
| UC-9 Error-budget burn                 | Burn rate integrated over the rest of the SLO window, against the budget                                 | Later, where Cloud Monitoring SLOs are defined.                                                                                                                                                                                                                                                                                |
| UC-6 Backlog and deadline              | Arrival and service rate against a drain deadline                                                        | Later, and only where a queue exports both series; stock GKE exports neither.                                                                                                                                                                                                                                                  |
| UC-7 Counterfactual canary             | Post-deploy metrics against a forecast from pre-deploy history                                           | Later. A synthetic control needing no second fleet; a candidate input for the rollout checks the upgrade work runs.                                                                                                                                                                                                            |
| UC-3 Anomaly bands                     | An observation against the forecast's own q10–q90 band, alerted on violation density                     | Not here. The fleet anomaly checks own anomalies; they may consume L1 bands. The prediction-plane design puts it last for the same reason: no proximity screen, so everything must be forecast, and it is the use case most likely to be muted.                                                                                |
| UC-2 Predictive autoscaling            | Request rate or CPU against provisioned capacity, with known-future covariates                           | Not here. This is the second divergence between the source designs: the prediction plane treats it as a policy on the substrate, and kube-agents treats scaling as a controller's job (HPA, KEDA, the cluster autoscaler). A finding may propose a scheduled `minReplicas` change as a pull request; the agent does not scale. |

#### Where not to forecast

A generic forecasting substrate invites being pointed at everything. These classes are
deliberately absent, and the collector refuses them rather than forecasting them badly:

| Anti-pattern                                                                       | Why it fails                                                                                | Use instead                                                                                                |
| ---------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| Deadlines: certificate expiry, key age, CA rotation, a maintenance exclusion's end | A countdown; there is nothing to forecast                                                   | Arithmetic, in the anomaly checks' expiry section, carrying the same lead-time field so the two read alike |
| Step-function, config-driven signals                                               | Changes are exogenous and instantaneous; history has no predictive content                  | Event-driven checks, the drift detector                                                                    |
| Rare discrete events (crash loops, node failures)                                  | Not a time series; the base rate is too low for quantiles                                   | The event watcher, reliability statistics                                                                  |
| Security and intrusion detection                                                   | Adversaries adapt, and statistically unusual is not malicious                               | Purpose-built detection                                                                                    |
| Series with under about two seasonal periods of history                            | Nothing to condition on; the model extrapolates the last slope                              | The instant checks until history accrues                                                                   |
| Control-plane load (etcd object count, API server latency)                         | A good second-wave candidate, but its limits are published GKE bounds, not declared objects | A later series class once the limit half can be read from a bounds table                                   |

### The forecaster

#### Why a foundation model at all

The forecast most operators run today is `predict_linear` in PromQL: a straight line through the
last few hours, extrapolated. For a volume filling at a steady rate it is right, cheap, and already
present. It fails on the series that matter at fleet scale because those series are not straight
lines. A working set that grows on weekdays and falls at the weekend, a batch namespace whose quota
use spikes nightly, a node pool that breathes with traffic: a linear fit through any of those either
cries wolf every Friday or misses the crossing by a week. A model that reads the shape of the
series is what turns a forecast into something a person will still trust after the third finding.

The classical alternatives — Holt-Winters, ARIMA, Prophet — read shape, but they need per-series
fitting, per-series seasonality declarations and a warm history before they produce anything.
Operating a fleet of them is the hard part, and it is what time-series foundation models remove:
they are pretrained once on a large corpus of series and forecast a new series **zero-shot**, from
its own history alone.

#### Why TimesFM 2.5

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
  head up to 1,000 steps. The breach distribution in the finding is read straight off those paths.
  A point forecast alone could not produce it.
- **Ecosystem.** Checkpoints on Hugging Face (`google/timesfm-2.5-200m-pytorch`, and a transformers
  port), PyTorch, Flax and MLX backends, a covariate extension, a fine-tuning path, and an official
  agent `SKILL.md` in the repository — the same artifact shape this harness ships its own skills in.
  Google also serves the same model as BigQuery's `AI.FORECAST` and on Vertex AI Model Garden,
  which keeps a managed path open (below) without changing the model.

The forecaster sits behind one interface — context windows and a spec in, quantile matrices out —
with more than one backend: TimesFM, a seasonal-naive and linear baseline, and a constant forecast
for degenerate series and for inference failure. The baseline is a first-class backend, not a test
fixture, because a series class on which the model cannot beat it should ship on the baseline, and
that is only knowable if the baseline can run in shadow beside the model. Requests are batched by
spec, so the specs are drawn from a small fixed set rather than set freely per series: spec
proliferation is batch fragmentation.

#### Covariates and long cycles

Some series have structure their own recent history cannot show: a nightly batch window, a planned
migration, a quarter-end, the Christmas peak. The prediction-plane design treats known-future
inputs as first-class (its UC-2 is built on them) and specifies a fallback for models without
native support: forecast without the covariate, then apply a simple adjustment keyed on it, fit on
past occurrences of the same event. TimesFM 2.5's repository ships in-context covariate regression
(`forecast_with_covariates`, the XReg extension), which fits a linear model on the covariates
jointly with the forecast; 3.0's native multivariate path is licence-blocked. The first five series
stay univariate.

A yearly effect is not a reason to feed the model three separate series — last day, last month,
same month last year. A decoder-only model takes one context; three windows spliced together hand
it two cliff edges. The two honest ways to show it a year are a single long context at coarse
resolution (a year at hourly alignment is 8,760 points, inside the 16,384 limit, and Cloud
Monitoring keeps system metrics for 24 months at ten-minute resolution) run beside a short
fine-resolution context, or a holiday calendar as a covariate. Which is better is a backtest
question; the [experiment](#experiment) runs the first.

#### What it is not

TimesFM does not know what a PersistentVolumeClaim is. It sees numbers and returns numbers. It does
not detect anomalies, though its prediction intervals can be used that way; it does not explain a
trend; it does not decide whether growth is legitimate. Every one of those is the agent's job, and
the [pipeline section](#how-a-prediction-reaches-people) keeps them there. A published evaluation of
foundation models on real operational series also finds cases where zero-shot forecasts do poorly
even with tuned context and horizon, which is why the [order of work](#order-of-work) puts a
backtest on the fleet's own series before any finding reaches a ledger.

#### Alternatives weighed

| Alternative                                            | What it offers                                                                                                                                            | Why not the primary                                                                                                                                                                                                                                                                                                                                               |
| ------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `predict_linear` in Managed Prometheus, seasonal-naive | Already there or two lines of code, free, right for monotone or strictly periodic series.                                                                 | Linear is blind to seasonality; seasonal-naive is blind to trend. Both are **baselines the backtest compares against**, and if one wins on a series class, that class ships on it (see [Order of work](#order-of-work)).                                                                                                                                          |
| Cloud Monitoring forecast conditions                   | A metric-threshold alerting policy with `forecastOptions` predicts a crossing within a window of 1 hour to 2.5 days, trained per series, no model to run. | The horizon caps at 2.5 days, which covers the acute tier and none of the others; each series needs a policy provisioned in advance, and the output is an alert, not an attributed finding with a remediation. It is a fine **input** for the acute tier through the Pub/Sub adapter's existing alert route, and a candidate remediation the agent could propose. |
| BigQuery `AI.FORECAST`                                 | The same TimesFM model, managed, forecasting millions of series in one SQL statement, with `AI.DETECT_ANOMALIES` alongside.                               | Needs the metrics exported into BigQuery first, a second data path with its own cost and retention, and BigQuery is a source several SOPs forbid. The right answer for a very large fleet once the mode has earned its place; not the first build.                                                                                                                |
| Vertex AI Model Garden endpoint                        | Managed serving of the same weights.                                                                                                                      | Per-call cost and egress for a model small enough to run beside the agent. Kept as the option for an install that forbids new in-cluster workloads; configuring it is a recorded data-egress decision.                                                                                                                                                            |
| TimesFM 3.0                                            | Native multivariate forecasting and past-and-future covariates.                                                                                           | Non-commercial weights. Revisit if relicensed.                                                                                                                                                                                                                                                                                                                    |
| Tabular foundation models (TabPFN and kin)             | Classification over features, which is the shape of "will this pod fail" rather than "when does this series cross".                                       | A different question, with licence terms that restrict the current weights; a later document, if a failure-classification mode is ever wanted.                                                                                                                                                                                                                    |

#### Where it runs

The forecaster is a **separate Deployment with no credentials**, on the pattern the memory store's
pods already follow: its own image pinned in [`images.json`](../../images.json), an `enabled:`
toggle in the chart alongside the existing optional components in
[`values.yaml`](../../charts/kube-agents/values.yaml), a manifest under the operator's integrations
tree, weights baked into the image so nothing reaches out to a model hub at start, and a
NetworkPolicy that admits only the agent's sandbox. It takes arrays and returns quantiles. It
cannot read Monitoring, cannot reach a cluster, and holds nothing worth stealing. If it is down, the
sweep falls back to the baseline backend, marks its findings so, and the instant checks keep
running: the predictive mode never makes the reactive and proactive ones worse.

The alternative, loading the model inside the sandbox image, was rejected: 800 MB of weights
against the image layer budget, and a model process sharing a pod with the agent's shell, for no
gain in trust. The split keeps the credentialed read where the relay's policy already governs it
and the model where a policy has nothing to govern. The prediction-plane design's split-inference
shape — the model on a GPU node pool, control on CPU — is available to a large fleet as a
deployment choice, since the interface is a batched call either way.

### Where the series come from

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
and the ledger groups findings per cluster family and region, as the anomaly checks require. Series
labels can carry sensitive identifiers; whatever the capability keeps inherits the retention and
access of the Monitoring data it came from.

### How a prediction reaches people

A prediction becomes work through paths that already exist: a ledger finding and a pull request for
most lead times, and an incident inject when the breach is close. Drift detection split its problem
into two jobs: computing the diff, which is mechanical, and judging it, which is where an agent
earns its place. Forecasting splits the same way, and for the same reason.

**Job A: compute the forecast.** Code, not a prompt. A sweep enumerates the enabled series per
cluster, triages them, reads the admitted ones through the relay, builds each context window,
sends the batch to the forecaster, and applies the decision rule against the declared limit. What
comes out is a candidate list with every field in the finding shape filled from data. A model call
should never be inside the agent's reasoning loop for this; a `kubectl top` sampled three times is
what the cost SOP does today, and the relay's own rationale for replacing it applies with more
force to a forecast.

**Job B: judge the forecast.** The agent, in the audit's session. For each candidate it asks what
code cannot: is the growth legitimate (a StatefulSet that is meant to accumulate) or a defect (a
log directory nobody rotates)? Is the limit the thing to change, or the consumer? Is there a
declaration in the GitOps repository to change, and what should it say? Is the finding new, or the
same trend as last week with a shorter lead time? The answers set the remediation kind and write
the recommendation. This is also where the agent applies the declared-intent rule the
obtainability audit already has: a repository that declares a claim's growth expected reclassifies
the finding rather than raising it. The prediction plane predicts _that_ a signal moves, not _why_;
the why is Job B.

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
tier alone — a short-horizon check every few hours, the full sweep daily — because a 30-day
forecast does not need refreshing every hour.

#### Triage

A mid-size cluster emits hundreds of thousands of series, and a fleet multiplies that. Most are
nowhere near informing a decision: a container at 12% of its memory limit on a flat slope does not
need a quantile forecast to establish that it will not run out. Deciding what to forecast is a
larger engineering problem than forecasting, and the prediction-plane design answers it with a
cascade of screens, each more expensive than the last:

| Tier | Screen                                                                                         | Admits                   |
| ---- | ---------------------------------------------------------------------------------------------- | ------------------------ |
| T0   | Is the series in an enabled class, and fresh?                                                  | Everything registered    |
| T1   | Proximity: `(limit − now) / σ_recent`                                                          | Drops the large majority |
| T2   | Motion: a robust slope, extrapolated to the limit                                              | Drops most of the rest   |
| T3   | Cheap forecast: seasonal-naive with an empirical residual band; disagreement with T2 escalates | A few percent            |
| T4   | TimesFM                                                                                        | The admitted set         |

Three rules make it safe as well as cheap. A series once admitted stays admitted for a cooldown, or
it flickers across the T1 boundary. A small random fraction of screened-out series is forecast
anyway; that is the only measurement of the triage's own false-negative rate, and without it the
triage cannot be falsified. And the inference budget per sweep is a fixed number that triage fills
by priority, so under pressure the sweep forecasts fewer low-priority series rather than running
long. The static-limit series are cheap precisely because T1 has a limit to measure proximity
against; a band reference has none, which is the cost reason UC-3 sits outside this design.

#### The decision rule

Given the quantile trajectory and the limit, the sweep computes the first step at which each
quantile crosses, interpolated within the step, and requires the crossing to hold for several
consecutive steps, since a single step poking over the line at the horizon's end is usually noise.

Which quantile counts is not hardcoded. Acting is right when the expected cost of acting is below
the expected cost of not acting, which for a crossing reduces to acting when
`P(breach) > C_fp / (C_fp + C_fn)`. A criteria entry therefore states a false-positive and a
false-negative cost per remediation class, even as rough relative numbers, and the threshold
probability follows. A larger PVC request costs almost nothing if unneeded and a full database
volume is an outage, so that class acts on a low probability; a project quota increase or a
data-volume migration needs near-certainty. An operator who insists can still pin a quantile.

A finding moves `clear → pending → raised`, with `pending` requiring the crossing in several
consecutive sweeps and the return to `clear` requiring a wider margin than raising did. Without
that asymmetry a series sitting near the boundary flaps, and the ledger fills with a finding that
opens and closes weekly. Dwell trades lead time for precision and is the main criteria knob.

Correlated findings collapse. A node pool nearing its maximum and every workload on it growing are
one finding with the rest attached as evidence; without that, the first real trend produces a page
of findings nobody reads.

#### On the vehicle

The capability is audit-shaped and maps onto the vehicle's requirements without special cases:

| Requirement      | For this capability                                                                                                                                                                                                                                                                                                                                                                            |
| ---------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| R1 Pre-defined   | The five series, the three horizons, a 90% limit fraction and cost weights per remediation class ship as defaults. Quiet rather than thorough: a fresh install should see a handful of findings, not a page.                                                                                                                                                                                   |
| R2 Scheduled     | One roster job for the daily sweep; the ledger is its record; a clean sweep is silent.                                                                                                                                                                                                                                                                                                         |
| R3 Triggerable   | "When will `orders-db-0`'s volume fill?" is a scoped run answered in the thread. "Run the capacity runway now" is the shipped stream marked due. The acute tier is the event path above, and a Cloud Monitoring forecast alert arriving through the Pub/Sub adapter is another event that wakes it.                                                                                            |
| R4 Customizable  | Criteria: the series enabled, the limit fraction, the three horizons, the cost weights, dwell, per-family exclusions, the alignment window, and each class's shadow or reporting state. The procedure, the red lines and the model are image-owned.                                                                                                                                            |
| R5 Self-learning | The evidence class this capability has that no other audit does: every finding is a prediction that is later true or false. A finding whose breach did not arrive, or arrived when the forecast said it would not, is a precision signal the agent can propose criteria changes from — scored as the [intervention problem](#the-intervention-problem) requires. Narrowing stays propose-only. |
| R6 Durability    | As the vehicle specifies; nothing here needs more.                                                                                                                                                                                                                                                                                                                                             |
| R7 Write path    | As the vehicle specifies. The model's outputs are never written to criteria; only a person's confirmation moves a threshold or promotes a class out of shadow.                                                                                                                                                                                                                                 |

The standing state the A2A bus provides is a natural home for the current forecast per series: a
state topic on the pattern the [payload spec](spec-a2a-payloads.md) already sketches for
upgrade-readiness verdicts, so a later question in chat starts from the last sweep rather than a
cold collection. That is an enhancement, not a dependency.

### Honesty about the future

A finding about the present can be checked by re-reading the object. A finding about the future
cannot, and a mode built on forecasts is only as useful as its record of being right.

#### Backtest against baselines before the first finding

On the fleet's own series, a rolling-origin backtest: cut each series at points in the past,
forecast forward from what was known at that point only, and compare against what happened. Point
in time is strict; a context that leaks one future point invalidates the result. Report the
forecast-level and decision-level [metrics](#metrics) per series class against three baselines:
the last value repeated, seasonal-naive and linear. Score the peak of each forecast window against
the peak that came, both the miss and the overshoot, as well as the per-point error. The last value
is the bar that matters most, because it is what the proactive mode already sees; the
[experiment](#experiment) found TimesFM's 8-hour peak indistinguishable from it. This is the
experiment the [order of work](#order-of-work) gates on.

#### Calibration

Zero-shot quantiles are calibrated on average over the model's training data, which promises
nothing about one install's volume fill. If the q90 is really a q60 for some series, every
threshold derived from it is silently wrong, and the cost rule above is denominated in
probabilities that do not mean what they say. The capability therefore keeps, per series group
(per series once history allows), a rolling record of forecast quantile against realised value at
several horizon steps, since coverage degrades with horizon and one aggregate hides it. From that
record it widens or narrows the model's interval by the factor that restores nominal coverage, a
conformal adjustment that is cheap, distribution-free and does not touch the model. A group whose
factor is large is telling the operator the model does not understand it.

Each group is in one of three states. `unmeasured`: too little history, findings are advisory and
marked low confidence. `calibrated`: coverage within tolerance, findings may carry a pull request.
`miscalibrated`: persistent error the correction cannot fix, excluded from pull requests and a
candidate for the baseline backend. A deploy or a limit change resets the group to `unmeasured`
rather than carrying a confidently wrong correction forward.

#### The intervention problem

If the mode works, its predictions stop coming true. It forecasts a full volume, someone merges the
expansion, and the volume never fills. Scored naively, that is a false positive; scored that way
for long enough, a working capability looks broken, someone tunes it to be less sensitive, and it
becomes broken. There is no complete answer, and the prediction-plane design gives three partial
ones that this capability adopts:

- **Record the counterfactual.** Every finding stores the state at the time it was raised, and
  every later sweep records whether the series crossed, when, and whether a remediation changed the
  limit in between. The question scored is whether the finding was justified, not whether the
  disaster occurred. The ledger already computes a run-over-run delta; this adds an outcome to each
  closed finding, and each ledger rewrite reports the class's record — "12 of 14 PVC forecasts in
  the last quarter crossed within the predicted window, 3 were remediated first".
- **Shadow classes.** A class in shadow produces unconfounded ground truth, because nobody acts on
  it. Every class starts there.
- **Historical replay.** A corpus of recorded series with known crossings, replayed offline, is
  unconfounded by construction and is the [replay fixture](#evaluation) below. It measures recall
  well and precision poorly, since it holds the incidents that happened and not the ones prevented.

The consequence, stated so nobody tunes against it: precision is measured on replay and shadow,
never on the outcomes of findings someone acted on.

#### Quiet defaults, conservative costs

The shipped cost weights err toward silence. The first weeks on a fleet are for tuning down, not
up; the vehicle's R1 says why.

#### Know the ways a series lies

The model accepts anything and returns confident nonsense when fed nonsense. The collector handles
each of these before the model sees the series, and the finding carries a quality flag when one
applied (`gappy`, `short_context`, `post_restart`, `low_variance`, `stale`, `clipped`):

- **Resolution and downsampling.** The context should span at least two periods of the dominant
  cycle, which is more points at native resolution than the model takes, so resolution follows the
  horizon: short horizons at native resolution, long ones aligned to five minutes, ten or an hour.
  The aligner preserves what the series class cares about — `max` for an exhaustion series, since a
  five-minute mean hides the spike that kills the pod; `min` for a trough envelope; `mean` for load.
- **Three kinds of gap.** A scrape miss is short and interpolated. An absent series — the pod did
  not exist — truncates the context and is never zero-filled. A zero is a real measurement.
  Zero-filling a restart gap hands the model a cliff edge, and it extrapolates one. A series with
  too little unmasked context is skipped with a limitation, not forecast.
- **Restarts and regime changes.** A restart resets memory; a deploy changes a slope; a PVC
  expansion resets a fill; a limit raise moves the threshold. The collector cuts the context at the
  most recent change in the declared limit, the object's generation or the container's restart
  count, and forecasts from the segment that reflects the current regime. The leak series is the
  exception: it wants to see across restarts, since a restart that resets a leak is the confirming
  evidence.
- **Controllers that hide the trend.** Covered above: forecast the quantity a controller consumes
  (node count), not the ratio it defends (utilization).
- **Hard caps and flat series.** A series already at its cap is flat and forecasts flat; the
  instant checks catch the cap, the forecaster is for the approach. A near-constant context is
  short-circuited to a constant forecast rather than sent to the model.
- **Bounds.** A ratio cannot exceed one and memory cannot be negative; forecasts are clipped to
  the series' bounds.
- **Series too short or too young.** Fewer than 32 points is below the model's floor and, more to
  the point, a day-old claim has no trend worth reading. The minimum context is a criterion, with a
  floor the image owns.
- **Counters.** A cumulative counter is rate-converted, with resets handled, before forecasting;
  forecasting the raw counter produces a straight line with no information in it. The limit is
  compared to the level, not the rate.

### Evaluation

#### Metrics

Average accuracy is not the goal; accuracy near the limit is. A model with a good overall error
that is optimistic in the upper tail is worse than useless for a memory series. Two families of
metric, per series class and horizon step:

- **Forecast level.** Weighted quantile (pinball) loss, the primary quality measure since the
  capability consumes quantiles; empirical coverage at each nominal level; and MASE against
  seasonal-naive, the relevance measure — above one, the model is losing to a two-line baseline on
  that class.
- **Decision level.** Precision and recall of the breach decision; the lead-time distribution of
  true positives, with a per-class minimum useful lead time below which a correct finding counts as
  a miss; and coverage restricted to windows where the series was near its limit, which is the
  number that predicts real performance and is usually worse than unconditional coverage.

#### The eval case

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
no waiting, and is the red case the first implementation must turn green three times. The recorded
series double as the start of the replay corpus: real recordings with known crossings, long
stretches of normal operation (precision is measured against those), and synthetic edge cases for
restarts, gaps, step changes and flat series.

**A live fixture second.** One workload on a seeded cluster that writes into a claim at a fixed
rate, sized so the claim never actually fills inside the eval cadence and reset on a schedule, gives
the fleet a real trend after about a week of history. It is a day-N fixture in the sense the fleet
catalogue already uses: a case that reads it cannot pass on a fresh apply, and says so. It belongs in
the same conversation as the other fixtures the fleet is being asked to grow, and its cost is one
small claim per eval project.

The case lands in the existing `capacity` domain; a new domain needs a presubmit seat before it
can exist, which a first case cannot earn. Registration follows the nightly-first rule the eval
rule sets out, and the case is never added to the blocking roster in the change that makes it pass.

### Order of work

Each phase is a separate change with its own live validation. The prediction-plane design names
five prerequisite spikes; they are folded into the phases they gate.

1. **The collector, as a library and a sandbox command.** Written to the relay's contract, producing
   aligned series with limits attached for the five series classes, with a `limitations` record per
   cluster. No model yet: its output is the baselines' input as well as the forecaster's. This
   phase is also the metrics source the anomaly checks say they need, built once. It carries two
   spikes: whether point-in-time series for past breaches can be reconstructed at the resolution
   needed (if not, recording must start now, which makes it the most urgent spike despite looking
   the least interesting), and how much gap handling, counter conversion and the choice of
   downsampling function change forecast quality — which decides how much engineering the context
   rules above deserve.
2. **The forecaster service and the backtest.** The TimesFM 2.5 image, the chart toggle, the
   baseline backends, and the rolling-origin backtest on a real fleet's series. It carries the
   inference-cost spike (latency and throughput at realistic batch sizes on the install's own CPU
   shape, which sizes the triage budget) and the quality spike (on which series archetypes — sawtooth
   memory, diurnal load, bursty queues, monotone disk — the model beats seasonal-naive). **Decision
   gate:** a series class ships on TimesFM only where the backtest shows it beats the baselines on
   breach precision at equal recall. Where it does not, that class ships on the baseline, and the
   finding shape is unchanged — the model is a provenance field, not the design. If no class clears
   the gate, the mode still ships on the baseline and this document records why.
3. **The capability.** The SOP, the roster job, the ledger stream, the triage and decision rule,
   the opt-in toggle and the per-cluster probe with its reprobe schedule, calibration state, the
   remediation kinds per series, every class in shadow, the replay-fixture case run red then green,
   and the criteria on the vehicle once its store exists (image-owned defaults until then).
4. **The acute tier.** The `forecast-breach` inject kind, the short-horizon sweep, and the session
   path's handling of a finding that is a forecast rather than an event.
5. **Chat.** "When will X run out" as a scoped run, and the state topic so the answer starts from
   the last sweep.
6. **Further references.** The leak series (derived trend), expected-spike annotation and window
   selection (band), error-budget burn (budget), and counterfactual rollout checks, each on its own
   case. The covariate spike — what XReg gives against the residual fallback on a real scheduled
   event — gates any series that needs a covariate.

Phases 1 and 2 produce no agent behaviour and need no case; phases 3 to 6 each start from one. Each
phase's gate is a backtest or eval record, not a demonstration.

### Success measures and risks

The prediction-plane design sets targets for its first version. Adapted to a capability whose
action is a pull request:

| Measure        | Definition                                                                                           | Target                                                                                |
| -------------- | ---------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| Lead time      | Median time from finding to predicted breach, for true positives                                     | At least ten times the remediation latency — for a pull request path, days, not hours |
| Precision      | Share of raised findings that would have crossed absent intervention, on replay and shadow           | 0.8 for a class allowed to open pull requests                                         |
| Recall         | Share of real breaches of a covered class found with usable lead time                                | 0.5 in the first version                                                              |
| Coverage error | Absolute gap between empirical and nominal coverage for q10, q50, q90, per group, rolling seven days | 0.05 after calibration                                                                |
| Cost           | The forecaster's compute as a share of the install's own                                             | 1%                                                                                    |
| Reversal rate  | Share of merged predictive remediations reverted within a week                                       | 0.02                                                                                  |

| Risk                                                         | Impact                                                  | Mitigation                                                                                        |
| ------------------------------------------------------------ | ------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| The model does not beat seasonal-naive on the fleet's series | The model half of the thesis fails                      | The phase 2 gate; the baseline backend keeps the capability alive either way                      |
| Triage false negatives hide a real breach                    | A missed breach in a mode claiming to predict them      | The random sampling floor makes the rate measurable; the ledger reports it                        |
| Intervention confounding lowers measured precision           | The capability is tuned into uselessness                | Precision on replay and shadow only, stated in the ledger's own record                            |
| Findings become noise                                        | The stream is muted and takes the good findings with it | Shadow first, quiet costs, dwell, collapse of correlated findings, UC-3 left out                  |
| Inference cost exceeds budget at fleet cardinality           | The capability cannot run widely                        | The inference-cost spike; a fixed budget that triage fills                                        |
| Model version drift                                          | A silent quality regression                             | A pinned image digest, the model as provenance, calibration as the regression detector            |
| A covariate is wrong about the future                        | A confidently wrong forecast                            | Record covariates with the forecast so a postmortem can tell a covariate error from a model error |

### Out of scope

- **Anomaly detection on the present value.** The fleet anomaly checks own it; they may read the
  forecaster's bands.
- **Predictive autoscaling and any direct actuation.** A controller's job (HPA, KEDA, the cluster
  autoscaler); the agent proposes limits through pull requests, it does not scale or resize. The
  prediction plane's `enforce` mode has no counterpart here.
- **Root-cause analysis.** The forecast says that a series moves; Job B's judgement about why is a
  recommendation, not a diagnosis.
- **Failure classification.** "Will this pod fail" from features is a different model family and
  a different licence conversation.
- **Deadlines.** Expiry and rotation are countdowns; they share the lead-time field and nothing else.
- **Multivariate forecasting.** Forecasting pairs independently and combining them understates the
  joint tail; the features that fix that need TimesFM 3.0.
- **Fine-tuning.** Zero-shot is the premise. A fleet whose series defeat it is a reason to revisit
  the model choice, not to run a training pipeline inside an install.

### Open questions

- **Precision on a real fleet.** The day-ahead backtest has run ([Experiment](#experiment)), but no
  series crossed a limit in its window, so breach precision, the number the proposal rests on,
  is still unmeasured.
- **Sweep cost.** A fleet of a hundred clusters with a few thousand mounted claims is a few thousand
  series of two thousand points each; on CPU that is minutes, not hours, by the published
  throughput figures, but the figure that matters is the one measured on the install's own node.
- **Calibration grouping.** Short-lived series never accumulate the residual history per-series
  calibration needs. Grouping by class and workload is the proposed answer; the right granularity
  is empirical.
- **Multi-resolution against long context.** Whether two specs per series (a week fine, a year
  coarse) beat one long context depends on the inference-cost curve and the backtest.
- **Metric packages that are off.** The kube-state-metrics and kubelet packages are per-cluster
  choices. Whether the capability should recommend enabling them as its own finding, or stay silent
  on a cluster that lacks them, is a criteria decision the first fleet will settle.
- **Autopilot.** System metrics are present; node pools are not the operator's to size. The node
  pool series does not apply, and the finding shape needs a way to say so per cluster mode.
- **Where the hit-and-miss record lives.** The ledger issue's hidden marker carries findings across
  runs; whether it can carry outcomes too, or whether the record wants the criteria store or a state
  topic, is a question for the change that builds phase 3. The replay corpus wants a longer
  retention than either, and its own storage.
- **Who owns a predictive remediation.** A platform-owned capability proposing changes to
  application-owned limits needs an ownership answer before its first pull request surprises
  someone; the team label names the reviewer, not the decision.
- **The relay's POST shapes.** MQL `timeSeries:query` is refused today, by design. The GET shapes
  suffice for the five series; a later series that needs MQL reopens the relay design's second
  question, not this one.

## Experiment

The experiment's code, method and full results are kept out of this repository, on the
`experiment/timesfm-backtest` branch of the author's fork:
[README](https://github.com/dshnayder/kube-agents/blob/experiment/timesfm-backtest/bench/experiments/timesfm-backtest/README.md) for the method and the commands to rerun it, and
[RESULTS.md](https://github.com/dshnayder/kube-agents/blob/experiment/timesfm-backtest/bench/experiments/timesfm-backtest/RESULTS.md) for the go/no-go write-up.

It is a rolling-origin backtest of TimesFM 2.5 against seasonal-naive and linear baselines, on real
series from the 11 evaluation clusters and from one customer staging cluster. At each origin the
model reads the days before it, forecasts the next 24 hours of five-minute points, and the forecast
is scored against what happened.

### What it tested

The starting proposal was a single comparison: the model reads T-8d to T-1d and predicts day T.
The experiment keeps that as its `tfm-7d` arm and changes three things around it. It runs every
midnight with enough history as an origin, because one day is one draw. It runs the same origins
through `snaive-1d`, `snaive-7d` and `linear-7d`, because the decision rule above makes
seasonal-naive the bar a forecast has to clear. And it adds a 28-day arm, because a model sees a
weekly cycle only after it has seen two.

The second proposal was to feed three windows: the last day, the last month, and the same month a
year earlier, so a December spike is visible in December. The motivation holds; the mechanism does
not fit the model. TimesFM 2.5 forecasts one univariate series at a time, so three windows cannot
be inputs side by side. The same effect comes from one long context (a year at hourly resolution is
8,760 points, inside the 16,384 the model accepts), from combining forecasts made over different
windows, or from a holiday calendar passed as a covariate through XReg. The experiment tests the
combination as `tfm-ens`, the mean of the 1-, 7- and 28-day forecasts. The yearly window it cannot
test: no cluster in reach is a year old, and Cloud Monitoring keeps five-minute points for six
weeks and ten-minute downsamples for 24 months, so the collector design above has to archive its
own series before year-over-year input exists at all.

### What it found

The first run covered the 11 evaluation hosts over 41 days, 2026-08-13 to 2026-09-23: 93 series
that live long enough to forecast. The production install was collected too and left out; it
does little beyond occasional pull-request tests, and its near-idle series flattered every score.
The experiment's README has the tables and caveats. It found five things; the last is the go/no-go
reading.

**The forecaster clears the baseline bar on the day's shape.** Zero-shot TimesFM had a median
MASE of 0.61–0.63, against 0.92 for seasonal-naive and 0.97 for linear. It beat seasonal-naive
on 78–82% of (series, origin) pairs, and its quantile loss was about 40% lower. It was strongest
in the first hour, at 0.08 against 0.71. The 7- and 28-day intervals held close to their nominal
80% coverage. Memory gained most. CPU requests, which move in steps at deploys, gained least.

**Context length is not the lever.** The proposal's 7-day window came within 0.01 MASE of the
28-day one. The ensemble of windows added nothing over the longest window alone. Seasonality
longer than the context therefore has to come from the other two routes in
[Covariates and long cycles](#covariates-and-long-cycles): long hourly context from archived
series, or a covariate. That makes the collector's archive a prerequisite for year-over-year
forecasting.

**Per-point quantiles do not bound a peak.** The day's highest q90 covered the real daily
maximum only 26–29% of the time, against 81% for seasonal-naive. It flagged 4–21% of the days
that set a new high. A breach test that compares the q90 path to a threshold is therefore
overconfident by construction.

[Calibration](#calibration) must include a peak-level correction as well as per-point interval
scaling. The experiment tried one, a split-conformal raise from each series' own earlier misses.
It restored 86–95% peak coverage. Once corrected, TimesFM's peak alerts were only modestly
better than the baselines': 18% precision at 92% recall for the 7-day arm, against 13% at 100%
for seasonal-naive, on 12 events.

**The breach gate is still open.** No series reached 90% of its limit in the window, so the
[order of work](#order-of-work)'s phase-2 gate, breach precision at equal recall, has no events
to score. It needs a longer window, a fleet with real pressure on its limits, or replayed past
incidents.

**A day ahead, the forecast is within −10%/+5% of the actual value 57% of the time, not 90%.**
Comparing each forecast hourly value with the value measured in that hour, 57% landed in the band,
19% were more than 5% too high and 23% more than 10% too low. No series type came near 90%: node
count reached 71%, memory 68%, CPU 36–52%. TimesFM beat repeating yesterday on every series type
(45% in band, 32% too high), and accuracy was far higher in the first hours: 80% for memory and
for node count one to six hours ahead. Forecasting 8 hours ahead roughly halved the typical hourly
miss: memory stayed within 2% high and 3% low, against 9% either way a day ahead; on a bad day (1
in 10) it was still up to 20% too high. A lower quantile (q30) halved the costly too-high share
for at most 5 points of in-band. Warnings on a forecast threshold crossing were rarely wrong but
caught 3 of 148 crossings that were new that day; the rest the proactive agent already sees.
On bursty clusters like these, day-ahead forecasts of CPU and memory are not accurate enough to
act on. One busy customer staging cluster forecast far better than the evaluation hosts (a bad-day
memory overshoot of 4% against 20%, 8 hours ahead), so the answer depends on the cluster. That is
why prediction is gated per cluster by a probe
**Eight hours ahead, TimesFM forecasts the last hour.** A later pass added the cheapest baseline
the backtest had left out: repeat the last hour's value for the whole window. It then compared the
peak each method forecast for the next 8 hours with the peak that came, since a warning is about
the peak. On the staging cluster, 129 cut points with 7 days of history, the forecast peak against
the actual peak was (median, then the worst miss and the worst overshoot in 1 window in 10):

| Series           | TimesFM          | Last hour repeated | Same hours yesterday |
| ---------------- | ---------------- | ------------------ | -------------------- |
| Cluster CPU used | −9% (−23%, +2%)  | −9% (−23%, +2%)    | 0% (−21%, +25%)      |
| Container CPU    | −7% (−32%, +1%)  | −6% (−27%, +1%)    | 0% (−18%, +20%)      |
| Memory           | −1% (−8%, 0%)    | −1% (−7%, 0%)      | 0% (−6%, +6%)        |
| Node count       | −10% (−17%, +1%) | −9% (−17%, +2%)    | +2% (−17%, +17%)     |

On the staging cluster, TimesFM and the last hour are the same forecast to within a point, and on
the evaluation hosts within four (cluster CPU −31% against −27%). Their average error agrees too:
6.8% against 7.5% for staging cluster CPU over 8 hours, 9.4% against 9.5% over 24. The staging
cluster did not forecast better because the model read its load. Its load changes less: memory
moves 5% in a typical day and requested CPU 7%, against 22% and 54% on the evaluation hosts. Any
forecast looks good on a quiet series. Where the staging load did rise, TimesFM missed it: about 40%
of 8-hour CPU windows peaked more than 15% above the current value, and in those TimesFM forecast a
peak 18–20% below the real one. The experiment's `results/baselines.md` has every series and both
clusters.

Repeating yesterday is no substitute. It centres on the real peak and misses less, but in 1 window
in 10 it overshoots by 17–25% on the staging cluster and by up to 155% on the evaluation hosts, and
each overshoot is a false alarm. A last-hour forecast is not a forecast at all: it is the current
value, which the proactive mode already watches. So where TimesFM matches it, forecasting adds
nothing and costs a forecaster. The [probe](#enabling-predictive-mode-opt-in-per-cluster) must
therefore compare against both baselines, and a series class runs on TimesFM only where it beats
them on the peak. The comparison used the median forecast. The day's q90 was already shown above
to cover the real daily maximum only 26–29% of the time, so the upper quantiles do not rescue it.

([Enabling predictive mode](#enabling-predictive-mode-opt-in-per-cluster)). Trend-driven resources
such as disks remain untested: no disk on these clusters lived long enough to forecast.

The cost spike has its first data point. On one 14-core CPU replica, a batch of 64 series with a
288-step horizon took 3.6, 7.6 and 29 seconds at 1-, 7- and 28-day context. On an 11-core
container the same batches took 7.6, 20.7 and 70 seconds. Horizon length and batch size up to 64
do not change the time; only the padded context does. A thousand series forecast from 7 days of
history is 16 requests, two to six minutes on one replica, so forecasting never lags the horizon
it covers.

## Related

- [Zero-Shot Forecasting for Predictive Operations](https://gist.github.com/mastersingh24/ac4cce73bc57ae4a6d8e04a4ad2cb0e7)
  — Gari Singh's platform-agnostic prediction-plane design, merged here.
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
  the 2.5 API, the covariate extension, and the official agent skill.
- [Cloud Monitoring forecast conditions](https://docs.cloud.google.com/monitoring/alerts/metric-forecast)
  — the managed alternative for the acute tier.
- [BigQuery `AI.FORECAST`](https://docs.cloud.google.com/bigquery/docs/reference/standard-sql/bigqueryml-syntax-ai-forecast)
  — the same model as a managed function, for a very large fleet.

## Prior art

A survey of what shipped as of September 2026, so a reviewer can see where this proposal sits.
Three findings, and what each changes in the document.

**No Kubernetes or AI SRE agent forecasts.** K8sGPT, HolmesGPT, Komodor's Klaudia, kagent,
Cleric, Resolve AI, Traversal, Metoro and Datadog's Bits Investigation all run the same loop:
detect, investigate, remediate. HolmesGPT and Azure SRE Agent add scheduled health checks, which
is this repository's proactive tier under another name; Azure's published capacity-planning
example is a custom skill that flags quota above a fixed fraction, a current-value threshold.
Google's Gemini Cloud Assist "Proactive Mode" is background investigation triggered by an alert
or a cost anomaly, and its documentation does not use the word forecast. Two small vendors,
Hawkeye and Phoebe AI, claim prediction on a one-to-three-day horizon; neither documents the
mechanism. The mode this document names is not occupied.

**The observability platforms have the forecasting, without the agent.** This is where the
real prior art is, and one of them has the whole loop:

- [Dynatrace Davis AI](https://docs.dynatrace.com/docs/observe/infrastructure-observability/kubernetes-app/use-cases/predictive-operations)
  documents a "predictive Kubernetes operations" workflow that forecasts disk fill, resolves the
  owner, and opens a pull request changing the disk size in the service's configuration
  repository. Dynatrace runs it weekly over
  [some eight thousand of its own disks](https://www.dynatrace.com/news/blog/automate-predictive-capacity-management-with-davis-ai-for-workflows/).
  That is forecast, ownership, declarative remediation: the shape proposed here, inside one
  vendor's platform and data lake.
- [Grafana Cloud](https://grafana.com/docs/grafana-cloud/machine-learning/dynamic-alerting/forecasting/)
  ships metric forecasts with daily and weekly seasonality, one forecast per entity in a label
  set, and a documented disk-full pattern that alerts days before the fill.
- New Relic ships
  [predictive alerts](https://docs.newrelic.com/docs/alerts/create-alert/set-thresholds/predictive-alerts/);
  Datadog's Watchdog runs on Toto, its own time-series foundation model; Cloud Monitoring has the
  forecast alert condition weighed [above](#alternatives-weighed), with its 2.5-day cap.

**Time-series foundation models have been measured on Kubernetes metrics.**
[Parseable's benchmark](https://www.parseable.com/blog/zero-shot-forecasting) (April 2026) ran
Chronos, TimesFM, IBM's Tiny Time-Mixers and Toto against classical baselines on real pod
metrics: Toto led on high-frequency multivariate series, Chronos was the most versatile, and no
model handled a first-of-its-kind event zero-shot. The
[k0rdent FinOps Agent](https://cloudnativenow.com/contributed-content/building-finops-with-k0rdent-open-source/)
already runs Toto zero-shot over Prometheus and OpenCost series for cost and utilization
forecasts with p10, p50 and p90 intervals; it is the nearest operations agent built on such a
model, and it forecasts spend rather than failure.

What this changes here. The positioning stands: nothing combines a forecast, an attributed
finding and a declarative remediation in an agent that runs inside the install, and Dynatrace's
workflow is evidence that the loop works in production. Two adjustments follow. The backtest in
[Order of work](#order-of-work) phase 2 should run Toto and Chronos beside TimesFM, since both
publish open weights and Toto was trained on observability data; the forecaster's backend
interface makes the winner per series class a configuration, not a redesign, and each model's
licence terms are checked in that phase before it is pinned. And the benchmark's observation about
first-of-its-kind events is the same regime-change rule stated in
[Know the ways a series lies](#know-the-ways-a-series-lies), now with a measurement behind it.
