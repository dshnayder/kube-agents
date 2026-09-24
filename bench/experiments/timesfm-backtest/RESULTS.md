# TimesFM day-ahead forecasting: results

## The answer

We forecast tomorrow's CPU usage, memory usage, requested CPU, node count and disk usage with
TimesFM and warned whenever the forecast reached a threshold. On 153 real series from 12 GKE
clusters over six weeks, this caught **9% of threshold crossings**. **84% of its warnings were
right.** It raised a false alarm on **0.2%** of series-days and missed a crossing on **12%**.

Almost every crossing it caught had started the day before, so the proactive agent would
already have seen it. Prediction adds value only on crossings that are new that day. Of the
**299** new crossings, TimesFM warned in advance of **3 (1%)**.

The accuracy hypothesis was that TimesFM predicts tomorrow's values within −10% to +5% of the
actual value, 90% of the time. It holds for disk usage only (90%). Memory reaches 76%, requested
CPU 61% and CPU usage 39–53%. For the day's busiest hour, repeating yesterday's values scores
within a few points of TimesFM on most series types.

**Recommendation: do not build the predictive agent on day-ahead threshold warnings for CPU,
memory or node count.** On these clusters the forecasts are low-noise, but they add almost no
warning the proactive agent does not already have. Forecasting may still pay off for
trend-driven resources, such as disks filling, memory leaks and quota growth, over horizons of
days. This experiment had too few such series to judge. [What would change the
answer](#what-would-change-the-answer) describes the experiment that would.

## The numbers at a glance

**Warnings.** The threshold is a limit each series crossed on about one day in ten. The rule
warns when tomorrow's forecast busiest hour reaches it. Scored over 33 days and 153 series.

| Rule                                  | Warnings | Right | Crossings caught | False alarms (share of series-days) |
| ------------------------------------- | -------: | ----: | ---------------: | ----------------------------------: |
| **All crossings** (420)               |          |       |                  |                                     |
| TimesFM                               |       43 |   84% |               9% |                                0.2% |
| TimesFM, sensitive setting (q90)      |      648 |   25% |              39% |                                 15% |
| Yesterday's pattern, repeated         |      498 |   31% |              37% |                                 10% |
| **New crossings only** (299)          |          |       |                  |                                     |
| TimesFM                               |        5 |   60% |               1% |                                0.1% |
| TimesFM, middle setting (q70)         |       75 |   29% |               7% |                                1.8% |
| TimesFM, sensitive setting (q90)      |      430 |   16% |              23% |                                 12% |
| Yesterday's pattern, repeated         |       91 |   41% |              12% |                                1.9% |
| Straight line through the last 7 days |       62 |   31% |               6% |                                1.5% |

**Accuracy.** Share of forecasts within −10% to +5% of the actual value:

| Series                | Every 5 minutes | Busiest hour of the day | Repeat yesterday, busiest hour |
| --------------------- | --------------: | ----------------------: | -----------------------------: |
| disk (volume) used    |             90% |                     90% |                            89% |
| container memory      |             76% |                     77% |                            73% |
| node count            |             77% |                     64% |                            63% |
| cluster CPU requested |             61% |                     36% |                            64% |
| container CPU         |             53% |                     43% |                            51% |
| cluster CPU used      |             39% |                      7% |                            24% |

## What was tested

**Data.** We used 41 days of five-minute Cloud Monitoring series, ending 2026-09-23, from 12 GKE
clusters: the production `kage-management` install and the agent host in each of the 11
evaluation projects. After dropping series too short or too sparse to forecast, 153 remained:
57 container memory, 57 container CPU, 12 each of cluster CPU used, cluster CPU requested and
node count, and 3 disk volumes. [README.md](README.md#corpus) has the selection rules.

**Forecasts.** On each of 33 days, TimesFM 2.5 read the previous 7 days, the length in the
original proposal, and forecast the next 24 hours: 288 values, each with a median and a 10–90%
range. Nothing from the forecast day was visible to it. A 28-day variant gave the same picture
and is in the detailed tables.

**Thresholds.** No series came within 73% of its real limit in the six weeks, so real limits
give nothing to catch. Each series therefore got a fixed threshold at the level its daily busiest
hour exceeded on 10% of days, the p90 threshold. A second threshold at 25% of days, p75, is in
the detailed tables. The forecaster never sees either.

**The warning rule.** Warn for tomorrow when the forecast busiest hour reaches the threshold.
"Busiest hour" is the highest one-hour average of the day. It stands for sustained load, which
is what a capacity alert fires on. The results with the single highest five-minute value are no
better. The default rule uses the median forecast. The q70 and q90 settings use the 70th and
90th percentiles of the forecast range, which warn more often.

**New crossings** are days on which the series was below its threshold the day before and above
it that day. These are the only crossings a forecast can warn about before the proactive agent,
which reads current state, sees them.

**Comparison rules** need no model:

- Yesterday's pattern, repeated, including its usual day-to-day change.
- The same day last week.
- A straight line through the last 7 days.

## Findings

### The forecasts are low-noise but miss most crossings

A TimesFM warning is usually right: 84% of the time at the p90 threshold and 97% at p75. But it
warns rarely. The model's error runs almost entirely in one direction: it underestimates.
Across every series type, the forecast busiest hour came out more than 5% above the actual on
fewer than 5% of days, and more than 10% below it on many. That is the error profile a noise-free
alert wants, and it is why false alarms are rare. It is also why most crossings are missed.

### What it does catch, the proactive agent already sees

Of the 36 crossings TimesFM caught at the p90 threshold, 33 belonged to series that were already
above the threshold the day before. The forecast is saying "still high tomorrow", which the
proactive agent already knows from the current state. On new crossings the forecast rarely rises
high enough to warn. Lowering the bar (q70, q90) buys recall at the cost of precision. No
setting beats repeating yesterday's pattern on both at once, and the only setting more precise
than it caught 3 crossings. The best series type, container
memory, shows the same result: TimesFM at q70 was right on 36% of warnings and caught 15%;
repeating yesterday was right on 46% and caught 23%.

### Why

TimesFM is good at the typical day. Its median error was about 40% lower than repeating
yesterday's values, its first-hour error less than a third, and its 10–90% band held 80% of the
actual values, as it should ([README.md](README.md#results)). A threshold crossing, though, is an
atypical day by definition: a burst of evaluation runs, a batch job, a deploy. Nothing in the
week before says when it will happen, so the model forecasts the typical level and spreads any
burst across the hours it might land in. A 24-hour-ahead forecast of a burst whose timing is
not in the history cannot do better. The same holds when the model forecasts the series of daily
peaks directly rather than the five-minute values: its warnings on new crossings were right only
10–17% of the time (the detailed tables' last section).

### Disk usage meets the accuracy target, on too few series to generalise

Disk usage grows smoothly, so its forecasts land within −10%/+5% 90% of the time. Repeating
yesterday does nearly as well (89%). Only 3 volumes qualified, with 2–3 new crossings among them.
A slowly growing series is where a forecast should add warning time, but this data cannot show
it.

### Longer history and the three-window idea

Feeding 28 days instead of 7 changed the typical-day error by 0.01 and did not improve the
warnings. On new crossings the 28-day median forecast raised none at the p90 threshold.
Averaging the 1-, 7- and 28-day forecasts, the stand-in for "last day, last month, same month
last year", was no better than the 28-day forecast alone. The yearly part could not be tested:
no cluster in reach is a year old, and Cloud Monitoring keeps five-minute data for six weeks.

### Cost is not the obstacle

On CPU only, one forecaster replica with 14 cores forecast 64 series for a day ahead in 7.6
seconds from 7 days of history, and 29 seconds from 28 days. A daily run over a thousand series
takes minutes.

## What would change the answer

Each of these would move the verdict, and the harness here runs any of them:

- **Trend-driven series.** Disks, persistent volumes, quotas, certificate counts and memory that
  leaks. These cross thresholds because they grow, not in bursts, and repeating yesterday cannot
  warn about them. They need a corpus with enough such series and real crossings: a production
  fleet or a replay of past incidents.
- **Longer horizons.** "This disk fills in nine days" is a more valuable warning than "this
  container peaks tomorrow", and it is the case the design's lead-time tiers are built for.
- **Real limits under pressure.** Nothing here came within 73% of a limit, so the question the
  design gates on, whether we warn before a real limit is hit, is still unscored.
- **Scheduled load as input.** Known events, such as release days or nightly runs, can be passed
  to TimesFM as covariates. That is the one way to forecast a burst's timing, and it was not
  tried.

Until one of these shows a new-crossing warning rate well above repeating yesterday's, a
predictive agent would mostly restate what the proactive agent already knows.

## Caveats

- The production install supplies 60 of the 153 series. The evaluation hosts are young and run
  the harness's own bursty workloads, which may be less predictable than a customer fleet's.
- Thresholds are placed from each series' own distribution, not by operators. A real limit sits
  where a person put it, often far above normal use.
- Six weeks and 33 forecast days. Each p90 table rests on about 300–420 crossings, enough to
  compare rules but not to split finely by series type.
- TimesFM ran zero-shot on CPU, with no fine-tuning and no covariates.

## Detailed tables and reproduction

[`results/decision.md`](results/decision.md) has every table behind this page:

- the accuracy bands per series type;
- the warning rules at every forecast setting, for both thresholds, both peak definitions,
  both history lengths and each series type;
- the daily-peak forecasts.

[`results/summary.md`](results/summary.md) has the standard forecast-accuracy scores. The
[README](README.md) has the method and the commands. `harness/decide.py` and `harness/peaks.py`
produce `decision.md` from a `backtest.py --dump` run.
