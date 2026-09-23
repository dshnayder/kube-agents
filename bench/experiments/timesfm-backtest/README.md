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
(series, origin) pairs they all scored. Eval runs create a namespace
each, so most listed series live less than a day and fail the coverage test. The corpus is not
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
```

The forecaster ([`forecaster/`](forecaster/)) is TimesFM 2.5 200M (Apache-2.0 weights) baked
into the image, behind `POST /forecast`, with one compiled copy per context bucket (512, 2048,
8192 points) because the model pads every call to its compiled length. The harness image
([`harness/`](harness/)) is the backtest; the analysis runs wherever the results land.

## Results

Pending: the Job `backtest-r1` is running.
