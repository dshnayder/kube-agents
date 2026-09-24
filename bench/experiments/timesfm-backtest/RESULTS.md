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

**Result:** at midnight UTC each day, TimesFM forecast the next 24 hours of CPU usage, requested CPU, memory
usage, node count and disk usage. We compared each forecast hourly value with the value actually
measured in that hour. **70%** of forecasts landed within −10%/+5%. **15%** were more than 5%
too high, which is the side that raises false alarms. **16%** were more than 10% too low, which
is the side that misses. The hypothesis holds for disk usage (90%). It does not hold for the
other series types.

| Series                | Within −10%/+5% | More than 5% too high | More than 10% too low | Repeating yesterday: within / too high |
| --------------------- | --------------: | --------------------: | --------------------: | -------------------------------------: |
| disk used             |         **90%** |                    4% |                    6% |                               84% / 5% |
| container memory      |             78% |                   14% |                    8% |                              68% / 21% |
| node count            |             75% |                    7% |                   18% |                              70% / 15% |
| container CPU         |             65% |                   16% |                   19% |                              50% / 31% |
| cluster CPU requested |             60% |                   14% |                   26% |                              55% / 23% |
| cluster CPU used      |             40% |                   22% |                   37% |                              29% / 38% |
| **all 153 series**    |         **70%** |               **15%** |               **16%** |                          **58% / 25%** |

What this means for the predictive agent:

- **As stated, the hypothesis is not confirmed.** A full day ahead, only smoothly growing series
  such as disk usage are forecast within the band 90% of the time. Of the 153 series, 24 reach
  90% on their own.
- **The model is clearly better than the simple rule.** On every series type, TimesFM puts more
  values in the band than repeating yesterday: 70% against 58%. It also over-forecasts
  less often: 15% against 25%. Prediction does add accuracy.
- **Accuracy depends on how far ahead you look.** For the next 1–6 hours, 88% of memory values
  and 83% of node-count values are in the band. At 12–24 hours ahead the figures are 73% and
  70%. An agent that acts a few hours ahead comes close to 90% for memory and node count. One that acts a day ahead does not.
- **Forecasting 8 hours ahead instead of 24 roughly halves the worst miss.** On a typical day,
  a memory forecast made 8 hours ahead is never more than 1% too high or 2% too low in any hour
  of the 8. A 24-hour forecast misses by up to 3% and 5%. On a bad day (1 in 10), the figures
  are 14% either way against about 25%. On a typical day, an 8-hour forecast of disk, memory
  or container CPU is never more than 3% too high. On a bad day, only disk stays within a few
  percent; everything else overshoots by 14% or more
  ([8 hours ahead or 24?](#8-hours-ahead-or-24)).
- **CPU is not predictable at this precision.** To catch 90% of hourly container-CPU values, the
  band would have to widen to −27%/+15%. For cluster CPU used, it would take −67%/+26%.

**Recommendation:** do not build the predictive agent on the promise of 24-hour-ahead forecasts
within −10%/+5% for CPU, memory or node count. The data supports two narrower uses:

- forecasting smoothly trending resources, such as disks, a day or more ahead;
- forecasting memory and node count a few hours ahead.

In both, TimesFM already beats repeating yesterday. Neither has been tested against what the
proactive agent sees from current values; the next experiment should do that on a production
fleet (see [What would change the answer](#what-would-change-the-answer)).

## What was tested

**Data.** We used 41 days of five-minute Cloud Monitoring series, ending 2026-09-23, from 12 GKE
clusters: the production `kage-management` install and the agent host in each of the 11
evaluation projects. After dropping series too short or too sparse to forecast, 153 remained:
57 container memory, 57 container CPU, 12 each of cluster CPU used, cluster CPU requested and
node count, and 3 disk volumes. [README.md](README.md#corpus) has the selection rules.

**Forecasts.** On each of 33 days, TimesFM 2.5 read the previous 7 days, the length in the
original proposal, and forecast the next 24 hours as 288 five-minute values. We used its median
forecast. Nothing from the forecast day was visible to it. It ran zero-shot, with no training
on this data.

**Comparison.** For every forecast value Pi, we took the value Vi actually measured at the same
time. The forecast is in band when Pi lies between Vi − 10% and Vi + 5%. Over-forecasting gets
the tighter bound because acting on a value that never arrives is the costly mistake. The
headline scores hourly values, the mean of each hour, 24 per day and about 80,000 in total. At
the raw five-minute step the figures are 64% in band, 20% too high and 16% too low
([`results/pointwise.md`](results/pointwise.md)). Values below 5% of the series' maximum over
the previous week, about 1% of the total, are left out: a percentage of a near-idle value
measures noise, not the forecast.

**No-model rules**, scored the same way on the same series and days:

- Repeat yesterday: yesterday's values, shifted by their median day-to-day change.
- Repeat the same day last week.
- A straight line through the last 7 days.

Repeating yesterday was the strongest, so it is the one in the table above.

## Findings

### Only disk usage reaches 90% in band a day ahead

The table above is the result. The misses fall on both sides, and which side depends on the
series type:

- Memory is more often too high than too low (14% against 8%).
- Node count and CPU are more often too low. The model does not foresee a scale-up or a burst
  that nothing in the past week announced.

Per series, 90% in band is reached by 2 of 3 disks, 15 of 57 memory series, 4 of 12 node counts, 1 of
12 requested-CPU series, 2 of 57 container-CPU series and none of the cluster-CPU series.

### TimesFM beats every rule that needs no model

On the same series and days, TimesFM puts 70% of hourly values in the band. Repeating yesterday
puts 58%, repeating last week 42%, and a straight-line trend 52%. It also produces fewer
over-forecasts than repeating yesterday: 15% against 25% overall, and 16% against 31% on
container CPU. This matches the standard accuracy scores in
[`results/summary.md`](results/summary.md), where TimesFM's typical error is about 40% lower
than repeating yesterday's.

### Accuracy falls with lead time

Share of hourly values in band, by how far ahead of the forecast they are:

| Series                | 0–1 h | 1–6 h | 6–12 h | 12–24 h |
| --------------------- | ----: | ----: | -----: | ------: |
| disk used             |  100% |  100% |    90% |     85% |
| container memory      |   96% |   88% |    79% |     73% |
| node count            |   92% |   83% |    78% |     70% |
| container CPU         |   86% |   72% |    64% |     60% |
| cluster CPU requested |   86% |   70% |    66% |     51% |
| cluster CPU used      |   68% |   49% |    42% |     34% |

The first hour is forecast well for everything except cluster CPU used. After that, accuracy
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
33 days, for all 153 series: about 15,000 forecasts per horizon. The tables give the largest
overshoot and undershoot on a typical day (the median forecast) and on a bad day (the worst 1
in 10). "0%" means under half a percent.

**On a typical day:**

| Series                | 8 h ahead: too high by at most | 8 h ahead: too low by at most | 24 h ahead: too high by at most | 24 h ahead: too low by at most |
| --------------------- | -----------------------------: | ----------------------------: | ------------------------------: | -----------------------------: |
| disk used             |                             0% |                            0% |                              0% |                             1% |
| container memory      |                             1% |                            2% |                              3% |                             5% |
| node count            |                             0% |                            0% |                              0% |                             8% |
| container CPU         |                             3% |                            8% |                              7% |                            15% |
| cluster CPU requested |                             1% |                           12% |                              4% |                            32% |
| cluster CPU used      |                             6% |                           36% |                             17% |                            66% |

**On a bad day (worst 1 in 10):**

| Series                | 8 h ahead: too high by at most | 8 h ahead: too low by at most | 24 h ahead: too high by at most | 24 h ahead: too low by at most |
| --------------------- | -----------------------------: | ----------------------------: | ------------------------------: | -----------------------------: |
| disk used             |                             1% |                            3% |                              6% |                            13% |
| container memory      |                            14% |                           14% |                             25% |                            24% |
| node count            |                            28% |                           35% |                             43% |                            45% |
| container CPU         |                            18% |                           31% |                             32% |                            45% |
| cluster CPU requested |                            38% |                           43% |                             44% |                            48% |
| cluster CPU used      |                            50% |                           72% |                             79% |                            76% |

How to read it:

- **Disk and memory.** On a typical day, an 8-hour forecast stays within 2% of the actual value
  in every hour. On a bad day, disk still does. Memory reaches 14% either way.
- **Node count** changes in whole nodes. On most days nothing changes and the forecast is exact.
  On a day the cluster scales, the forecast is off by one or more nodes, and one node is a third
  of a 3-node cluster.
- **CPU** misses mostly by forecasting too low: the model does not foresee bursts. Container
  CPU 8 hours ahead is at most 3% high and 8% low on a typical day, and 18% high and 31% low on a
  bad one. Cluster-level CPU misses by double digits even on a typical day.

Part of the difference is that a 24-hour window has three times as many hours in which to miss.
To remove that, we also compared the same 8 clock hours forecast 8 hours ahead and 16–24 hours
ahead. On a typical day the two are close: memory 1%/2% both ways; container CPU 3%/8% against
4%/9%. On a bad day, the fresher forecast is better by a few points: memory 14%/14% against
20%/18%; container CPU 18%/31% against 25%/34%. Re-forecasting more often helps, but less than
the tables above suggest.

With 24 hours of history instead of 7 days, the typical-day figures change by at most 4
points. On bad days, 7 days helps mainly cluster CPU used, where the 8-hour overshoot falls
from 82% to 50%. [`results/window.md`](results/window.md) has both histories.

### Shifting the forecast lower does not help

The median forecast is used throughout. A lower quantile cuts the too-high share but adds as
much to the too-low share. For memory at q30, 8% of values are too high and 14% too low, with
78% in band, the same as the median. No quantile raises any series type's in-band share by more
than 3 points (the quantile tables in [`results/pointwise.md`](results/pointwise.md)).

### Longer history does not help

Where 28 days of history existed, feeding 28 days instead of 7 moved the hourly in-band share
from 76% to 77% on the same series and days. Averaging forecasts made from 1, 7 and 28 days of
history, the stand-in for the "last day, last month, same month last year" idea, was no better
than 28 days alone. The yearly part could not be tested: no cluster in reach is a year old, and
Cloud Monitoring keeps five-minute data for six weeks.

### Warnings before a threshold is crossed add little

We also tested the use the design has in mind: warn when tomorrow's forecast reaches a
threshold. Each series got a threshold its busiest hour crossed on about one day in ten.

- **Few false alarms.** A warning on the median forecast was right 84% of the time, and false
  alarms fell on 0.2% of series-days.
- **Few catches.** It caught only 9% of crossings.
- **Little the proactive agent lacks.** Almost every catch had already crossed the day before,
  which the proactive agent sees from current values. Of 299 crossings that were new that day,
  it warned about 3.

Threshold crossings are the atypical days, and a forecast that cannot place a burst in time
does not see them coming. [`results/decision.md`](results/decision.md) has the tables.

### Cost is not the obstacle

On CPU only, one forecaster replica with 14 cores forecast 64 series for a day ahead in 7.6
seconds from 7 days of history. A daily run over a thousand series takes minutes.

## What would change the answer

- **Trend-driven series.** Disks, persistent volumes, quotas and memory that leaks grow rather
  than burst, which is where disk's 90% comes from. This corpus had 3 disks. A production fleet
  would have hundreds.
- **Shorter horizons.** Memory and node count come close to 90% in band within 6 hours. An agent
  that forecasts a few hours ahead, several times a day, should be tested as its own design.
- **Scheduled load as input.** Known events, such as release days or nightly runs, can be passed
  to TimesFM as covariates. That is the one way to forecast a burst's timing, and it was not
  tried.
- **A different fleet.** The evaluation hosts run the harness's own bursty workloads. A customer
  fleet with steadier traffic may forecast better.

## Caveats

- The production install supplies 60 of the 153 series. The other clusters are young evaluation
  hosts.
- Disk results rest on 3 volumes.
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
- [`results/decision.md`](results/decision.md): the threshold-warning tables.
- [`results/summary.md`](results/summary.md): the standard forecast-accuracy scores.

The [README](README.md) has the method and the commands. `harness/pointwise.py`,
`harness/decide.py` and `harness/peaks.py` produce these tables from a `backtest.py --dump`
run; `harness/window.py` and `harness/horizon.py` make and score the 8-hour forecasts.
