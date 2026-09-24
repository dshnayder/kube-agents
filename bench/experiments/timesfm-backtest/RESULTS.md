# TimesFM day-ahead forecasting: results

## The answer

**Hypothesis:** TimesFM can predict a time series' values 24 hours ahead to within −10% to +5% of
the actual value, 90% of the time.

The band is a rough figure. Its shape carries the point: it is narrower on the high side
because the two ways of being wrong cost different amounts.

- **Too high is costly.** A forecast above what really happens makes the agent act on a problem
  that never arrives: a pull request someone has to review, a person interrupted. Enough of
  them and the agent's warnings become noise. The experience gets worse, and people learn to
  ignore every alarm the agent raises, including the real ones.
- **Too low is tolerable.** A forecast below what really happens misses the problem in advance,
  and the proactive agent, which watches current values, catches it when it arrives. The
  predictive agent loses its head start, not the fix.

**Result:** the test ran on the 11 clusters that host the evaluation harness, which every
presubmit and nightly deploys into; they scale between 1 and 13 nodes many times a day. At
midnight UTC each day, TimesFM forecast the next 24 hours of CPU usage, requested CPU, memory
usage and node count. We compared each forecast hourly value with the value actually measured
in that hour. **57%** of forecasts landed within −10%/+5%. **19%** were more than 5% too high,
the costly side. **23%** were more than 10% too low, the tolerable side. No series type comes
near 90%.

| Series                | Within −10%/+5% | More than 5% too high | More than 10% too low | Repeating yesterday: within / too high |
| --------------------- | --------------: | --------------------: | --------------------: | -------------------------------------: |
| container memory      |             68% |                   22% |                   10% |                              53% / 30% |
| node count            |             71% |                    8% |                   21% |                              65% / 18% |
| container CPU         |             52% |                   21% |                   28% |                              37% / 37% |
| cluster CPU requested |             52% |                   17% |                   31% |                              47% / 28% |
| cluster CPU used      |             36% |                   24% |                   41% |                              25% / 41% |
| **all 93 series**     |         **57%** |               **19%** |               **23%** |                          **45% / 32%** |

What this means for the predictive agent:

- **As stated, the hypothesis is not confirmed.** A full day ahead, 4 of the 93 series have 90%
  of their values in the band.
- **The model is clearly better than the simple rule.** On every series type, TimesFM puts more
  values in the band than repeating yesterday, 57% against 45%, and it over-forecasts less
  often, 19% against 32%. Prediction does add accuracy, and most of it on the costly side.
- **Accuracy depends on how far ahead you look.** For the next 1–6 hours, 80% of memory and of
  node-count values are in the band. At 12–24 hours ahead the figures are 60% and 65%.
- **Forecasting 8 hours ahead instead of 24 roughly halves the typical miss.** On a typical
  day, a memory forecast made 8 hours ahead is at most 2% too high and 3% too low in any hour of
  the 8; a 24-hour forecast misses by up to 9% either way. On a bad day (1 in 10), memory is 20%
  too high at worst against 28%, and CPU and node count overshoot by 26–54%
  ([8 hours ahead or 24?](#8-hours-ahead-or-24)).
- **CPU is not predictable at this precision.** To take in 90% of hourly container-CPU values,
  the band would have to widen to −32%/+23%. For cluster CPU used, it would take −69%/+29%.

**Recommendation:** do not build the predictive agent on the promise of 24-hour-ahead forecasts
within −10%/+5% for CPU, memory or node count on clusters like these. The data supports
forecasting memory and node count a few hours ahead, where TimesFM beats repeating yesterday
and over-forecasts less. That use has not been tested against what the proactive agent already
sees from current values. One busy customer staging cluster forecast far better than the
evaluation hosts: a bad-day memory overshoot of 4% against 20%, 8 hours ahead
([staging cluster](#a-busy-staging-cluster-forecasts-far-better)). Steady production load is
where the question should be asked next. Forecasting itself takes seconds per request, so it
never lags the horizon ([forecast time](#forecasting-takes-seconds-not-hours)).

## What was tested

**Data.** We used 41 days of five-minute Cloud Monitoring series, ending 2026-09-23, from the
agent host in each of the 11 evaluation projects. After dropping series too short or too sparse
to forecast, 93 remained: 30 container memory, 30 container CPU, and 11 each of cluster CPU
used, cluster CPU requested and node count. No disk volume on these clusters lives long enough
to forecast. The production install `kage-management` was collected too and is left out: it does
little beyond occasional tests of pull-request changes, and its near-idle series flattered every
score. [README.md](README.md#corpus) has the selection rules.

**Forecasts.** On each of 33 days, TimesFM 2.5 read the previous 7 days, the length in the
original proposal, and forecast the next 24 hours as 288 five-minute values. We used its median
forecast. Nothing from the forecast day was visible to it. It ran zero-shot, with no training
on this data.

**Comparison.** For every forecast value Pi, we took the value Vi actually measured at the same
time. The forecast is in band when Pi lies between Vi − 10% and Vi + 5%. The headline scores
hourly values, the mean of each hour, 24 per day and about 36,000 in total. At the raw
five-minute step the figures are 56% in band, 23% too high and 21% too low
([`results/pointwise.md`](results/pointwise.md)). Values below 5% of the series' maximum over
the previous week are left out, because a percentage of a near-idle value measures noise, not
the forecast; on these clusters that removed almost nothing.

**No-model rules**, scored the same way on the same series and days:

- Repeat yesterday: yesterday's values, shifted by their median day-to-day change.
- Repeat the same day last week.
- A straight line through the last 7 days.

Repeating yesterday was the strongest, so it is the one in the table above.

## Findings

### No series type reaches 90% in band a day ahead

The table above is the result. The misses fall on both sides, and which side depends on the
series type:

- Memory is more often too high than too low (22% against 10%).
- Node count and CPU are more often too low. The model does not foresee a scale-up or a burst
  that nothing in the past week announced.

Per series, 90% in band is reached by 3 of 11 node counts, 1 of 30 container-CPU series, and
none of the memory, requested-CPU or cluster-CPU series.

### TimesFM beats every rule that needs no model

On the same series and days, TimesFM puts 57% of hourly values in the band. Repeating yesterday
puts 45%, repeating last week 33%, and a straight-line trend 41%. It also produces fewer
over-forecasts than repeating yesterday: 19% against 32% overall, and 21% against 37% on
container CPU. This matches the standard accuracy scores in
[`results/summary.md`](results/summary.md), where TimesFM's typical error is about a third lower
than repeating yesterday's.

### Accuracy falls with lead time

Share of hourly values in band, by how far ahead of the forecast they are:

| Series                | 0–1 h | 1–6 h | 6–12 h | 12–24 h |
| --------------------- | ----: | ----: | -----: | ------: |
| container memory      |   95% |   80% |    69% |     60% |
| node count            |   91% |   80% |    74% |     65% |
| container CPU         |   74% |   59% |    53% |     46% |
| cluster CPU requested |   84% |   64% |    59% |     42% |
| cluster CPU used      |   63% |   43% |    40% |     28% |

The first hour is forecast well for memory, node count and requested CPU. After that, accuracy
falls steadily across the day.

### 8 hours ahead or 24?

**The test.** Take 10am as an example:

- **8 hours ahead.** At 2am, TimesFM gets the previous 7 days of data and forecasts every hour
  from 2am to 10am. At 10am we compare each hour's forecast with what was actually measured. We
  note the largest overshoot (forecast above actual) and the largest undershoot (forecast below
  actual), each as a percentage of the actual value.
- **24 hours ahead.** At 10am yesterday, TimesFM gets the previous 7 days and forecasts every
  hour until 10am today. We compare all 24 hours the same way.

We repeated this from four start times a day (00:00, 06:00, 12:00 and 18:00 UTC) on each of the
33 days, for all 93 series: about 7,900 forecasts per horizon. The tables give the largest
overshoot and undershoot on a typical day (the median forecast) and on a bad day (the worst 1
in 10). "0%" means under half a percent.

**On a typical day:**

| Series                | 8 h ahead: too high by at most | 8 h ahead: too low by at most | 24 h ahead: too high by at most | 24 h ahead: too low by at most |
| --------------------- | -----------------------------: | ----------------------------: | ------------------------------: | -----------------------------: |
| container memory      |                             2% |                            3% |                              9% |                             9% |
| node count            |                             0% |                            1% |                              0% |                            19% |
| container CPU         |                             4% |                           13% |                             12% |                            22% |
| cluster CPU requested |                             2% |                           19% |                              6% |                            33% |
| cluster CPU used      |                             8% |                           43% |                             20% |                            67% |

**On a bad day (worst 1 in 10):**

| Series                | 8 h ahead: too high by at most | 8 h ahead: too low by at most | 24 h ahead: too high by at most | 24 h ahead: too low by at most |
| --------------------- | -----------------------------: | ----------------------------: | ------------------------------: | -----------------------------: |
| container memory      |                            20% |                           15% |                             28% |                            23% |
| node count            |                            33% |                           36% |                             47% |                            45% |
| container CPU         |                            26% |                           35% |                             46% |                            45% |
| cluster CPU requested |                            40% |                           45% |                             46% |                            48% |
| cluster CPU used      |                            54% |                           72% |                             85% |                            77% |

How to read it:

- **Memory.** On a typical day, an 8-hour forecast stays within 3% of the actual value in every
  hour; a 24-hour one drifts to 9%. On a bad day it is up to 20% too high.
- **Node count** changes in whole nodes. On most days the 8-hour forecast is exact. These
  clusters scale between 1 and 13 nodes as evaluation runs arrive, and on a day that happens the
  forecast is off by several nodes, a third or more of the cluster.
- **CPU** misses mostly by forecasting too low: the model does not foresee bursts. Its
  overshoots are small on a typical day (at most 4% for container CPU, 8 hours ahead) and large
  on a bad one (26%).

Part of the difference is that a 24-hour window has three times as many hours in which to miss.
To remove that, we also compared the same 8 clock hours forecast 8 hours ahead and 16–24 hours
ahead. On a typical day the fresher forecast is a little better: memory 2%/3% against 4%/5%;
container CPU 4%/13% against 6%/13%. On a bad day the gap is similar: memory 20%/15% against
22%/19%; container CPU 26%/35% against 35%/38%. Re-forecasting more often helps, but less than
the tables above suggest.

With 24 hours of history instead of 7 days, the typical-day figures change by at most 4
points. On bad days, 7 days helps mainly cluster CPU used, where the 8-hour overshoot falls
from 94% to 54%. [`results/window.md`](results/window.md) has both histories.

### A lower forecast trades costly misses for tolerable ones

The median forecast is used throughout. Forecasting from a lower quantile instead moves misses
from the costly side to the tolerable side, and costs little in band:

| Series                | Median (q50): in / too high / too low | q30: in / too high / too low |
| --------------------- | ------------------------------------: | ---------------------------: |
| container memory      |                       68% / 22% / 10% |              67% / 13% / 19% |
| node count            |                        71% / 8% / 21% |               72% / 4% / 24% |
| cluster CPU requested |                       52% / 17% / 31% |               56% / 8% / 36% |
| container CPU         |                       52% / 21% / 28% |               47% / 9% / 44% |
| cluster CPU used      |                       36% / 24% / 41% |              37% / 11% / 53% |

At q30 the too-high share roughly halves on every series type, and the in-band share moves by
at most 5 points. For a warning an agent acts on, that is the better trade. It does not move any
series type toward 90% in band ([`results/pointwise.md`](results/pointwise.md) has q20 to q60).

### Longer history does not help

Where 28 days of history existed, feeding 28 days instead of 7 moved the hourly in-band share
from 67% to 69% on the same 281 series-days. Averaging forecasts made from 1, 7 and 28 days of
history, the stand-in for the "last day, last month, same month last year" idea, was no better
than 28 days alone. The yearly part could not be tested: no cluster in reach is a year old, and
Cloud Monitoring keeps five-minute data for six weeks.

### Warnings before a threshold is crossed add little

We also tested the use the design has in mind: warn when tomorrow's forecast reaches a
threshold. Each series got a threshold its busiest hour crossed on about one day in ten.

- **Few false alarms.** The median forecast raised 12 warnings, and 10 were right. False alarms
  fell on 0.1% of series-days.
- **Few catches.** It caught 10 of 196 crossings, 5%.
- **Little the proactive agent lacks.** Of 148 crossings that were new that day, not seen the
  day before, it warned about 3, with no false alarms.

Threshold crossings are the atypical days, and a forecast that cannot place a burst in time
does not see them coming. [`results/decision.md`](results/decision.md) has the tables.

### A busy staging cluster forecasts far better

The evaluation hosts are small and bursty by design. To see whether that drives the result, we
ran the 8-against-24-hour test on one real staging cluster of a Google first-party customer:
GKE Autopilot, about ten nodes, serving a real service's load. Its corpus is 50 series over the
same 41 days: 23 container memory, 23 container CPU, cluster CPU used and requested, node count,
and one disk volume. Forecasts started every 6 hours, 129 start times, with 7 days of history:

| Series                | Typical, 8 h: too high / too low | Typical, 24 h | Bad day, 8 h | Bad day, 24 h |
| --------------------- | -------------------------------: | ------------: | -----------: | ------------: |
| container memory      |                          0% / 1% |       1% / 2% |      4% / 9% |     13% / 14% |
| node count            |                         3% / 12% |      6% / 17% |    14% / 20% |     21% / 27% |
| cluster CPU requested |                          1% / 4% |       2% / 6% |      7% / 7% |     11% / 13% |
| container CPU         |                          3% / 9% |      8% / 17% |    16% / 34% |     24% / 46% |
| cluster CPU used      |                         5% / 11% |     10% / 19% |    16% / 27% |     26% / 37% |

Against the evaluation hosts, the bad-day overshoot 8 hours ahead falls from 20% to 4% for
memory, from 33% to 14% for node count, from 40% to 7% for requested CPU and from 54% to 16% for
cluster CPU used. On about ten nodes, one node is 10%, so node count's misses are about one
node. Memory and requested CPU stay within the band on most days even 24 hours ahead. The
single disk stays within 1% high and 8% low at worst. CPU still misses bursts on the low side.
[`results/window-staging.md`](results/window-staging.md) has both histories, including 24
hours of history, which changes these figures by at most 2 points.

This is one cluster, and pointwise scoring was not run on it, so it shows the direction rather
than a fleet-wide rate: on steady production-like load, over-forecasts are rare enough that
memory, requested CPU and node-count forecasts could carry a warning.

### Forecasting takes seconds, not hours

A forecast has to finish well inside the horizon it covers, or the agent falls behind. It does,
by three orders of magnitude. One forecaster container with 11 CPU cores (2.2 GHz Xeon, no GPU)
took, per request:

| History | Padded context | 1 series | 64 series | Seconds per series at 64 |
| ------- | -------------: | -------: | --------: | -----------------------: |
| 1 day   |            512 |    7.4 s |     7.6 s |                     0.12 |
| 7 days  |          2,048 |   19.5 s |    20.7 s |                     0.32 |
| 28 days |          8,192 |   72.2 s |    70.0 s |                      1.1 |

Those are 8-hour forecasts; 24-hour ones take the same time. The cost is set by the padded
context, not by the horizon or the batch size, so a request of 64 series costs what one series
does. A thousand series forecast 8 hours ahead from 7 days of history is 16 requests, about 5.5
minutes on one such container. The GKE run, one replica with 14 cores, was faster: 3.6, 7.6 and
29 seconds for 64 series. [`results/timing.md`](results/timing.md) has every combination.

## What would change the answer

- **Trend-driven series.** Disks, persistent volumes, quotas and memory that leaks grow rather
  than burst. No disk on these clusters lived long enough to forecast; a production fleet would
  have hundreds.
- **Shorter horizons.** Memory and node count reach 80% in band 1–6 hours ahead. An agent that
  forecasts a few hours ahead, several times a day, should be tested as its own design, against
  what the proactive agent already sees.
- **Scheduled load as input.** Known events, such as release days or nightly runs, can be passed
  to TimesFM as covariates. That is the one way to forecast a burst's timing, and it was not
  tried.
- **A different fleet.** The evaluation hosts run the harness's own bursty workloads. One
  customer staging cluster with steadier traffic forecast far better
  ([above](#a-busy-staging-cluster-forecasts-far-better)); a fleet of such clusters, scored point
  by point, would test that properly.

## Caveats

- Eleven small, young clusters running one workload, the evaluation harness.
- Six weeks, 33 forecast days. The yearly pattern is untested.
- The percentage band penalises small values heavily. Values below 5% of the weekly maximum are
  excluded, but low-utilisation CPU series still score worse partly for that reason.

## Detailed tables and reproduction

- [`results/pointwise.md`](results/pointwise.md): the point-by-point tables at both steps, for
  every method, by lead time and by forecast quantile.
- [`results/window.md`](results/window.md): largest overshoot and undershoot, 8 against 24
  hours ahead, with 24 hours and 7 days of history.
- [`results/horizon.md`](results/horizon.md): 8-hour against 24-hour forecasts as the share of
  hourly values in band, per time of day.
- [`results/window-staging.md`](results/window-staging.md): the same, on the staging cluster.
- [`results/decision.md`](results/decision.md): the threshold-warning tables.
- [`results/timing.md`](results/timing.md): forecast time by horizon, batch size and history.
- [`results/summary.md`](results/summary.md): the standard forecast-accuracy scores.

The [README](README.md) has the method and the commands. `harness/pointwise.py`,
`harness/decide.py` and `harness/peaks.py` produce these tables from a `backtest.py --dump`
run; `harness/window.py` and `harness/horizon.py` make and score the 8-hour forecasts, and
`harness/timing.py` times the forecaster.
