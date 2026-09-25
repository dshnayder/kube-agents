# Predictive operations: warning before a resource runs out

**Status:** proposal for review; nothing is built. A first experiment has run on real clusters, and
the [Experiment](#experiment) section reports it. The [Scope](#scope) section lists what an install
already has. Everything after it describes a direction for kube-agents, not a build plan with dates.

**Authors:** Dmitry Shnayder; Gari Singh, whose [prediction-plane design](https://gist.github.com/mastersingh24/ac4cce73bc57ae4a6d8e04a4ad2cb0e7) is merged into this one.

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

| Subsection                                                               | What it gives you                                                                       |
| ------------------------------------------------------------------------ | --------------------------------------------------------------------------------------- |
| [Scope](#scope)                                                          | what an install already has, and what this document adds                                |
| [The three modes](#the-three-modes)                                      | what "predictive" means, and which problems it is for                                   |
| [Three layers](#three-layers-forecast-prediction-action)                 | forecast, prediction and action, and which of them kube-agents takes on                 |
| [Enabling predictive mode](#enabling-predictive-mode-opt-in-per-cluster) | the opt-in switch, the per-cluster probe, and when a cluster starts or stops predicting |
| [What a predictive finding is](#what-a-predictive-finding-is)            | what a warning contains, which series come first, and the full list of use cases        |
| [The forecaster](#the-forecaster)                                        | why TimesFM 2.5, what it cannot do, the alternatives considered, and where it runs      |
| [Where the series come from](#where-the-series-come-from)                | where the metrics are read from, and why no new credentials are needed                  |
| [How a prediction reaches people](#how-a-prediction-reaches-people)      | how forecasts are computed, filtered and judged, and where the results go               |
| [Honesty about the future](#honesty-about-the-future)                    | how the forecasts are checked, corrected and kept honest                                |
| [Evaluation](#evaluation)                                                | the metrics, and the test case the first implementation must pass                       |
| [Order of work](#order-of-work)                                          | the phases, and the check that could stop the work after phase 2                        |
| [Success measures and risks](#success-measures-and-risks)                | the targets each phase is judged against, and what could go wrong                       |
| [Out of scope](#out-of-scope)                                            | what this is not                                                                        |
| [Open questions](#open-questions)                                        | what only building it or measuring it can answer                                        |

## TL;DR

This document proposes and evaluates a **predictive mode** for kube-agents. Today the agent is
**reactive**, fixing what a person reports, and **proactive**, finding problems that already exist
before anyone reports them. In predictive mode it warns before the problem exists. It uses
[TimesFM](https://github.com/google-research/timesfm), Google's open time-series forecasting model,
to forecast series such as CPU, memory, disk and node count. When a forecast shows a resource
running out, the agent raises an early warning with a proposed fix, before the outage.

The feature is **opt-in and disabled by default**. Once enabled, a cluster uses predictions only
after a probe, a test on the cluster's own history, shows its load can be forecast. It stops if its
predictions start going wrong.

What the evidence says so far: TimesFM's forecasts are not yet accurate enough to act on. On bursty
test clusters, forecasts a day ahead were too far off. On a real customer staging cluster, 8-hour
forecasts looked accurate, but only because the load changed little. Simply assuming the load would
stay at its current level was just as accurate, and when the load did go up, TimesFM did not predict
it. Forecasting is cheap, seconds per request on a CPU, so cost is not the obstacle; accuracy is.
Nothing is built yet.

## Summary

kube-agents has two operating modes today. In the **reactive** mode a person sees a problem and asks
the agent about it in chat. In the **proactive** mode the agent finds a problem nobody has reported
yet: scheduled audits look for configuration drift, version skew and security gaps, and an event
watcher responds to a Kubernetes warning event as soon as it appears. Both modes act on a problem
that already exists.

This document proposes a third mode, **predictive**, in which the agent acts on a problem that does
not exist yet. The evidence is a forecast, and the warning says: _this value will reach this limit
at about this time, with this confidence_. Examples are a PersistentVolumeClaim (a disk) filling at
its current rate, a node pool a week away from its autoscaler maximum, a container whose memory use
is climbing toward its limit, and a namespace approaching its ResourceQuota. Each of these is a
harmless number today and an incident later. The only question is whether someone notices in time.

The underlying gap is the same in every case. The cluster keeps a long history of each of these
values, but every check that watches them looks only at the latest value. A disk alert at 85% gives
minutes of warning on a disk that fills fast, and on a disk that fills slowly it becomes a ticket
that is ignored for three weeks. The history already shows where the value is heading; nothing
reads it.

The mechanism is a **forecast threshold breach**: a forecast shows a value crossing a limit. The
agent already knows the limits, because they are Kubernetes and GCP objects it reads today. Cloud
Monitoring already holds the history, and the agent can already read it through its credential
proxy. What is missing is a forecaster between the two, and a way to turn a forecast into a
warning and a proposed fix. The proposed forecaster is
[TimesFM 2.5](https://github.com/google-research/timesfm), Google's open-weight time-series model,
run as a service inside the install with no credentials of its own. It is a foundation model: it is
pretrained once on a large collection of time series and then forecasts a new series from that
series' own history, with no per-series training or tuning. This is called **zero-shot**
forecasting. Classical methods such as Holt-Winters or Prophet need to be fitted to each series
separately, and they need a long history first. TimesFM treats a disk created yesterday the same as
one created a year ago, and it is small enough to run on a CPU next to the agent. A warning follows
the same path every other finding follows: an entry in the audit log and, where a fix is possible, a
pull request that a person reviews and merges. The only thing that changes is _when_ the agent acts.
This matches the rule in the [workflow model](../architecture/04-workflow-model.md): a new trigger
changes when the agent wakes up, never what it is allowed to do.

**Opt-in, and decided per cluster.** Predictive mode ships disabled. When an operator enables it,
each managed cluster runs a probe: the forecaster replays the cluster's recent history and checks
how often it would have been right, and above all how often it would have warned about a problem
that never came. Only a cluster that passes gets predictions, and only for the types of series that
passed. Every prediction is later compared with what really happened. A cluster whose score drops
stops predicting, and the probe is rerun on the other clusters from time to time.
[Enabling predictive mode](#enabling-predictive-mode-opt-in-per-cluster) has the details.

**Why a false alarm costs more than a missed warning.** A forecast that is too high makes the agent
act on a problem that never arrives: someone reviews a pull request for nothing, or a person is
interrupted. After enough of these, people start ignoring every warning the agent raises, including
the real ones. A forecast that is too low only loses the early warning, because the proactive mode
still catches the problem when it happens. For this reason every threshold in the design is set to
stay quiet when in doubt.

**How a warning reaches people.** Code computes the forecasts, and the agent then judges them: is
the growth expected, and should the limit be raised or the consumer fixed? A problem that is days
away becomes a finding in the audit log and, where the limit is defined in Git, a pull request that
a person merges. A problem expected within the next 24 hours is sent into the same incident path the
event watcher already uses, so a person sees it the same day.
[How a prediction reaches people](#how-a-prediction-reaches-people) has the details.

**What the experiment found.** Forecasts are not yet accurate enough to warn on. On 11 bursty test
clusters, a forecast made a day ahead was close to the real value (no more than 5% too high or 10%
too low) only 57% of the time. That is better than assuming today will repeat yesterday (45%), but
far from good enough to act on. Forecasting 8 hours ahead instead of 24 halved the typical error. On
a real customer staging cluster, 8-hour forecasts were much closer to the real values, but mainly
because that cluster's load changed little during the day. Assuming the load would stay at its last
hourly value was just as accurate. When the load did go up, TimesFM predicted it would stay near its
current level, so it would not have raised a warning. Speed is not a problem: one CPU container
forecasts 64 series from 7 days of history in about 20 seconds. The [Experiment](#experiment)
section has the numbers.

**What gets built first.** A metrics collector, then the forecaster service tested against past data
from a real fleet, then the feature itself. Each type of series first runs in shadow mode, where
predictions are recorded and scored but not acted on, until its track record shows it can be
trusted. [Order of work](#order-of-work) lists the phases and the checks between them.

**Why the name "predictive".** _Proactive_ is already used for the audits and on the site's
[Proactive autonomy](../site/src/content/docs/overview/proactive-autonomy.md) page, where it means
"finds an existing problem without being asked". _Predictive_ names the mechanism, a forecast, and
makes the three modes read as a sequence: reactive, proactive, predictive. A fix made before the
problem happens is called _preemptive remediation_.

**Where the design comes from.** It merges two designs. The kube-agents part is Dmitry Shnayder's:
the shape of a finding, the split between code that computes and an agent that judges, the audit log
and pull-request path, the capability lifecycle and the evaluation loop. The platform-independent
part comes from Gari Singh's [Zero-Shot Forecasting for Predictive
Operations](https://gist.github.com/mastersingh24/ac4cce73bc57ae4a6d8e04a4ad2cb0e7): the forecast,
prediction and action layers, the list of reference types and use cases, the rules for preparing a
series before forecasting, calibration, the cost-based decision rule, the filtering cascade, the
intervention problem, and the preliminary investigations. Where the two designs disagreed, this
document says so and explains how it resolves the disagreement.

## Detailed design

Each subsection below can be read on its own; the [table above](#how-to-read-this-document) says
what each one covers. Two terms from the rest of the repository appear throughout. An **SOP**
(standard operating procedure) is the written procedure a scheduled audit follows. The **ledger** is
the GitHub issue where each audit records its findings and how they changed since the last run.

### Scope

Most of what a predictive mode needs already exists on `main` under other names. This table shows
what an install already has and what this document adds.

| Piece                      | Already on `main`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | What this document adds                                                                                                                                                                                                       |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Scheduled proactive work   | The list of scheduled audits in `agents/platform/cron/jobs.json`. Each audit follows an SOP under `agents/platform/governance/` and reports to the `fleet-audit` ledger ([autonomous watchdogs](../site/src/content/docs/concepts/autonomous-watchdogs.md), [`fleet-audit-issue-ledger.md`](fleet-audit-issue-ledger.md)).                                                                                                                                                                                                            | One more audit on that list, whose findings are based on a forecast instead of a current value.                                                                                                                               |
| Threshold checks on limits | `stockout-prevention` flags regional quota at or above 90%, and autoscaler out-of-resources events in the last 24 hours ([SOP](../../agents/platform/governance/stockout_prevention_sop.md) §3.7, §3.11). `fleet-wide-cost-analysis` samples `kubectl top` three times ([SOP](../../agents/platform/governance/fleet_wide_cost_analysis_sop.md) step 2). `inventory_prioritize` already treats "a quota trend crossing" as a finding with a date ([SOP](../../agents/platform/governance/inventory_prioritize_sop.md)).               | The same limits, checked against where the value is heading instead of where it is now. The existing checks stay as they are; a predictive finding refers to the same limit and adds the expected time of the breach.         |
| Event path                 | The Kubernetes event watcher labels every message it sends to the agent `kind: k8s-event`, and a code comment says other signal sources "would use different constants when they ship" ([`types.go`](../../k8s-operator/cmd/k8s-event-watcher/types.go)). The Pub/Sub adapter turns Cloud Logging alerts into work for the agent ([README](../../agentplugins/pubsub-platform/README.md)).                                                                                                                                            | A new `forecast-breach` message kind for a breach expected within the next 24 hours, so that urgent case goes straight to the incident path instead of waiting for the next scheduled run.                                    |
| Metrics source             | The credential proxy's GCP API relay allows three read-only Monitoring calls: `timeSeries` list, `metricDescriptors` list, and the Managed Prometheus `query`/`query_range`/`series`/`labels` calls ([`api_policy.py`](../../agents/platform/scripts/api_policy.py), [`gcp-api-relay.md`](gcp-api-relay.md)). The install grants `roles/monitoring.viewer`. The relay design names a future metrics collector as its first user and states the rules that collector must follow. Several SOPs forbid reading Prometheus and BigQuery. | Nothing on the credential side. The forecaster's collector is that future user. The SOPs that forbid those sources keep forbidding them; this feature is written against the relay from the start, with its own restrictions. |
| Capability lifecycle       | The [capability delivery vehicle](capability-delivery-vehicle.md), a design for how a capability is shipped, scheduled, triggered from chat or events, customised through reviewed settings, and allowed to learn from results (requirements R1–R7, not yet built).                                                                                                                                                                                                                                                                   | How this feature meets R1–R7: what its settings are, and what it learns from (a forecast that came true or did not).                                                                                                          |
| Related requirements       | [Fleet anomaly detection checks](fleet-anomaly-detection-checks.md) asks for usage trends and says that "the source is part of the requirement"; its expiry section covers certificates, keys and CA rotation. [Drift detection](drift-detection.md) splits its problem into computing the difference (done by code) and judging it (done by the agent).                                                                                                                                                                              | The same split for forecasting: code computes the forecast, and the agent judges it. Expiry dates are left to the anomaly checks; this document covers measured values, not countdowns.                                       |
| A forecasting model        | Nothing. No model, no dependency, no image.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | TimesFM 2.5 as a service inside the cluster with no credentials, an `images.json` entry, and an `enabled:` switch in the Helm chart, in the same way the memory store's pods are added today.                                 |
| Test fixtures and cases    | The seeded test fleet contains only fixed, planted defects ([`bench/tasks/DRAFTS.md`](../../bench/tasks/DRAFTS.md)); nothing on it changes over time.                                                                                                                                                                                                                                                                                                                                                                                 | A fixture that replays recorded data for the first test case, and a proposal for one live fixture that gives the fleet a value that grows over time.                                                                          |

As with the capability delivery vehicle, file names, tool names, and the order of changes beyond
the [Order of work](#order-of-work) are left to the change that implements this.

### The three modes

| Mode           | The problem when the agent acts       | What wakes the agent                                   | Ships today                                                             |
| -------------- | ------------------------------------- | ------------------------------------------------------ | ----------------------------------------------------------------------- |
| **Reactive**   | Exists, and a person noticed it       | A chat request                                         | Yes: the Chat Agent and its specialists                                 |
| **Proactive**  | Exists, and nobody has noticed it yet | A schedule, or an event that already happened          | Yes: the governance audits, the event watcher, the Pub/Sub alert routes |
| **Predictive** | Does not exist yet                    | A forecast that it will happen within a set time frame | No                                                                      |

The difference between proactive and predictive is whether the evidence is about the past or the
future. An `OOMKilled` event is proactive work: the container was already killed for running out of
memory, and the watcher makes sure someone looks at it. A container whose memory use has grown from
40% to 85% of its limit over nine days, on a trend that reaches the limit on Thursday, is predictive
work: nothing has failed yet, and nothing in the cluster will report a problem until it does.

This also says which problems the predictive mode is for. Failures come in two kinds. **Sudden**
failures are a step change: a bad deploy, a lost node, a dependency going down. The event watcher
and the alert routes handle those, and a forecast cannot help, because there is no trend to follow.
**Gradual** failures come from a resource being used up against a defined limit: disk, memory, node
count, quota, number of objects. Each of those has a history that shows where it is heading, and
each ends the same way when nobody looks: a wave of `FailedScheduling` errors, a pod stuck in
`Pending`, a write error on a full disk. The predictive mode handles gradual failures first.

Two things follow from this. First, the predictive mode is not anomaly detection. An anomaly is a
value that is unusual right now, and the [fleet anomaly checks](fleet-anomaly-detection-checks.md)
cover that. A value can be completely normal while it climbs toward a limit. Second, it is not
autoscaling. A predictive finding never scales anything. It proposes a change to a declared limit
through a pull request, or it tells the owner their usage will reach one. Leaving scaling to the
controllers built for it is what makes a predictive finding safe to raise.

### Three layers: forecast, prediction, action

Gari Singh's design describes a _prediction plane_ in three layers. A product can stop after any of
them:

| Layer               | Output                                                                          | In kube-agents                                                                               |
| ------------------- | ------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| **L1 — Forecast**   | A forecast for a series, as a range of likely values for each future time step  | The forecaster service: numbers in, forecast ranges out, no credentials, no judgement        |
| **L2 — Prediction** | A specific claim: "series X reaches limit R in about time T, with confidence C" | The predictive finding, produced by the scheduled run and judged by the agent                |
| **L3 — Action**     | A change made to the system, with safety limits                                 | A pull request against the declared configuration, merged by a person; never a direct change |

Most of the value is in L2. L1 on its own is just a chart, although other features can use it too:
the fleet anomaly checks could use the forecast ranges as a moving definition of "normal" without
running a model of their own. L3 is where the risk is, and this is where the two source designs
differ. Gari's design allows an `enforce` mode per policy, in which the system raises a memory limit
or adds a node itself, with rate limits, caps on concurrent changes and an automatic stop if things
go wrong. The kube-agents workflow model does not allow an agent to change a cluster except through
declared configuration, so this document keeps L3 at `recommend`: every action is a pull request,
and the existing rules that make the agent stop and ask a person still apply. `enforce` is out of
scope entirely, not postponed.

Four principles from Gari's design carry over unchanged, because each is about the finding rather
than about who makes the change:

- A forecast is a suggestion, never an order. The agent can reject it, delay it or lower its
  severity.
- Shadow first. A new type of series runs with its findings recorded but not reported until its
  track record justifies turning it on, and turning it on is a recorded settings change (R4, R7).
- Different thresholds for different costs. A cheap fix that is easy to undo (a larger disk request
  on a storage class that allows expansion) gets a low threshold. An expensive or permanent one (a
  project quota increase, moving data to a new volume) gets a high one. The
  [decision rule](#the-decision-rule) derives both thresholds from costs instead of picking them by
  hand.
- Never hide stale data. A forecast built on data with gaps or old data is marked as such, and it
  cannot lead to a pull request.

### Enabling predictive mode: opt-in, per cluster

The [experiment](#experiment) found that how accurate a forecast is depends more on the cluster than
on the model. The same forecaster that overshot memory by 20% on a bad day on the bursty test
clusters overshot by only 4% on a customer staging cluster with steady load. A low error is not
enough on its own, though. On that steady cluster, assuming the value would stay at its last hourly
level was just as accurate as TimesFM, and the proactive mode already watches the current value. So
neither a single fleet-wide decision nor a check on error alone will do. The mode is enabled per
cluster, and a cluster must show on its own history that predictions would have been right and
would have told people something they did not already know.

Predictive mode is opt-in and off by default. When an operator enables it, each cluster under
management moves between three states:

- **`probing`.** The probe repeats the experiment on the cluster's own recent history. It picks
  points in the past, forecasts forward from each one using only the data available at that point,
  and compares the forecast with what actually happened. This is called a backtest. It checks the
  costly side first: how far forecasts overshoot on a bad day at the time frames the mode uses. Then
  it checks whether TimesFM predicts the highest value in each window better than two simple
  forecasts: the last value held constant, and the same hours of the previous day. If TimesFM does
  no better than holding the last value, prediction adds nothing for that type of series. A cluster
  that passes moves to `predicting`, and one that does not moves to `unpredictable`. Nothing is
  acted on while probing.
- **`predicting`.** The scheduled run forecasts the cluster's future values and acts on them through
  the normal finding path. Every prediction is stored with the time period it covers. When that
  period has passed and the real values are known, the prediction is scored against them. This
  running score is the same record [calibration](#calibration) keeps. If it falls below the
  required level, the cluster stops predicting and moves to `unpredictable` without waiting for an
  operator.
- **`unpredictable`.** No predictions are computed or acted on. The proactive mode still watches
  current values, so a cluster without predictions loses only the early warning, not the alert. The
  probe reruns on a schedule. If the cluster's series have become predictable, for example because a
  workload settled down or a noisy tenant left, the cluster moves back to `predicting`.

The pass marks are deliberately different in each direction. Entering `predicting` requires a better
score than staying there, and a cluster must stay in a state for a minimum time before it can leave
it. Without this, a cluster close to the line would switch back and forth. Both pass marks, the
interval between probes and the forecast time frames are settings on the
[capability delivery vehicle](#on-the-vehicle), with cautious defaults, rather than fixed values in
the code. A forecast that is too high is the costly mistake, since each one means a pull request and
an interrupted person, and enough of them teach people to ignore the agent. So the pass marks are
based on overshoot, and forecasts that are too low count for less.

The decision is made per cluster, but the probe records it for each type of series within the
cluster. A cluster whose memory forecasts well and whose CPU does not gets memory predictions only.
Within a `predicting` cluster, the per-group states described in [Calibration](#calibration) still
apply.

Scoring live predictions runs into [the intervention problem](#the-intervention-problem): if someone
acts on a prediction, the predicted problem may never happen, and the prediction then looks wrong.
The live score therefore uses only predictions that nobody acted on. That is most of them, because
most forecasts do not lead to a finding. A series with a fixed finding is left out of the score
until its next window in which nothing was acted on.

The probe is cheap enough to run regularly. Forecasting time depends on how much history the model
reads, not on how far ahead it forecasts or how many series are in a request. One 11-core CPU
container forecasts 64 series from 7 days of history in about 20 seconds per request. A probe over
four weeks, with two starting points a day and 50 series, is 56 requests, about 20 minutes. On the
staging cluster, the experiment's run with 7 days of history, 129 starting points and 50 series took
50 minutes.

### What a predictive finding is

A predictive finding is a **forecast threshold breach**: a statement that a named series will cross
a named limit within a stated time, together with how confident the forecast is and how much
warning time remains.

#### The two halves of a series

Every series the mode watches has two parts: a **used amount** the agent reads from a metrics
source, and a **declared limit** the agent reads from an object it already audits. The finding is
about the pair, and the agent forecasts only the first part. This matters for two reasons.

First, forecasting a ratio that a controller keeps constant is the most common way to get a
confident wrong answer. A node pool's CPU utilisation stays near its target precisely because the
autoscaler keeps adding nodes. The value that shows the risk is the number of nodes compared with
the autoscaler maximum, not the utilisation. Second, a limit is a fact, not a forecast. Reading it
from the object means anyone can reproduce the threshold, as every audit's rules require. Where the
limit itself changes, for example when a Vertical Pod Autoscaler rewrites resource requests, the
limit becomes a second series, and the breach is where the two forecasts meet instead of where one
forecast crosses a fixed line. That doubles the cost for those series, and it is left to a later
phase.

#### Reference types

The key idea in Gari's design is that a forecast becomes useful as soon as there is something to
compare it with, and that the use cases differ only in what that something is:

| Reference        | Meaning                                                              | Use cases        |
| ---------------- | -------------------------------------------------------------------- | ---------------- |
| Fixed limit      | A declared limit, quota or capacity                                  | UC-1, UC-5, UC-8 |
| Its own forecast | The forecast range of the value itself, used as "normal"             | UC-3, UC-10      |
| Before and after | A forecast from history before a change, compared with what followed | UC-7             |
| Deadline         | A value that must be reached by a certain time                       | UC-6             |
| Budget           | A total of the value over a period                                   | UC-9             |
| Derived trend    | A part of the signal, such as its daily lows                         | UC-4             |

The UC numbers refer to the use cases in [the use-case map](#the-use-case-map) below. The first
version supports only the fixed limit, for the reason Gari's design gives for its own first phase:
the limit already exists in the system, so the first thing shipped can be checked against it. The
other types are later phases.

#### The finding's fields

A finding carries enough information for a reader to check it without running the model again:

- **Series.** Cluster, namespace, object, container where relevant, and the metric name as the
  source names it. Its ID must stay the same across runs for the same object; otherwise the ledger
  would report one slow trend as a new finding every run.
- **Limit.** The value and where it was read: the PersistentVolumeClaim's
  `spec.resources.requests.storage`, the node pool's `autoscaling.maxNodeCount`, the container's
  memory limit, or the ResourceQuota's `hard` value.
- **Last observation.** The most recent value and when it was measured, so a reader can see how far
  from the limit the series is now.
- **Breach.** The model forecasts a range of values, described by quantiles. The q90 forecast is the
  value the real one should stay below 90% of the time; the q50 forecast is the middle; q10 is the
  low end. The finding gives the crossing time for each of them, for example q90 in 8 days, q50 in
  17, q10 never. Together they show how uncertain the breach time is, instead of a single date. The
  earliest plausible crossing is the one from the quantile closest to the limit.
- **Warning time.** The time from now until the earliest plausible crossing. It sets the
  severity and where the finding goes.
- **Trend.** The recent rate of change, and whether the warning time got shorter or longer since the
  last run. The anomaly checks require every finding to say whether things got better or worse since
  last week.
- **Calibration and quality.** The series group's calibration state and any data quality problems
  the collector found (see [Know the ways a series lies](#know-the-ways-a-series-lies)).
- **Provenance.** Model name, version and image digest; how much history was used and which time
  span it covers; a digest of that history, so the forecast can be reproduced exactly weeks later;
  and the settings revision, which the vehicle's R4 requires in every report.
- **Owner.** Taken from the team label, as every fleet finding names one.

#### Severity and path, by lead time

| Warning time              | Severity | Where the finding goes                                                                                                                           |
| ------------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| Within 24 hours (default) | critical | A `forecast-breach` message opens an incident through the existing session path, so a person sees it today instead of at the next scheduled run. |
| Within 14 days (default)  | major    | A ledger finding, with a pull request that fixes it where the limit is defined in the GitOps repository, and marked `kind: manual` otherwise.    |
| Within 30 days (default)  | minor    | A ledger finding, for information only. No pull request until the warning time drops into the next tier.                                         |

All three defaults are settings (R4) that can be changed per group of clusters. The agent may never
narrow them on its own (R5). The time frame for a type of series is set by how long its fix takes,
not by the model. Expanding a volume may need a maintenance window, and a quota increase takes days,
so storage and quota need long time frames, while memory needs short ones.

#### The first series

Five series come first. Each has a declared limit the agent already reads, a metric a standard GKE
cluster already exports, and a fix the existing declarative path already knows how to make.

| Series                            | Used amount                                                                                               | Declared limit                    | Fix                                                                                                                                                                  |
| --------------------------------- | --------------------------------------------------------------------------------------------------------- | --------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| PersistentVolumeClaim fill (UC-5) | `kubernetes.io/pod/volume/used_bytes` per mounted claim                                                   | The claim's requested storage     | `kind: manifest`: a larger request where the storage class allows expansion; `kind: manual` where it does not, since moving the data is the owner's decision         |
| Node pool headroom (UC-8)         | Node count per pool, from the resource labels on the per-node series                                      | The pool's autoscaler maximum     | A pull request against the pool's configuration that raises the maximum, or the alternative machine shapes the stockout SOP already proposes                         |
| Container memory to limit (UC-1)  | `kubernetes.io/container/memory/used_bytes` (working set) per container                                   | The container's memory limit      | `kind: manifest` to raise the limit when the owner confirms the growth is expected; otherwise a finding for the owner, because a memory leak is not a sizing problem |
| Namespace quota (UC-8)            | `kube_resourcequota` usage, through Managed Prometheus, where the kube-state-metrics package is turned on | The quota's `hard` value          | `kind: manifest`: a quota change in the tenant's configuration, or a finding for the owner                                                                           |
| Project quota (UC-8)              | `serviceruntime.googleapis.com/quota/allocation/usage` per region and metric                              | The matching `quota/limit` series | `kind: manual`: a quota increase request; the stockout SOP's existing 90% check stays as it is                                                                       |

The memory series needs more care than the others. A service that allocates memory quickly can go
from 60% to being killed in ninety seconds, which no daily run catches. A JVM or Go service may sit
at 92% of its limit all the time, by design. A forecast can tell "high" apart from "rising toward
the limit", but only with a short time frame and full-resolution data. So the daily run covers slow
memory growth, and fast growth belongs to the 24-hour tier.

#### The use-case map

Gari's design lists ten use cases. Each combines the same three pieces (a signal, its forecast, and
something to compare the forecast with) in a different way. This table shows where each fits in
kube-agents.

| Use case                                  | Signal, compared with                                                                                         | In kube-agents                                                                                                                                                                                                                                                                                                                        |
| ----------------------------------------- | ------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| UC-1 Out of memory                        | Memory working set, compared with `limits.memory`                                                             | First series.                                                                                                                                                                                                                                                                                                                         |
| UC-5 Storage running out                  | Volume used bytes, compared with capacity; time until full as a range ("q10 says 4 days, q90 says 31")        | First series. A sudden change in how fast a disk fills is worth flagging on its own: a broken log-retention setting shows up there long before the disk is full.                                                                                                                                                                      |
| UC-8 Capacity and quota                   | Node count, requested compared with allocatable resources, quota usage compared with quota                    | First series (node pool, namespace quota and project quota). Long-term capacity planning (reservations, committed use) is a later report, not a finding.                                                                                                                                                                              |
| UC-10 Alert context and choosing a window | An alert, annotated with whether the forecast expected it; the quietest time in the next 72 hours             | Later. An incident from the 24-hour tier gets a "was this expected?" field; an expected alert gets a lower severity, but it is never hidden. Choosing a quiet window answers "when should this node pool upgrade run?" for the upgrade-readiness work.                                                                                |
| UC-4 Slow leak                            | The daily lows of memory or open file handles                                                                 | Later. It answers "is this a leak or just load?", which the agent asks about every memory finding. Forecasting the daily lows instead of the raw values separates the two.                                                                                                                                                            |
| UC-9 Error budget                         | How fast the error budget is being spent, projected over the rest of the SLO period, compared with the budget | Later, where Cloud Monitoring SLOs are defined.                                                                                                                                                                                                                                                                                       |
| UC-6 Backlog and deadline                 | Arrival rate and processing rate, compared with a deadline for clearing the queue                             | Later, and only where a queue exports both values; standard GKE exports neither.                                                                                                                                                                                                                                                      |
| UC-7 Before-and-after comparison          | Metrics after a deploy, compared with a forecast made from the history before it                              | Later. It shows the effect of a change without needing a second fleet to compare against, and could feed the rollout checks the upgrade work runs.                                                                                                                                                                                    |
| UC-3 Anomaly ranges                       | A value, compared with its own forecast range (q10 to q90); alert when it falls outside too often             | Not here. The fleet anomaly checks cover anomalies, and they may use L1's forecast ranges. Gari's design puts this last for a similar reason: there is no limit to compare against, so every series would have to be forecast, and this is the use case people are most likely to mute.                                               |
| UC-2 Predictive autoscaling               | Request rate or CPU, compared with provisioned capacity, using known future events as extra inputs            | Not here. This is the second point where the source designs differ: Gari's design treats it as a policy the platform runs, and kube-agents treats scaling as a controller's job (HPA, KEDA, the cluster autoscaler). A finding may propose a scheduled `minReplicas` change as a pull request, but the agent does not scale anything. |

#### Where not to forecast

A general forecasting service tempts people to point it at everything. These kinds of data are left
out on purpose, and the collector refuses them instead of forecasting them badly:

| What not to forecast                                                                 | Why it does not work                                                                          | Use instead                                                                                                       |
| ------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| Deadlines: certificate expiry, key age, CA rotation, the end of a maintenance window | These are countdowns; there is nothing to forecast                                            | Simple date arithmetic in the anomaly checks' expiry section, with the same warning-time field so both read alike |
| Values that jump when configuration changes                                          | The changes come from outside and happen at once; the history says nothing about the next one | Event-driven checks and the drift detector                                                                        |
| Rare events (crash loops, node failures)                                             | Not a time series; they happen too rarely to forecast                                         | The event watcher, and reliability statistics                                                                     |
| Security and intrusion detection                                                     | Attackers change their behaviour, and statistically unusual is not the same as malicious      | Purpose-built detection tools                                                                                     |
| Series with less than about two full cycles of history (for example two days)        | Too little to go on; the model just extends the most recent slope                             | The existing threshold checks, until enough history builds up                                                     |
| Control-plane load (etcd object count, API server latency)                           | A good candidate for later, but its limits are published GKE limits, not declared objects     | A later type of series, once the limits can be read from a table of GKE limits                                    |

### The forecaster

#### Why a foundation model at all

The forecast most operators use today is `predict_linear` in PromQL: a straight line fitted through
the last few hours and extended into the future. For a disk filling at a steady rate it is correct,
cheap and already available. It fails on the series that matter across a fleet, because those
series are not straight lines. Examples are memory use that grows on weekdays and falls at weekends,
a batch namespace whose quota use spikes every night, and a node pool that grows and shrinks with
traffic. A straight line through any of those either raises a false alarm every Friday or misses the
real crossing by a week. A model that follows the shape of the series is what keeps people trusting
the forecasts after the first few.

Classical methods such as Holt-Winters, ARIMA and Prophet do follow the shape, but each series needs
its own fitting, its own declaration of daily or weekly cycles, and a long history before it
produces anything. Running thousands of them is the hard part. Time-series foundation models remove
that work: they are trained once on a large collection of series and then forecast a new series
**zero-shot**, from its own history alone.

#### Why TimesFM 2.5

[TimesFM](https://github.com/google-research/timesfm) is Google Research's time-series foundation
model. This document proposes version 2.5, and each of the following reasons would be enough on its
own:

- **Licence.** The code, and the model weights up to and including 2.5, are Apache 2.0. TimesFM 3.0
  adds forecasting several related series together, but its pretrained weights are under a
  non-commercial licence that forbids production use, as its README says. A component shipped in an
  install cannot carry that restriction. So 3.0 is not an option unless its weights are relicensed,
  and the design must not depend on features only 3.0 has.
- **Size.** About 200 million parameters and roughly 800 MB of weights. It runs on a CPU; community
  measurements put its memory use at around 1.5 GB, which is a modest Deployment and needs no GPU.
  The install's memory store already runs two pods of similar size.
- **Input.** One series of between 32 and 16,384 points, with no settings for how often the data is
  sampled and no feature engineering. That is exactly what a Monitoring `timeSeries` call returns
  after the points are aligned to a fixed interval, and producing it is the collector's whole job.
- **Output.** Ten quantiles for each future time step, from q10 to q90, for up to 1,000 steps ahead.
  The range of breach times in a finding comes straight from these. A single-value forecast could
  not produce it.
- **Ecosystem.** Model checkpoints on Hugging Face (`google/timesfm-2.5-200m-pytorch`, and a
  transformers version), PyTorch, Flax and MLX support, an extension for extra inputs, a way to
  fine-tune it, and an official agent `SKILL.md` in the repository, the same kind of file this
  project uses for its own skills. Google also offers the same model as BigQuery's `AI.FORECAST`
  and on Vertex AI Model Garden, which keeps a managed option open (see below) without changing the
  model.

The forecaster has one interface (history and settings in, forecast ranges out) and several
backends: TimesFM; two simple forecasts, one repeating earlier days (called seasonal-naive) and one
extending a straight line (linear); and a constant forecast for series that do not change and for
when the model fails. The simple forecasts are full backends, not just test tools. If the model
cannot beat them on a type of series, that type should use the simple forecast, and the only way to
find out is to run both side by side. Requests are grouped by their settings, so the settings come
from a small fixed set instead of being chosen freely per series. Too many different settings would
split the requests into small, inefficient batches.

#### Covariates and long cycles

Some series follow patterns their recent history cannot show: a nightly batch job, a planned
migration, the end of a quarter, the Christmas peak. Gari's design treats known future events as
important inputs (its UC-2 depends on them). For models that cannot take them directly, it proposes
a fallback: forecast without the event, then apply a simple adjustment learned from past occurrences
of the same event. TimesFM 2.5's repository includes a way to add such inputs
(`forecast_with_covariates`, the XReg extension), which fits a linear model on the extra inputs
together with the forecast. TimesFM 3.0's built-in support is blocked by its licence. The first five
series use no extra inputs.

To show the model a yearly pattern, feeding it three separate windows (the last day, the last month,
and the same month last year) does not work. The model reads one continuous history, and three
windows joined together would show it two sudden jumps that never happened. There are two sound ways
to show it a year. One is a single long history at low resolution (a year of hourly points is 8,760
points, within the 16,384 limit, and Cloud Monitoring keeps system metrics for 24 months at
ten-minute resolution), used alongside a short high-resolution history. The other is a holiday
calendar passed as an extra input. Which works better is a question for measurement; the
[experiment](#experiment) tested the first.

#### What it is not

TimesFM does not know what a PersistentVolumeClaim is. It receives numbers and returns numbers. It
does not detect anomalies, although its forecast ranges can be used for that. It does not explain a
trend, and it does not decide whether growth is expected. All of those are the agent's job, and
[How a prediction reaches people](#how-a-prediction-reaches-people) keeps them there. A published
evaluation of foundation models on real operational data also found cases where zero-shot forecasts
did poorly even with well-chosen history lengths and time frames. That is why the
[order of work](#order-of-work) tests the model on the fleet's own data before any finding reaches
a ledger.

#### Alternatives weighed

| Alternative                                                | What it offers                                                                                                                                       | Why it is not the main choice                                                                                                                                                                                                                                                                                                              |
| ---------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `predict_linear` in Managed Prometheus, and seasonal-naive | Already available or two lines of code, free, and correct for series that only grow or that repeat exactly.                                          | A straight line ignores daily and weekly cycles; repeating earlier days ignores growth. Both are **the simple forecasts the model is tested against**, and if one of them wins on a type of series, that type uses it (see [Order of work](#order-of-work)).                                                                               |
| Cloud Monitoring forecast conditions                       | An alerting policy with `forecastOptions` predicts a threshold crossing between 1 hour and 2.5 days ahead, trained per series, with no model to run. | It cannot look further than 2.5 days, which covers the 24-hour tier and nothing else. Each series needs a policy set up in advance, and the output is an alert, not a finding with an owner and a fix. It is a good **input** for the 24-hour tier, through the Pub/Sub adapter's existing alert route, and a fix the agent could propose. |
| BigQuery `AI.FORECAST`                                     | The same TimesFM model, managed by Google, forecasting millions of series in one SQL statement, with `AI.DETECT_ANOMALIES` alongside.                | The metrics must first be exported to BigQuery, which is a second data path with its own cost and retention, and several SOPs forbid BigQuery as a source. It is the right choice for a very large fleet once the mode has proven itself, but not for the first version.                                                                   |
| Vertex AI Model Garden endpoint                            | Google-managed serving of the same weights.                                                                                                          | A cost per call and data leaving the cluster, for a model small enough to run next to the agent. Kept as the option for an install that does not allow new workloads in the cluster; choosing it is a recorded decision to send data outside.                                                                                              |
| TimesFM 3.0                                                | Forecasting several related series together, and extra inputs about the past and future.                                                             | Non-commercial weights. Reconsider if they are relicensed.                                                                                                                                                                                                                                                                                 |
| Tabular foundation models (TabPFN and similar)             | Classification from features, which suits "will this pod fail" rather than "when will this series cross its limit".                                  | A different question, and the current weights have restrictive licence terms. A separate document, if a failure-prediction mode is ever wanted.                                                                                                                                                                                            |

#### Where it runs

The forecaster is a **separate Deployment with no credentials**, set up the same way as the memory
store's pods: its own image pinned in [`images.json`](../../images.json), an `enabled:` switch in
the chart next to the other optional components in
[`values.yaml`](../../charts/kube-agents/values.yaml), a manifest in the operator's integrations
folder, the model weights built into the image so nothing is downloaded at startup, and a network
policy that accepts connections only from the agent's sandbox. It receives numbers and returns
forecasts. It cannot read Monitoring, cannot reach a cluster, and holds no data or credentials worth
stealing. If it is down, the scheduled run falls back to the simple forecasts and marks its findings
accordingly, and the existing threshold checks keep running. The predictive mode never makes the
reactive and proactive modes worse.

Loading the model inside the agent's sandbox image was considered and rejected. It would add 800 MB
of weights against the image size budget, and it would put a model process in the same pod as the
agent's shell, without making anything more secure. Keeping them apart means the reading of metrics,
which needs credentials, stays where the relay's rules already control it, and the model runs where
there is nothing to control. Gari's design also describes running the model on a GPU node pool with
the control logic on CPU. A large fleet can choose that, since the interface is a batched call
either way.

### Where the series come from

The relay design expected this user. Its list of allowed calls includes, read-only, the three
Monitoring calls a collector needs: `timeSeries` list for GKE system metrics, `metricDescriptors`
list so the collector can look up a metric it has not seen before, and the Managed Prometheus query
calls for data that only kube-state-metrics or the kubelet export. Its "what follows" section sets
the rules for this collector. It must get its session from `ApiSession()` and use the real endpoint
URLs. When the relay refuses a call with a 403, whose body names the `gcp.api.*` rule that refused
it, the collector records a limitation for that cluster instead of treating the value as zero. The
forecaster's collector follows those rules and needs no new allowed calls.

What each source provides:

- **GKE system metrics** (`kubernetes.io/`) are on by default on every GKE cluster and sampled every
  60 seconds. Cloud Monitoring keeps them at full resolution for six weeks, and after that as
  ten-minute averages for up to 24 months. Six weeks of one-minute points is far more than the model
  can read. The default is one week at five-minute intervals, about 2,000 points, and one month at
  ten-minute intervals for series with a weekly cycle.
- **Managed Prometheus** carries the kube-state-metrics and kubelet packages where a cluster has
  them turned on. They are not on everywhere, so the collector first checks that a needed series
  exists. If it does not, the collector records that as a limitation on the cluster, never as a
  clean result. The cost SOP follows the same rule when `kubectl top` is unavailable.
- **The declared limits** come from the objects the audits already read with `kubectl get` and
  `gcloud container node-pools describe`, under the same read-only command rules.

The relay works at the level of a GCP project, which is the Platform Agent's scope. Every series is
tagged with its cluster from its resource labels, so one collection run covers every cluster the
agent manages, and the ledger groups findings by cluster group and region, as the anomaly checks
require. Series labels can contain sensitive names, so whatever the feature stores gets the same
retention and access rules as the Monitoring data it came from.

### How a prediction reaches people

A prediction turns into work through paths that already exist: a ledger finding and a pull request
when the problem is days away, and an incident message when it is close. Drift detection split its
work into two jobs: computing the difference, which is mechanical, and judging it, which is what an
agent is good at. Forecasting splits the same way, for the same reason.

**Job A: compute the forecast.** This is done by code, not by the language model. A scheduled run
lists the enabled series for each cluster, filters them, reads the remaining ones through the relay,
prepares each history, sends them to the forecaster in batches, and applies the decision rule
against the declared limit. The result is a list of candidate findings with every field filled in
from data. The language model should never be doing this work inside its own reasoning. Today the
cost SOP asks the agent to sample `kubectl top` three times, and the reasons the relay design gives
for replacing that apply even more strongly to a forecast.

**Job B: judge the forecast.** This is done by the agent, in the audit's session. For each candidate
it answers the questions code cannot. Is the growth expected (a StatefulSet that is meant to keep
data) or a defect (a log folder nobody cleans up)? Should the limit be raised, or should the
consumer be fixed? Is there configuration in the GitOps repository to change, and what should it
say? Is this a new finding, or the same trend as last week with less time left? The answers decide
the kind of fix and the text of the recommendation. The agent also applies a rule the
obtainability audit already has: if a repository says a claim's growth is expected, the finding is
reclassified instead of raised. The forecast shows _that_ a value is moving, not _why_; the why is
Job B.

**The results use the existing paths.** Findings go to the audit's ledger issue through the same
helper every audit uses, with the changes since the previous run computed, and a `kind: manifest`
fix becomes a small pull request that a person merges. Nothing new is built for reporting, as the
vehicle's R2 requires. The workflow model's rules for when the agent must stop and ask a person are
unchanged: a predictive finding that would raise a project quota, resize a node pool used across the
fleet, or touch a data volume becomes a recommendation, never a pull request the agent opens on its
own.

**Urgent predictions use the event path.** When a scheduled run finds a breach expected within the
next 24 hours, it sends a message with `kind: forecast-breach` into the session path the event
watcher already uses. The watcher's own code comment reserves that mechanism for signal sources
other than Kubernetes events, and this is one. The incident thread then contains the finding, and a
person who replies in it is answered by a session that has seen the forecast. The 24-hour tier can
also run on its own between the daily runs, for example a short-range check every few hours with the
full run once a day, because a 30-day forecast does not need to be refreshed every hour.

#### Triage

A medium-sized cluster produces hundreds of thousands of series, and a fleet multiplies that. Most
of them are nowhere near a limit. A container at 12% of its memory limit with no upward trend does
not need a forecast to show that it will not run out. Deciding what to forecast is a bigger
engineering problem than the forecasting itself. Gari's design solves it with a series of filters,
each more expensive than the one before:

| Tier | Filter                                                                                                               | What gets through            |
| ---- | -------------------------------------------------------------------------------------------------------------------- | ---------------------------- |
| T0   | Is the series of an enabled type, and is its data recent?                                                            | Everything registered        |
| T1   | Distance to the limit: `(limit − now) / σ_recent`, how many typical recent fluctuations away the limit is            | Removes the large majority   |
| T2   | Movement: a trend line that ignores outliers, extended to the limit                                                  | Removes most of the rest     |
| T3   | Cheap forecast: repeat earlier days, with a range based on past errors; if it disagrees with T2, the series moves on | A few percent                |
| T4   | TimesFM                                                                                                              | The series that got this far |

Three rules keep the filtering safe as well as cheap. A series that got through stays in for a
while, so it does not keep dropping in and out at the T1 boundary. A small random sample of the
filtered-out series is forecast anyway; that is the only way to measure how many real problems the
filters miss. And each run has a fixed forecasting budget that the filters fill in priority order,
so under load a run forecasts fewer low-priority series instead of taking longer. Series with fixed
limits are cheap precisely because T1 has a limit to measure the distance to. A series compared only
with its own forecast range has no such limit, which is the cost reason UC-3 is left out of this
design.

#### The decision rule

Given the forecast and the limit, the scheduled run finds the first time step at which each quantile
crosses the limit, and it requires the crossing to last for several steps in a row, because a
single step just over the line at the end of the forecast is usually noise.

Which quantile counts is not fixed in the code. It is worth acting when the expected cost of acting
is lower than the expected cost of not acting. For a crossing, that means acting when
`P(breach) > C_fp / (C_fp + C_fn)`, where `C_fp` is the cost of a false alarm and `C_fn` the cost of
a missed breach. So each type of fix states those two costs in its settings, even as rough relative
numbers, and the probability threshold follows from them. A larger disk request costs almost nothing
if it turns out to be unnecessary, while a full database disk is an outage, so that fix is worth
making at a low probability. A project quota increase or moving data to a new volume needs near
certainty. An operator can still fix the quantile by hand if they prefer.

A finding moves through three states: `clear`, `pending` and `raised`. It moves to `pending` when a
crossing is forecast, and to `raised` only when the crossing appears in several runs in a row. To
return to `clear`, the forecast must move further from the limit than it was when the finding was
raised. Without this, a series close to the line would open and close the same finding every week.
Waiting longer gives less warning time but fewer false alarms, and it is the main setting to tune.

Related findings are merged. A node pool nearing its maximum and every workload on it growing are
one finding, with the rest attached as evidence. Otherwise the first real trend would produce a page
of findings that nobody reads.

#### On the vehicle

The feature works like an audit and fits the capability delivery vehicle's requirements without
special cases:

| Requirement      | For this feature                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| ---------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| R1 Pre-defined   | The five series, the three time frames, a 90% limit fraction and the costs for each type of fix ship as defaults. The defaults are chosen to be quiet rather than thorough: a new install should see a handful of findings, not a page.                                                                                                                                                                                                                                |
| R2 Scheduled     | One scheduled job for the daily run; the ledger is its record; a run that finds nothing says nothing.                                                                                                                                                                                                                                                                                                                                                                  |
| R3 Triggerable   | "When will `orders-db-0`'s volume fill?" is a run limited to that volume, answered in the chat thread. "Run the capacity check now" starts the scheduled run early. The 24-hour tier is the event path above, and a Cloud Monitoring forecast alert that arrives through the Pub/Sub adapter is another event that starts it.                                                                                                                                          |
| R4 Customisable  | Settings: which series are enabled, the limit fraction, the three time frames, the costs, how many runs a crossing must appear in, exclusions per cluster group, the history window, and whether each type of series is in shadow or reporting. The procedure, the safety rules and the model are fixed in the image.                                                                                                                                                  |
| R5 Self-learning | This feature has a kind of evidence no other audit has: every finding is a prediction that later turns out true or false. A predicted breach that did not happen, or a breach that happened when the forecast said it would not, tells the agent how accurate it is, and the agent can propose settings changes from it, scored as [the intervention problem](#the-intervention-problem) requires. The agent can only propose narrowing a setting, never do it itself. |
| R6 Durability    | As the vehicle specifies; nothing here needs more.                                                                                                                                                                                                                                                                                                                                                                                                                     |
| R7 Write path    | As the vehicle specifies. The model's outputs are never written into settings; only a person's confirmation changes a threshold or takes a type of series out of shadow.                                                                                                                                                                                                                                                                                               |

The agent-to-agent bus could hold the latest forecast for each series, as a state topic like the one
the [payload spec](spec-a2a-payloads.md) already sketches for upgrade-readiness results. A later
question in chat could then start from the last run instead of collecting data from scratch. That
is an improvement, not a requirement.

### Honesty about the future

A finding about the present can be checked by looking at the object again. A finding about the
future cannot, and a mode built on forecasts is only as useful as its record of being right.

#### Backtest against baselines before the first finding

Before any finding is raised, test the forecasts on the fleet's own data. Pick points in the past,
forecast forward from each point using only the data available then, and compare the forecast with
what actually happened. The test must use strictly only past data; a history that includes even one
future point makes the result meaningless. Report the forecast accuracy and decision accuracy
[metrics](#metrics) for each type of series against three simple forecasts: the last value held
constant, the same hours of the previous day or week (seasonal-naive), and a straight-line trend
(linear). Score both the error at each point and the highest value in each forecast window, and
count forecasts that were too low separately from those that were too high. Holding the last value
is the most important comparison, because it is what the proactive mode already sees. The
[experiment](#experiment) found that TimesFM's 8-hour peak forecasts were almost identical to it.
This test is what the [order of work](#order-of-work) depends on.

#### Calibration

The model's forecast ranges are accurate on average across the data it was trained on, but that says
nothing about one particular disk in one install. If the q90 forecast is really only exceeded 40% of
the time for some series (so it behaves like a q60), every threshold based on it is wrong without
anyone noticing, and the cost rule above works with probabilities that do not mean what they say.
So the feature keeps a running record, per group of series (and per series once there is enough
history), of how often the real value fell below each forecast quantile, at several distances into
the future, because accuracy gets worse further out and one overall number would hide that. From
that record it widens or narrows the forecast range by whatever factor makes the quantiles accurate
again. This correction (called conformal calibration) is cheap, makes no assumptions about the data,
and does not change the model. A group that needs a large correction is telling the operator that
the model does not understand it.

Each group is in one of three states. `unmeasured`: too little history; findings are for information
only and marked low confidence. `calibrated`: the quantiles are accurate enough; findings may come
with a pull request. `miscalibrated`: the correction cannot fix the error; no pull requests, and a
candidate for switching to a simple forecast. A deploy or a limit change resets the group to
`unmeasured`, instead of carrying an old correction forward that may now be wrong.

#### The intervention problem

If the mode works, its predictions stop coming true. It forecasts that a disk will fill, someone
merges the expansion, and the disk never fills. Scored simply, that looks like a false alarm. Scored
that way for long enough, a working feature looks broken, someone makes it less sensitive, and then
it really is broken. There is no complete solution. Gari's design offers three partial ones, and
this feature uses all three:

- **Record what would have happened.** Every finding stores the state at the time it was raised, and
  every later run records whether the series crossed the limit, when, and whether a fix changed the
  limit in the meantime. The question scored is whether the finding was justified, not whether the
  problem happened. The ledger already tracks changes between runs; this adds an outcome to each
  closed finding, and each ledger update reports the record for that type of series, for example
  "12 of 14 disk forecasts in the last quarter crossed within the predicted window; 3 were fixed
  first".
- **Shadow mode.** Nobody acts on a type of series in shadow, so its predictions can be scored
  cleanly. Every type starts there.
- **Replaying history.** Recorded series with known crossings, replayed offline, cannot be affected
  by anyone acting on them. This is the [replay fixture](#evaluation) below. It measures well how
  many real breaches are found, but poorly how many warnings are false, because it contains only the
  incidents that happened, not the ones that were prevented.

The rule that follows, stated so that nobody tunes the feature against it: how often warnings are
right is measured only on replayed history and in shadow mode, never on findings someone acted on.

#### Quiet defaults, conservative costs

The default costs lean toward staying quiet. The first weeks on a fleet should be spent making the
feature less sensitive where needed, not more; the vehicle's R1 explains why.

#### Know the ways a series lies

The model accepts any input and returns a confident forecast even when the input is garbage. The
collector handles each of the following problems before the model sees a series, and the finding
carries a quality flag when one applied (`gappy`, `short_context`, `post_restart`, `low_variance`,
`stale`, `clipped`):

- **Resolution.** The history should cover at least two full cycles of the main pattern, which is
  more points at full resolution than the model can read. So resolution depends on how far ahead the
  forecast goes: short forecasts use full resolution, and longer ones use points every five minutes,
  ten minutes or hour. When points are combined, the method depends on what matters for the series:
  the maximum for a series that can run out, because a five-minute average hides the spike that
  kills the pod; the minimum for daily lows; and the average for load.
- **Three kinds of gap.** A missed sample is short and filled in from its neighbours. A missing
  series (the pod did not exist yet) shortens the history and is never filled with zeros. A zero is
  a real measurement. Filling a restart gap with zeros shows the model a sudden drop, and it will
  forecast another one. A series with too little usable history is skipped and recorded as a
  limitation, not forecast.
- **Restarts and other changes.** A restart resets memory; a deploy changes the trend; a disk
  expansion resets how full it is; a limit increase moves the threshold. The collector cuts the
  history at the most recent change in the declared limit, the object's generation or the
  container's restart count, and forecasts only from the part that reflects the current situation.
  The memory-leak series is the exception: it needs to see across restarts, because a restart that
  resets a leak is the evidence that there is one.
- **Controllers that hide the trend.** Covered above: forecast the amount a controller uses (the
  number of nodes), not the ratio it keeps steady (utilisation).
- **Values at a hard cap, and flat values.** A series already at its cap is flat and forecasts flat;
  the existing checks catch the cap, and the forecaster is for the approach to it. A history that
  barely changes gets a constant forecast directly, without calling the model.
- **Bounds.** A ratio cannot be above one and memory cannot be negative, so forecasts are clipped to
  the possible range.
- **Series that are too short or too new.** Fewer than 32 points is below what the model accepts,
  and more importantly, a day-old disk has no trend worth reading. The minimum history is a setting,
  with a minimum value fixed in the image.
- **Counters.** A counter that only goes up is converted to a rate, with resets handled, before
  forecasting; forecasting the raw counter produces a straight line that says nothing. The limit is
  compared with the level, not the rate.

### Evaluation

#### Metrics

Accuracy on average is not the goal; accuracy near the limit is. A model with good average accuracy
that forecasts too low at the high end is worse than useless for a memory series. There are two sets
of metrics, reported per type of series and per distance into the future:

- **Forecast accuracy.** Quantile loss (also called pinball loss), the main quality measure, since
  the feature uses quantiles; how often the real value actually fell below each quantile; and MASE,
  the forecast's error divided by the error of the seasonal-naive forecast. A MASE above one means
  the model is doing worse than a two-line simple forecast on that type of series.
- **Decision accuracy.** Precision (the share of warnings that were right) and recall (the share of
  real breaches that got a warning); the spread of warning times for correct warnings, with a
  minimum useful warning time per type of series, below which a correct warning counts as a miss;
  and quantile accuracy measured only in windows where the series was near its limit. That last
  number predicts real performance best, and it is usually worse than the overall figure.

#### The eval case

The [eval-driven development rule](../../.agents/rules/eval_driven_development.md) requires every
change to agent behaviour to start with a test case that fails, and the seeded test fleet is where
the planted defects for those cases live. Forecasting raises a problem no existing case has: a
planted defect is a state, but a trend needs a history, and the fleet is read-only for tests and
does not change by design. Two fixtures solve this, in this order.

**First, a replay fixture.** The collector reads its series through a session. A test fixture can
return a recorded series in place of the session's answer, in the same way the obtainability tests
do for the Compute Advice API. A test case then plays back a recorded disk that fills up on a known
date. Its checks confirm four things: the agent's report names that disk; the predicted breach time
is close enough to the real one; the fix is a `kind: manifest` change to the disk's configuration in
the case's GitOps fixture; and nothing in the cluster was changed. This works today with no waiting,
and it is the failing case the first implementation must make pass three times. The recorded series
also start the replay collection: real recordings with known crossings, long periods of normal
operation (against which false alarms are measured), and made-up edge cases for restarts, gaps,
sudden changes and flat series.

**Second, a live fixture.** One workload on a seeded cluster that writes to a disk at a fixed rate,
sized so the disk never actually fills between test runs and reset on a schedule, gives the fleet a
real trend after about a week of history. It is a fixture that needs time to build up, in the sense
the fleet catalogue already uses: a case that reads it cannot pass on a freshly created fleet, and
says so. It belongs in the same discussion as the other fixtures the fleet is being asked to add,
and it costs one small disk per evaluation project.

The case goes into the existing `capacity` domain, because a new domain needs a place in the
presubmit checks before it can exist, and a first case cannot earn one. It is registered in the
nightly run first, as the eval rule requires, and it is never added to the list of cases that block
merges in the same change that makes it pass.

### Order of work

Each phase is a separate change with its own live test. Gari's design names five preliminary
investigations; each is done in the phase that depends on it.

1. **The collector, as a library and a sandbox command.** Written to the relay's rules, it produces
   aligned series with their limits attached for the five types of series, and a record of
   limitations per cluster. There is no model yet: the collector's output feeds the simple forecasts
   as well as the forecaster. This phase also builds, once, the metrics source the anomaly checks
   say they need. It includes two investigations. The first is whether series from past breaches can
   be reconstructed at the resolution needed. If not, recording must start now, which makes it the
   most urgent investigation even though it looks the least interesting. The second is how much gap
   handling, counter conversion and the choice of how to combine points affect forecast accuracy,
   which decides how much work the preparation rules above deserve.
2. **The forecaster service and the backtest.** The TimesFM 2.5 image, the chart switch, the simple
   forecast backends, and a backtest on a real fleet's series. It includes the inference-cost
   investigation (speed at realistic batch sizes on the install's own CPU type, which sets the
   triage budget) and the quality investigation (on which kinds of series the model beats the simple
   forecasts: sawtooth memory, daily load cycles, bursty queues, steadily growing disks). **Decision
   point:** a type of series uses TimesFM only where the backtest shows it beats the simple
   forecasts on how often breach warnings are right, at the same share of breaches found. Where it
   does not, that type uses a simple forecast, and the finding looks the same; the model is recorded
   as a provenance field, not built into the design. If no type passes, the mode still ships on the
   simple forecasts, and this document records why.
3. **The feature.** The SOP, the scheduled job, the ledger, the triage and decision rule, the opt-in
   switch and the per-cluster probe with its re-probe schedule, calibration, the kinds of fix per
   series, every type of series in shadow, the replay test case run failing and then passing, and
   the settings on the vehicle once its settings store exists (defaults fixed in the image until
   then).
4. **The 24-hour tier.** The `forecast-breach` message kind, the short-range run, and the session
   path's handling of a finding that is a forecast rather than an event.
5. **Chat.** "When will X run out" as a run limited to X, and the state topic so the answer starts
   from the last run.
6. **Further reference types.** The memory-leak series (derived trend), annotating expected spikes
   and choosing quiet windows (its own forecast range), error budget (budget), and before-and-after
   rollout checks, each with its own test case. The investigation of extra inputs, comparing XReg
   with the fallback adjustment on a real scheduled event, must be done before any series that needs
   one.

Phases 1 and 2 add no agent behaviour and need no test case; phases 3 to 6 each start from one.
Each phase is judged by a backtest or test record, not by a demonstration.

### Success measures and risks

Gari's design sets targets for its first version. Adjusted for a feature whose action is a pull
request:

| Measure           | Definition                                                                                                            | Target                                                                                  |
| ----------------- | --------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| Warning time      | Median time from a correct finding to the predicted breach                                                            | At least ten times as long as the fix takes; for a pull request, days rather than hours |
| Precision         | Share of raised findings that would have crossed the limit if nobody had acted, measured on replay and in shadow mode | 0.8 for a type of series allowed to open pull requests                                  |
| Recall            | Share of real breaches of a covered type found with enough warning time                                               | 0.5 in the first version                                                                |
| Quantile accuracy | Gap between how often the value fell below q10, q50 and q90 and how often it should have, per group, over seven days  | 0.05 after calibration                                                                  |
| Cost              | The forecaster's compute as a share of the install's own                                                              | 1%                                                                                      |
| Reversal rate     | Share of merged predictive fixes reverted within a week                                                               | 0.02                                                                                    |

| Risk                                                                 | Impact                                          | Mitigation                                                                                                       |
| -------------------------------------------------------------------- | ----------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| The model does no better than simple forecasts on the fleet's series | The case for using a model fails                | The phase 2 decision point; the simple forecasts keep the feature working either way                             |
| The filters remove a series that was about to breach                 | A missed breach in a mode meant to predict them | The random sample of filtered-out series measures how often this happens; the ledger reports it                  |
| Fixes make correct predictions look wrong                            | The feature is tuned until it is useless        | Precision is measured only on replay and in shadow mode, and the ledger's own record says so                     |
| Findings become noise                                                | People mute the findings and miss the good ones | Shadow first, quiet defaults, a crossing must repeat before it is raised, related findings merged, UC-3 left out |
| Forecasting costs more than the budget across a whole fleet          | The feature cannot run widely                   | The inference-cost investigation; a fixed budget that the filters fill                                           |
| A new model version is worse                                         | Accuracy drops without anyone noticing          | A pinned image digest, the model recorded in each finding, and calibration to detect the drop                    |
| An extra input is wrong about the future                             | A confident wrong forecast                      | Store the extra inputs with the forecast, so a review can tell an input error from a model error                 |

### Out of scope

- **Anomaly detection on current values.** The fleet anomaly checks cover it; they may use the
  forecaster's ranges.
- **Predictive autoscaling and any direct changes to clusters.** Scaling is the job of controllers
  (HPA, KEDA, the cluster autoscaler). The agent proposes limit changes through pull requests; it
  does not scale or resize anything. Gari's `enforce` mode has no equivalent here.
- **Root-cause analysis.** The forecast says that a value is moving. The agent's judgement about why
  is a recommendation, not a diagnosis.
- **Predicting failures from features.** "Will this pod fail" needs a different kind of model and a
  different licence discussion.
- **Deadlines.** Expiry and rotation dates are countdowns. They share the warning-time field with
  this feature and nothing else.
- **Forecasting related series together.** Forecasting each series separately and combining the
  results underestimates how likely they are to spike at the same time. The features that fix this
  need TimesFM 3.0.
- **Fine-tuning.** The premise is that the model works without training. If a fleet's series defeat
  it, that is a reason to reconsider the model, not to run a training pipeline inside an install.

### Open questions

- **Precision on a real fleet.** The day-ahead backtest has run ([Experiment](#experiment)), but no
  series crossed a limit during it, so the share of breach warnings that are right, the number the
  proposal depends on, is still unmeasured.
- **Cost of a full run.** A fleet of a hundred clusters with a few thousand mounted disks is a few
  thousand series of two thousand points each. By the published speed figures that takes minutes on
  a CPU, not hours, but the figure that matters is the one measured on the install's own nodes.
- **Calibration groups.** Short-lived series never build up enough history to calibrate each one
  separately. Grouping them by type and workload is the proposed answer; the right grouping has to
  be found by measurement.
- **Two histories or one long one.** Whether two forecasts per series (a week at high resolution, a
  year at low resolution) beat one long history depends on the cost of forecasting and on the
  backtest.
- **Metric packages that are turned off.** The kube-state-metrics and kubelet packages are chosen
  per cluster. Whether the feature should recommend turning them on as its own finding, or stay
  silent on a cluster without them, is a settings decision the first real fleet will settle.
- **Autopilot.** System metrics are available, but node pools are not the operator's to size. The
  node pool series does not apply, and the finding needs a way to say so for each kind of cluster.
- **Where the record of hits and misses lives.** The ledger issue's hidden marker carries findings
  from one run to the next. Whether it can also carry outcomes, or whether the record belongs in the
  settings store or a state topic, is for the change that builds phase 3. The replay collection
  needs longer retention than either, and its own storage.
- **Who owns a predictive fix.** A platform-owned feature that proposes changes to limits owned by
  application teams needs an agreed owner before its first pull request surprises someone. The team
  label names the reviewer, not who makes the decision.
- **The relay's POST calls.** MQL `timeSeries:query` is refused today, by design. The read-only GET
  calls are enough for the five series. A later series that needs MQL reopens the relay design's
  second question, not this one.

## Experiment

The experiment's code, method and full results are kept outside this repository, on the
`experiment/timesfm-backtest` branch of the author's fork: the
[README](https://github.com/dshnayder/kube-agents/blob/experiment/timesfm-backtest/bench/experiments/timesfm-backtest/README.md)
describes the method and the commands to rerun it, and
[RESULTS.md](https://github.com/dshnayder/kube-agents/blob/experiment/timesfm-backtest/bench/experiments/timesfm-backtest/RESULTS.md)
has the full write-up and the go/no-go conclusion.

The experiment is a backtest. It picks many starting points in the past. At each one, TimesFM 2.5
reads the days before that point and forecasts the next 24 hours in five-minute steps, and the
forecast is compared with what actually happened. The same is done with simple forecasts for
comparison. The data comes from two places: the 11 clusters this project's CI uses for evaluation
(called the test clusters below), and one customer staging cluster.

### What it tested

The original proposal was a single comparison: the model reads the eight days to one day before day
T, and predicts day T. The experiment keeps that as its `tfm-7d` variant and changes three things
around it. It uses every midnight with enough history as a starting point, because a single day
could be a lucky or unlucky draw. It runs the same starting points through three simple forecasts:
`snaive-1d` (repeat yesterday), `snaive-7d` (repeat the same day last week) and `linear-7d` (a
straight line through the last week), because the decision rule above makes the simple forecasts
the bar TimesFM has to clear. And it adds a 28-day variant, because a model can only recognise a
weekly cycle after it has seen at least two weeks.

The second proposal was to give the model three windows: the last day, the last month, and the same
month a year earlier, so that a December spike is visible in December. The reason is sound, but the
model cannot take input that way. TimesFM 2.5 forecasts one series at a time from one continuous
history, so three windows cannot be given side by side. The same effect can come from one long
history (a year of hourly points is 8,760 points, within the model's limit of 16,384), from
combining forecasts made from different history lengths, or from a holiday calendar passed as an
extra input through XReg. The experiment tests combining forecasts as `tfm-ens`, the average of the
1-, 7- and 28-day forecasts. It cannot test a yearly window: no cluster available is a year old, and
Cloud Monitoring keeps five-minute points for six weeks and ten-minute averages for 24 months. So
the collector described above has to store its own history before year-over-year input is possible
at all.

### What it found

The first run covered the 11 test clusters over 41 days, from 2026-08-13 to 2026-09-23: 93 series
that existed long enough to forecast. The project's production install was collected too but left
out, because it does little apart from occasional pull-request tests, and its almost idle series
made every forecast look better than it is. The experiment's README has the tables and caveats.

**TimesFM follows the daily shape better than the simple forecasts.** TimesFM's typical MASE was
0.61–0.63, against 0.92 for repeating earlier days and 0.97 for a straight line. (MASE is the error
relative to repeating earlier days; lower is better.) It beat repeating earlier days on 78–82% of
the forecasts, and its quantile loss was about 40% lower. It was strongest in the first hour, with a
MASE of 0.08 against 0.71. The forecast ranges from 7 and 28 days of history were close to the 80%
coverage they should have. Memory improved the most. CPU requests, which change in steps when
something is deployed, improved the least.

**More history does not help much.** Seven days of history came within 0.01 MASE of 28 days, and
averaging forecasts from different history lengths added nothing over the longest one alone. So
patterns longer than the history, such as yearly ones, have to come from the other two options in
[Covariates and long cycles](#covariates-and-long-cycles): a long hourly history from stored data,
or an extra input. That makes storing history in the collector a requirement for year-over-year
forecasting.

**The forecast ranges do not cover the daily peak.** The day's highest q90 value was above the real
daily maximum only 26–29% of the time, against 81% for repeating earlier days. It gave a warning on
only 4–21% of the days that set a new high. A breach check that compares the q90 forecast with a
threshold therefore underestimates the risk by design.

[Calibration](#calibration) must therefore also correct the forecast peak, not only widen the range
at each point. The experiment tried one such correction, which raises each series' forecast by an
amount learned from its own earlier misses. It brought peak coverage up to 86–95%. After that
correction, TimesFM's peak warnings were only a little better than the simple forecasts': 18% of
warnings were right, with 92% of peaks caught, for the 7-day variant, against 13% right with 100%
caught for repeating earlier days. This is based on only 12 events.

**The breach test is still open.** No series reached 90% of its limit during the experiment, so the
[order of work](#order-of-work)'s phase 2 decision point, how often breach warnings are right, has
no events to measure. It needs a longer period, a fleet whose resources are really under pressure,
or a replay of past incidents.

**A day ahead, the forecast is within 5% above or 10% below the real value only 57% of the time.**
Each hourly forecast value was compared with the value measured in that hour. 57% were within the
range, 19% were more than 5% too high, and 23% were more than 10% too low. No type of series came
close to 90%: node count reached 71%, memory 68%, and CPU 36–52%. TimesFM beat repeating yesterday
on every type of series (45% within the range, 32% too high), and it was much more accurate in the
first few hours: 80% for memory and node count one to six hours ahead. Forecasting 8 hours ahead
instead of 24 roughly halved the typical hourly error: memory stayed within 2% too high and 3% too
low, against 9% either way a day ahead. On a bad day (the worst 1 in 10), the 8-hour forecast was
still up to 20% too high. Using a lower quantile (q30) instead of the middle forecast halved the
share of costly too-high forecasts and lost at most 5 points of accuracy. Warnings from a forecast
crossing a threshold were rarely wrong, but they caught only 3 of 148 crossings that were new that
day; the proactive agent already sees the rest.

On bursty clusters like these, day-ahead forecasts of CPU and memory are not accurate enough to act
on. One busy customer staging cluster looked much better (on a bad day, an 8-hour memory forecast
was 4% too high, against 20% on the test clusters), which suggested the answer depends on the
cluster. That is why prediction is decided per cluster by a probe
([Enabling predictive mode](#enabling-predictive-mode-opt-in-per-cluster)). Series that grow
steadily, such as disks, remain untested: no disk on these clusters existed long enough to forecast.
The next finding shows that the staging cluster's better numbers do not mean TimesFM understood its
load.

**Eight hours ahead, TimesFM predicted little more than the current value.** The first analysis
compared TimesFM only with forecasts built from earlier days. A later analysis added the simplest
possible forecast: assume the value stays where it was in the last hour. It also changed what was
scored. A warning depends on the highest value in the coming hours, so each method was scored by
the highest value it predicted for the next 8 hours against the highest value that actually
occurred. On the staging cluster (129 forecasts, each from 7 days of history), the results were as
follows. Each cell shows the typical error, then in brackets the largest shortfall and the largest
overshoot seen in 1 forecast in 10. A negative number means the forecast was too low.

| Series           | TimesFM          | Last hour's value held | Same hours yesterday |
| ---------------- | ---------------- | ---------------------- | -------------------- |
| Cluster CPU used | −9% (−23%, +2%)  | −9% (−23%, +2%)        | 0% (−21%, +25%)      |
| Container CPU    | −7% (−32%, +1%)  | −6% (−27%, +1%)        | 0% (−18%, +20%)      |
| Memory           | −1% (−8%, 0%)    | −1% (−7%, 0%)          | 0% (−6%, +6%)        |
| Node count       | −10% (−17%, +1%) | −9% (−17%, +2%)        | +2% (−17%, +17%)     |

On the staging cluster, TimesFM and the last-hour forecast differ by at most one point. On the test
clusters they differ by at most four (cluster CPU: 31% too low against 27%). Their average error is
also nearly the same: 6.8% against 7.5% for staging cluster CPU over 8 hours, and 9.4% against 9.5%
over 24 hours.

So the staging cluster did not look better because TimesFM understood its load. It looked better
because its load changes little during a day: memory typically varies by 5% and requested CPU by 7%,
against 22% and 54% on the test clusters. When values barely move, any forecast looks accurate.

The windows that matter for a warning are the ones where the load goes up. On the staging cluster,
about 40% of 8-hour CPU windows reached a peak more than 15% above the starting value. In those
windows, TimesFM predicted a peak 18–20% lower than the real one, because it forecast that the load
would stay roughly where it was. For example, if CPU use was 100 cores and rose to 130, TimesFM
predicted about 105. With a limit of 120, it would not have warned.

Repeating yesterday's values is not a good substitute. Its typical error is close to zero and it
underestimates rises less, but in 1 forecast in 10 it predicts a peak 17–25% too high on the staging
cluster, and up to 155% too high on the test clusters. Each of those would be a false alarm.

Holding the last hour's value is not really a forecast: it is the current value, which the
proactive mode already watches. Where TimesFM does no better than that, running it costs a service
to operate and tells people nothing new. The
[probe](#enabling-predictive-mode-opt-in-per-cluster) therefore compares TimesFM with both simple
forecasts, and TimesFM is used for a type of series only where it predicts peaks better than both.
These results use TimesFM's middle (median) forecast. Its high-end forecast (q90) does not fix the
problem: as reported above, the day's highest q90 value reached the real daily maximum only 26–29%
of the time. The experiment's `results/baselines.md` has every series type for both clusters.

**Forecasting is fast enough.** On one 14-core CPU replica, a batch of 64 series forecast 288 steps
(24 hours) ahead took 3.6, 7.6 and 29 seconds with 1, 7 and 28 days of history. On an 11-core
container the same batches took 7.6, 20.7 and 70 seconds. How far ahead the forecast goes, and the
number of series in a batch up to 64, do not change the time; only the length of the history does.
A thousand series forecast from 7 days of history is 16 requests, two to six minutes on one replica,
so forecasting never falls behind the period it covers.

## Related

- [Zero-Shot Forecasting for Predictive Operations](https://gist.github.com/mastersingh24/ac4cce73bc57ae4a6d8e04a4ad2cb0e7)
  — Gari Singh's platform-independent prediction-plane design, merged into this one.
- [`capability-delivery-vehicle.md`](capability-delivery-vehicle.md) — how this feature would be
  shipped, scheduled and customised.
- [`fleet-anomaly-detection-checks.md`](fleet-anomaly-detection-checks.md) — the neighbouring
  requirements; the metrics source its usage section asks for is the one built in phase 1.
- [`drift-detection.md`](drift-detection.md) — the split between code that computes and an agent
  that judges, which this document copies.
- [`gcp-api-relay.md`](gcp-api-relay.md) — how metrics are read, and the rules the collector
  follows.
- [`fleet-audit-issue-ledger.md`](fleet-audit-issue-ledger.md) — where findings are recorded.
- [`../architecture/04-workflow-model.md`](../architecture/04-workflow-model.md) — why a new trigger
  changes when the agent wakes up and nothing else.
- [TimesFM repository](https://github.com/google-research/timesfm) — the licence terms for each
  version, the 2.5 API, the extra-inputs extension, and the official agent skill.
- [Cloud Monitoring forecast conditions](https://docs.cloud.google.com/monitoring/alerts/metric-forecast)
  — the managed alternative for the 24-hour tier.
- [BigQuery `AI.FORECAST`](https://docs.cloud.google.com/bigquery/docs/reference/standard-sql/bigqueryml-syntax-ai-forecast)
  — the same model as a managed function, for a very large fleet.

## Prior art

This is a survey of what had shipped as of September 2026, so a reviewer can see where this proposal
stands. It found three things, and each changes something in this document.

**No Kubernetes or AI SRE agent forecasts.** K8sGPT, HolmesGPT, Komodor's Klaudia, kagent, Cleric,
Resolve AI, Traversal, Metoro and Datadog's Bits Investigation all follow the same loop: detect,
investigate, fix. HolmesGPT and Azure SRE Agent add scheduled health checks, which is what this
repository calls the proactive mode. Azure's published capacity-planning example is a custom skill
that flags quota above a fixed percentage, which is a check on the current value. Google's Gemini
Cloud Assist "Proactive Mode" runs investigations in the background when an alert or a cost anomaly
triggers them, and its documentation never mentions forecasting. Two small vendors, Hawkeye and
Phoebe AI, claim to predict problems one to three days ahead, but neither explains how. Nobody
offers the mode this document proposes.

**The observability platforms have forecasting, but no agent.** This is where the real prior art
is, and one of them has the whole loop:

- [Dynatrace Davis AI](https://docs.dynatrace.com/docs/observe/infrastructure-observability/kubernetes-app/use-cases/predictive-operations)
  documents a "predictive Kubernetes operations" workflow that forecasts disk usage, finds the
  owner, and opens a pull request that changes the disk size in the service's configuration
  repository. Dynatrace runs it weekly on
  [about eight thousand of its own disks](https://www.dynatrace.com/news/blog/automate-predictive-capacity-management-with-davis-ai-for-workflows/).
  That is forecast, owner and declarative fix, the same approach proposed here, but inside one
  vendor's platform and data store.
- [Grafana Cloud](https://grafana.com/docs/grafana-cloud/machine-learning/dynamic-alerting/forecasting/)
  offers metric forecasts with daily and weekly cycles, one forecast per item in a label set, and a
  documented example that alerts days before a disk fills.
- New Relic offers
  [predictive alerts](https://docs.newrelic.com/docs/alerts/create-alert/set-thresholds/predictive-alerts/).
  Datadog's Watchdog uses Toto, Datadog's own time-series foundation model. Cloud Monitoring has the
  forecast alert condition discussed [above](#alternatives-weighed), which cannot look further than
  2.5 days.

**Time-series foundation models have been tested on Kubernetes metrics.**
[Parseable's benchmark](https://www.parseable.com/blog/zero-shot-forecasting) (April 2026) compared
Chronos, TimesFM, IBM's Tiny Time-Mixers and Toto with classical methods on real pod metrics. Toto
did best on high-frequency series forecast together, Chronos was the most versatile, and no model
could forecast a kind of event it had never seen before. The
[k0rdent FinOps Agent](https://cloudnativenow.com/contributed-content/building-finops-with-k0rdent-open-source/)
already uses Toto, without training, on Prometheus and OpenCost data to forecast cost and usage with
p10, p50 and p90 ranges. It is the closest operations agent built on such a model, and it forecasts
spending rather than failures.

What this changes here. The positioning holds: no product combines a forecast, a finding with an
owner, and a declarative fix in an agent that runs inside the install, and Dynatrace's workflow
shows that the approach works in production. Two adjustments follow. First, the backtest in
[Order of work](#order-of-work) phase 2 should run Toto and Chronos next to TimesFM, since both have
open weights and Toto was trained on observability data. The forecaster's backend interface makes
the best model for each type of series a configuration choice, not a redesign, and each model's
licence is checked in that phase before it is adopted. Second, the benchmark's finding about events
the model has never seen is the same rule about changes stated in
[Know the ways a series lies](#know-the-ways-a-series-lies), now with a measurement behind it.
