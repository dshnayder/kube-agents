# TimesFM backtest

The experiment behind the "Backtest experiment" section of
[`docs/designs/predictive-operations.md`](../../../docs/designs/predictive-operations.md). It
asks how accurately TimesFM 2.5, zero-shot, forecasts tomorrow's GKE telemetry from the days
before it, and whether that accuracy is enough to act on. The design document reads the
results; this directory holds the method, the code, and under `results/` the summary tables.

## The question, and what changed from the proposal

The proposal was: feed the model the values of T-8d to T-1d, predict day T, compare with what
day T actually did. That is a sound backtest, run here with three changes:

- **Many origins, not one day.** One day T is one draw; a quiet Tuesday says nothing about a
  busy Monday. Every UTC midnight with enough history behind it is an origin, 33 in all.
- **Baselines.** An error figure on its own cannot be read. "Repeat yesterday"
  (`snaive-1d`), "repeat last week's same day" (`snaive-7d`) and a straight line through the
  last week (`linear-7d`) run on the same origins and series. The design's rule is that the
  model has to beat seasonal-naive or it does not ship.
- **Longer context.** Seven days holds one weekly cycle, and a model sees a cycle only once it
  has seen it at least twice, so a 28-day arm runs beside the 7-day one.

The second proposal, feeding three windows (last day, last month, the same month last year),
has the right motivation: load has daily, weekly and yearly cycles, and a December spike is
visible only in last December. TimesFM takes one univariate series per forecast, so three
windows cannot go in side by side. The ways to get the same effect are one long context (a
year at hourly resolution is 8,760 points, inside the 16,384 the model accepts), a combination
of forecasts made from different windows, or a holiday calendar as a covariate through XReg.
This experiment tests the combination as `tfm-ens`, the average of the 1-, 7- and 28-day
forecasts. The yearly window cannot be tested: no cluster in reach is a year old, and Cloud
Monitoring keeps five-minute points for six weeks (ten-minute downsamples for 24 months).

## Corpus

[`collect.py`](collect.py) reads Cloud Monitoring for 41 days ending 2026-09-23 00:00 UTC at
five-minute resolution, 11,808 points a series, from twelve clusters: the production install
`kage-management` and the `platform-agent-host` cluster in each of the eleven evaluation pool
projects, which every presubmit and nightly deploys into. Prow's own build clusters were not
readable with the credentials available.

| Class              | Series                                                                                | Reference series  |
| ------------------ | ------------------------------------------------------------------------------------- | ----------------- |
| `cluster_cpu`      | cores used, summed over the cluster                                                   | allocatable cores |
| `cpu_requested`    | cores requested, summed                                                               | allocatable cores |
| `node_count`       | nodes                                                                                 | none              |
| `container_memory` | non-evictable memory, per namespace, top-level controller and container               | memory limit      |
| `container_cpu`    | cores used, same grouping                                                             | CPU limit         |
| `volume_used`      | bytes used, per namespace, controller and volume, 1 GiB to 50 GiB volumes, no secrets | volume capacity   |

Grouping by top-level controller rather than pod keeps a container's series continuous across
restarts, which is what a capacity forecast wants. Ten of the twelve clusters are younger than
the window (their first point falls between day 4 and day 18), so a series is kept when it spans
at least 21 days from its first point, 90% of the points in that span are present, and it is not
constant; GKE's own namespaces are dropped. An arm forecasts a series at an origin only when the
arm's whole context window is real data, so a series from a three-week-old cluster enters the
1- and 7-day arms but never the 28-day one, and the analysis compares arms only on the
(series, origin) pairs they all scored. Eval runs create a namespace each, so most listed series live less than a day and fail the coverage test. The corpus is not
committed: it names workloads and namespaces, and it is regenerated from the command below.

## Arms

| Arm         | Input                                          | Quantiles                                     |
| ----------- | ---------------------------------------------- | --------------------------------------------- |
| `snaive-1d` | yesterday, repeated                            | empirical, from last week's day-on-day errors |
| `snaive-7d` | the same day last week                         | empirical, from its own errors                |
| `linear-7d` | least-squares line through the last 7 days     | empirical, from the fit's residuals           |
| `tfm-1d`    | TimesFM 2.5, last 1 day (288 points)           | the model's q10–q90                           |
| `tfm-7d`    | TimesFM 2.5, last 7 days: the proposal         | the model's q10–q90                           |
| `tfm-28d`   | TimesFM 2.5, last 28 days                      | the model's q10–q90                           |
| `tfm-ens`   | mean of the three TimesFM forecasts, per point | mean of their quantiles                       |

Each forecasts 288 steps, the whole of day T. TimesFM runs with the flags the design names
(normalised inputs, continuous quantile head, flip invariance, positive-only inference,
quantile-crossing fix). Gaps in the context are linearly interpolated; a series is not scored
for a day on which it is missing more than 10% of its truth.

## What is measured

Per series, origin and arm, in [`harness/backtest.py`](harness/backtest.py):

- **MASE**: mean absolute error of the median, divided by the mean day-on-day change over the
  week before the origin. Below 1 beats `snaive-1d`'s in-sample error; every arm shares the
  scale. `mase_1h` is the same over the first hour.
- **WQL**: pinball loss over q10–q90, normalised by the truth's magnitude. It scores the whole
  distribution.
- **Coverage**: the share of day T's points inside q10–q90. 0.8 is right; below it the band is
  overconfident.
- **Peak coverage**: whether day T's maximum stays below the day's highest q90. This is the
  number an alert or a pre-scale acts on.
- **New-high days**: days whose peak clears the prior week's maximum by 10% of the week's
  range, and whether the arm's highest q90 flagged them in advance; precision and recall.
  Limit days (a peak at 90% of a container's limit or a volume's capacity) are scored the same
  way.
- **Conformal peak**, in [`harness/analyze.py`](harness/analyze.py): the arm's highest q90
  raised by the 90% split-conformal quantile of that series' own earlier peak misses, using only
  origins before the one scored. This is the design's calibration step applied to the daily
  maximum.
- **Latency**: forecaster seconds per batch of up to 64 series, by context length, on CPU.

## Re-running it

```bash
# corpus (Application Default Credentials that can read Monitoring in every target project)
SSL_CERT_FILE=/etc/ssl/cert.pem python3 collect.py --end 2026-09-23 --days 41 \
  --out data/series.jsonl.gz
./build_push.sh                  # both images, where docker runs
./cluster.sh up                  # cluster, bucket, corpus upload, forecaster (4 × n2-standard-16)
./cluster.sh run r1              # the backtest Job, then wait
./cluster.sh fetch r1            # metrics.csv.gz and latency.csv.gz into results/
python3 harness/analyze.py results/metrics.csv.gz results/latency.csv.gz > results/summary.md
./cluster.sh down
# RESULTS.md's tables: the forecaster image run locally on :8080 (a few hours on CPU); --dump
# keeps every forecast path, peaks.py forecasts the daily-peak series
python3 harness/backtest.py --data data/series.jsonl.gz --forecaster http://localhost:8080 \
  --out results/r2 --dump dump/
python3 harness/peaks.py --data data/series.jsonl.gz --forecaster http://localhost:8080 \
  --out results/r2/peaks.csv.gz
python3 harness/decide.py dump/ results/r2/peaks.csv.gz > results/decision.md
python3 harness/pointwise.py dump/ > results/pointwise.md
```

The forecaster ([`forecaster/`](forecaster/)) is TimesFM 2.5 200M (Apache-2.0 weights) baked
into the image, behind `POST /forecast`, with one compiled copy per context bucket (512, 2048,
8192 points) because the model pads every call to its compiled length. The harness image
([`harness/`](harness/)) is the backtest; the analysis runs wherever the results land.

## Results

[RESULTS.md](RESULTS.md) is the write-up for deciding whether to build the predictive agent:
point-by-point accuracy against a −10%/+5% band, threshold warnings, and the recommendation. This section records the
standard forecast-accuracy scores behind it.

Run `r1`, 2026-09-23. The full tables are in [`results/summary.md`](results/summary.md). The
per-row metrics are not committed, for the same reason as the corpus.

153 series were kept: 60 from `kage-management` and 3 to 9 from each evaluation host, whose
namespaces rarely outlive a run. The long set covers origins 28 to 40, 13 days, and the 113
series with 28 days of real history, 1,030 (series, origin) pairs per arm. The short set covers
all 33 origins with the 1- and 7-day arms, 3,357 pairs per arm.

**TimesFM beats every baseline on the whole day.** On the long set:

| Arm         | Median MASE | First-hour MASE |   WQL | 80% coverage | Beats `snaive-1d` |
| ----------- | ----------: | --------------: | ----: | -----------: | ----------------: |
| `snaive-1d` |        0.90 |            0.65 | 0.046 |         0.78 |                 — |
| `snaive-7d` |        1.14 |            0.91 | 0.064 |         0.73 |               30% |
| `linear-7d` |        0.84 |            0.62 | 0.043 |         0.59 |               56% |
| `tfm-1d`    |        0.58 |            0.19 | 0.031 |         0.74 |               84% |
| `tfm-7d`    |        0.55 |            0.18 | 0.028 |         0.81 |               84% |
| `tfm-28d`   |        0.54 |            0.20 | 0.027 |         0.83 |               85% |
| `tfm-ens`   |        0.55 |            0.18 | 0.028 |         0.81 |               86% |

Compared with repeating yesterday, the model's median error is about 40% lower, its quantile
loss is about 40% lower, and its first-hour error is less than a third. The 7- and 28-day bands
are calibrated (0.81 and 0.83 against a nominal 0.80). The 1-day band is slightly overconfident.
The short set gives the same ordering: 0.59 and 0.57 for the 1- and 7-day arms, against 0.91
for `snaive-1d`. By class, memory gains most (0.44 against 0.84). CPU requested gains least,
because requests change in steps at deploys: `snaive-7d` scores 0.78 there and `tfm-7d` 0.75.
Node counts were flat on most days, so every arm's median there is near zero and the class says
little.

**Context length hardly matters for the day's shape.** The 7-day window from the proposal is
within 0.01 MASE of the 28-day one. One day of context is measurably worse. The ensemble of the
three windows is no better than the 28-day arm alone.

**The raw forecast misses daily peaks.** The day's highest q90 was at or above the day's real
maximum only 19–26% of the time for the TimesFM arms. For `snaive-1d` it was 80%. The model's
quantiles are per point. A smooth q90 path sits under a spike that could land at any of the
day's 288 steps, and the maximum of per-point q90s is not a q90 of the maximum. As a result, the
raw q90 flags almost none of the 77 days that set a new high (recall 3–18%).

**A conformal correction fixes coverage, but TimesFM gains little on peaks.** The correction
raises each series' peak bound by the 90% quantile of its own earlier misses. On origins 33 to
40, where every arm has at least five earlier origins, it gives:

| Arm         | Peak coverage | Headroom (scale units) | Alerts | Precision | Recall |
| ----------- | ------------: | ---------------------: | -----: | --------: | -----: |
| `snaive-1d` |          0.96 |                    5.3 |    421 |      0.11 |   0.98 |
| `linear-7d` |          0.96 |                    4.4 |    360 |      0.13 |   0.98 |
| `tfm-7d`    |          0.95 |                    4.7 |    382 |      0.12 |   0.96 |
| `tfm-28d`   |          0.90 |                    2.5 |    205 |      0.19 |   0.81 |
| `tfm-ens`   |          0.90 |                    2.7 |    223 |      0.17 |   0.81 |

After correction, every arm reaches roughly the target coverage. The 28-day forecast gives the
tightest bound, half the headroom and half the alerts, at the cost of recall. None of them is
precise: at best about one alert in five was a real new high, and only 47 events fall in this
window.

**No series reached 90% of its limit.** The breach question the design gates on cannot be
scored on this corpus.

**Latency.** A batch of up to 64 series with a 288-step horizon took 3.6 s median at 1-day
context, 7.6 s at 7 days and 29 s at 28 days. That is one TimesFM replica with 14 CPU cores,
padded to buckets of 512, 2,048 and 8,192 points. A thousand series at 28 days is 16 batches,
about eight minutes on one replica.

### Caveats

- One install dominates. `kage-management` supplies 60 of the 153 series. The evaluation hosts
  are young and small, and their workloads are the harness itself.
- Six weeks of data. The long set has 13 origins, and the conformal table only 8. Weekly
  effects are seen at most five times.
- No year-old series was available, so the three-window proposal's yearly window is untested.
- The model runs zero-shot, with no covariates, on CPU. XReg and fine-tuning were not tried.

### What it answers

- Feeding the model 7 days to forecast day T works. It beats every baseline, and 28 days adds
  almost nothing to the day's shape. Longer context is worth its fourfold cost only for peak
  bounds.
- Averaging forecasts from several windows is no better than the longest window alone. The
  December case needs a year of archived series, fed either as one long hourly context or
  through a holiday covariate. Neither exists yet.
- An alert on "tomorrow's peak exceeds X" cannot use the model's raw q90. It needs a peak-level
  calibration, and after calibration its advantage over the baselines is modest.
  [RESULTS.md](RESULTS.md) scores such alerts directly against per-series thresholds.
