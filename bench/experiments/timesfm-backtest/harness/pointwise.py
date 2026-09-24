#!/usr/bin/env python3
"""Point-by-point accuracy of a day-ahead forecast: at midnight the model forecasts the next 24
hours, P1..Pn, and each Pi is compared with the value actually measured at the same time, Vi.

    python3 pointwise.py dump/ > results/pointwise.md

A point is "in band" when Pi lies between Vi - BAND_UNDER and Vi + BAND_OVER (relative to Vi),
"over" when it is higher, "under" when it is lower. Over-forecasting gets the tighter bound
because acting on a value that never arrives is the costly mistake. Readings are scored at the
collected 5-minute step and as hourly means (24 per day). A point whose actual value is below
MIN_SHARE_OF_WEEK_MAX of the series' maximum over the previous week is left out: a percentage
of a near-idle value measures noise, not the forecast, and the share left out is reported.
"""

import glob
import os
import sys

import numpy as np
import pandas as pd

from scope import included

DAY = 288
HOUR = 12
BAND_OVER = 0.05
BAND_UNDER = 0.10
TARGET = 0.9
MIN_SHARE_OF_WEEK_MAX = 0.05
# Quantile columns in the dump: 0 is the mean, 1..9 are q10..q90.
QUANTILE_COLUMNS = {f"q{10 * k}": k for k in range(1, 10)}
HEADLINE_QUANTILE = "q50"
SWEEP = ["q20", "q30", "q40", "q50", "q60"]
HEADLINE_ARM = "tfm-7d"
ARMS = {"tfm-7d": "TimesFM, 7 days of history", "tfm-28d": "TimesFM, 28 days of history",
        "snaive-1d": "repeat yesterday", "snaive-7d": "repeat the same day last week",
        "linear-7d": "straight line through the last 7 days"}
# Methods that need only a week of history, so they cover every scored (series, day).
SHORT_ARMS = ["tfm-7d", "snaive-1d", "snaive-7d", "linear-7d"]
STEPS = {"5-minute": 1, "hourly": HOUR}
# Forecast lead time, in hours after the forecast was made.
LEAD_BINS = [(0, 1), (1, 6), (6, 12), (12, 24)]
CLASSES = {
    "volume_used": "disk used", "container_memory": "container memory",
    "node_count": "node count", "cpu_requested": "cluster CPU requested",
    "container_cpu": "container CPU", "cluster_cpu": "cluster CPU used",
}
PERCENT = 100


def present(d):
    """The classes, in CLASSES order, that have at least one row in d."""
    return [c for c in CLASSES if (d.cls == c).any()]


def coarsen(x, step):
    if step == 1:
        return x
    with np.errstate(all="ignore"):
        return np.nanmean(x.reshape(*x.shape[:-1], -1, step), axis=-1)


def load(directory):
    """Per (arm, step, quantile): arrays of relative error, class, series and lead hour."""
    parts = {}
    for path in sorted(glob.glob(os.path.join(directory, "origin-*.npz"))):
        z = np.load(path)
        origin = int(os.path.basename(path)[7:9])
        classes = z["classes"].astype(str)
        ids = z["ids"].astype(str)
        floor = MIN_SHARE_OF_WEEK_MAX * np.nanmax(z["week"], axis=1)
        for arm in ARMS:
            if arm not in z.files:
                continue
            fc = z[arm]
            scored = ~np.isnan(fc[:, :, 0]).all(axis=1)
            for step_name, step in STEPS.items():
                truth = coarsen(z["truth"], step)
                lead = np.arange(truth.shape[1]) * step / HOUR
                for q, k in QUANTILE_COLUMNS.items():
                    pred = coarsen(fc[:, :, k], step)
                    for i in np.flatnonzero(scored):
                        if not included(ids[i]):
                            continue
                        v, p = truth[i], pred[i]
                        real = ~np.isnan(v) & ~np.isnan(p)
                        keep = real & (v > floor[i])
                        parts.setdefault((arm, step_name, q), []).append(pd.DataFrame({
                            "err": (p[keep] - v[keep]) / v[keep], "cls": classes[i],
                            "id": ids[i], "origin": origin, "lead": lead[keep]}))
                        parts.setdefault(("dropped", arm, step_name, q), []).append(
                            (int(real.sum() - keep.sum()), int(real.sum())))
    return parts


def frame(parts, arm, step, q=HEADLINE_QUANTILE):
    return pd.concat(parts[(arm, step, q)], ignore_index=True)


def split(err):
    over, under = err > BAND_OVER, err < -BAND_UNDER
    return (~over & ~under).mean(), over.mean(), under.mean()


def pct(x):
    return f"{PERCENT * x:.0f}%"


def class_table(d):
    rows = []
    for cls in present(d):
        name = CLASSES[cls]
        g = d[d.cls == cls]
        inside, over, under = split(g.err)
        per_series = g.groupby("id").err.apply(lambda e: split(e)[0])
        rows.append({"series": name, "series count": g.id.nunique(), "points": len(g),
                     "in band": pct(inside), "over by more than 5%": pct(over),
                     "under by more than 10%": pct(under),
                     "90% of errors lie between": f"{pct(np.percentile(g.err, 5))} and "
                                                  f"{pct(np.percentile(g.err, 95))}",
                     f"series with {pct(TARGET)} in band":
                         f"{(per_series >= TARGET).sum()} of {len(per_series)}"})
    inside, over, under = split(d.err)
    rows.append({"series": "all", "series count": d.id.nunique(), "points": len(d),
                 "in band": pct(inside), "over by more than 5%": pct(over),
                 "under by more than 10%": pct(under)})
    return pd.DataFrame(rows).set_index("series").fillna("")


def compare_table(parts, step, column, arms):
    """One statistic per class and arm, only on the (series, day) pairs every arm forecast, so
    a difference is the method's and not a difference in which series or days it covered."""
    frames = {arm: frame(parts, arm, step) for arm in arms}
    common = set.intersection(*(set(zip(d.id, d.origin)) for d in frames.values()))
    rows = {}
    for arm, d in frames.items():
        d = d[[k in common for k in zip(d.id, d.origin)]]
        rows[ARMS[arm]] = {f"{CLASSES[c]} ({d[d.cls == c].id.nunique()})":
                           pct(split(d[d.cls == c].err)[column]) for c in present(d)}
        rows[ARMS[arm]]["all"] = pct(split(d.err)[column])
    return pd.DataFrame(rows), len(common)


def lead_table(d):
    rows = {}
    for lo, hi in LEAD_BINS:
        g = d[(d.lead >= lo) & (d.lead < hi)]
        rows[f"{lo}-{hi} h ahead"] = {CLASSES[c]: pct(split(g[g.cls == c].err)[0])
                                      for c in present(g)}
    return pd.DataFrame(rows)


def sweep_table(parts, step):
    rows = {}
    for q in SWEEP:
        d = frame(parts, HEADLINE_ARM, step, q)
        rows[q] = {CLASSES[c]: "{} / {} / {}".format(*map(pct, split(d[d.cls == c].err)))
                   for c in present(d)}
    return pd.DataFrame(rows)


def dropped(parts, step):
    pairs = parts[("dropped", HEADLINE_ARM, step, HEADLINE_QUANTILE)]
    return sum(p[0] for p in pairs) / sum(p[1] for p in pairs)


def show(df):
    print(df.to_markdown())
    print()


def main():
    parts = load(sys.argv[1])
    print("# Point-by-point accuracy of day-ahead forecasts\n")
    print(f"Generated by `harness/pointwise.py`. Band: forecast between {pct(BAND_UNDER)} below "
          f"and {pct(BAND_OVER)} above the actual value. Median forecast (q50) unless stated. "
          f"Points below {pct(MIN_SHARE_OF_WEEK_MAX)} of the series' previous-week maximum are "
          f"left out: {pct(dropped(parts, '5-minute'))} of 5-minute points, "
          f"{pct(dropped(parts, 'hourly'))} of hourly ones.\n")
    for step in STEPS:
        print(f"## {step.capitalize()} readings, {ARMS[HEADLINE_ARM]}\n")
        show(class_table(frame(parts, HEADLINE_ARM, step)))
        for arms, what in ((SHORT_ARMS, "7-day methods"), (list(ARMS), "every method")):
            for column, stat in ((0, "in band"), (1, "over by more than 5%")):
                table, n = compare_table(parts, step, column, arms)
                print(f"### {step.capitalize()}: share {stat}, {what}\n")
                print(f"The {n} (series, day) pairs every method here forecast; series count "
                      "in brackets.\n")
                show(table)
        print(f"### {step.capitalize()}: share in band by lead time, {ARMS[HEADLINE_ARM]}\n")
        show(lead_table(frame(parts, HEADLINE_ARM, step)))
        print(f"### {step.capitalize()}: in band / over / under at other forecast quantiles\n")
        show(sweep_table(parts, step))


if __name__ == "__main__":
    main()
