#!/usr/bin/env python3
"""Turn backtest.py's per-row metrics into the tables the README quotes.

    python3 analyze.py results/metrics.csv.gz results/latency.csv.gz > results/summary.md

Two origin sets, because the arms need different history: the long set is every origin with 28
days behind it, where all seven arms compete; the short set is every origin, without tfm-28d and
the ensemble. Within a set only the (series, origin) pairs every arm scored are compared: a series
from a cluster younger than 28 days has no tfm-28d forecast, so it sits in the short set only. MASE is scaled by the in-sample seasonal-naive-1d error of the week before the
origin, the same scale for every arm, so a MASE below 1 beats yesterday-repeated on its own week.
"""

import sys

import numpy as np
import pandas as pd

LONG_FROM = 28
NEW_HIGH_MARGIN = 0.1
# Conformal peak correction: each series' own past peak misses, at this coverage, need at least
# this many earlier origins of the same arm before the correction is trusted.
PEAK_COVERAGE = 0.9
MIN_CALIBRATION = 5
REFERENCE = "snaive-1d"
ARM_ORDER = ["snaive-1d", "snaive-7d", "linear-7d", "tfm-1d", "tfm-7d", "tfm-28d", "tfm-ens"]


def complete(df):
    """Only the (series, origin) pairs every arm in df scored, so arms compete on one set."""
    arms = df.arm.nunique()
    n = df.groupby(["id", "origin"]).arm.transform("nunique")
    return df[n == arms]


def table(df):
    return df.to_markdown(floatfmt=".3f")


def arm_summary(df):
    ref = df[df.arm == REFERENCE].set_index(["id", "origin"]).mase
    out = []
    for arm, g in df.groupby("arm"):
        r = ref.reindex(g.set_index(["id", "origin"]).index).values
        m = g.mase.values
        valid = ~(pd.isna(m) | pd.isna(r))
        out.append(dict(
            arm=arm, rows=len(g), median_mase=g.mase.median(), mean_mase=g.mase.mean(),
            median_mase_1h=g.mase_1h.median(), wql=g.wql_num.sum() / g.abs_sum.sum(),
            cov80=g.cov80.mean(),
            peak_covered=(g.truth_max <= g.pred_max_q90).mean(),
            median_peak_err=((g.pred_max - g.truth_max).abs() / g.scale).median(),
            beats_snaive1d=(m[valid] < r[valid]).mean()))
    return pd.DataFrame(out).set_index("arm").reindex([a for a in ARM_ORDER
                                                        if a in set(df.arm)])


def events(df, truth, pred):
    out = []
    for arm, g in df.groupby("arm"):
        g = g[g[truth].astype(str) != ""]
        g = g.assign(**{c: pd.to_numeric(g[c]).astype(int) for c in (truth, pred)})
        tp = int(((g[truth] == 1) & (g[pred] == 1)).sum())
        fp = int(((g[truth] == 0) & (g[pred] == 1)).sum())
        fn = int(((g[truth] == 1) & (g[pred] == 0)).sum())
        out.append(dict(arm=arm, events=tp + fn, alerts=tp + fp, tp=tp, fp=fp, fn=fn,
                        precision=tp / (tp + fp) if tp + fp else float("nan"),
                        recall=tp / (tp + fn) if tp + fn else float("nan")))
    return pd.DataFrame(out).set_index("arm").reindex([a for a in ARM_ORDER
                                                        if a in set(df.arm)])


def conformal_peaks(df):
    """Add pred_peak_cal: the day's max q90 raised by the split-conformal quantile of the
    series' own earlier misses (truth_max - pred_max_q90) under the same arm. Uses only origins
    before the one being scored, so it is a forecast, not a fit."""
    df = df.sort_values(["id", "arm", "origin"]).copy()
    miss = (df.truth_max - df.pred_max_q90).values
    cal = np.full(len(df), np.nan)
    for _, idx in df.groupby(["id", "arm"]).indices.items():
        for j in range(MIN_CALIBRATION, len(idx)):
            past = miss[idx[:j]]
            level = min(1.0, np.ceil((len(past) + 1) * PEAK_COVERAGE) / len(past))
            cal[idx[j]] = np.quantile(past, level, method="higher")
    df["pred_peak_cal"] = df.pred_max_q90 + np.maximum(cal, 0)
    threshold = df.ctx_max + NEW_HIGH_MARGIN * (df.ctx_max - df.ctx_min)
    df["pred_new_high_cal"] = (df.pred_peak_cal > threshold).astype(int)
    return df


def calibrated_summary(df):
    out = []
    for arm, g in df.groupby("arm"):
        tp = int(((g.new_high == 1) & (g.pred_new_high_cal == 1)).sum())
        fp = int(((g.new_high == 0) & (g.pred_new_high_cal == 1)).sum())
        fn = int(((g.new_high == 1) & (g.pred_new_high_cal == 0)).sum())
        out.append(dict(arm=arm, rows=len(g),
                        peak_covered_raw=(g.truth_max <= g.pred_max_q90).mean(),
                        peak_covered_cal=(g.truth_max <= g.pred_peak_cal).mean(),
                        median_headroom=((g.pred_peak_cal - g.truth_max) / g.scale).median(),
                        events=tp + fn, alerts=tp + fp,
                        precision=tp / (tp + fp) if tp + fp else float("nan"),
                        recall=tp / (tp + fn) if tp + fn else float("nan")))
    return pd.DataFrame(out).set_index("arm").reindex([a for a in ARM_ORDER
                                                        if a in set(df.arm)])


def main():
    df = pd.read_csv(sys.argv[1], keep_default_na=True, dtype={"limit_hit": object,
                                                               "pred_limit_hit": object})
    df["limit_hit"] = df.limit_hit.fillna("")
    df["pred_limit_hit"] = df.pred_limit_hit.fillna("")
    lat = pd.read_csv(sys.argv[2])
    long = complete(df[df.origin >= LONG_FROM])
    short = complete(df[~df.arm.isin(["tfm-28d", "tfm-ens"])])
    ids = df.id.nunique()
    print(f"# TimesFM backtest summary\n\n{ids} series, {df.cluster.nunique()} clusters, "
          f"origins {df.origin.min()}..{df.origin.max()} (long set from {LONG_FROM}).\n")
    print("Series per class:\n")
    print(table(df.groupby("class").id.nunique().to_frame("series")))
    print(f"\n## All arms, long origin set ({long.origin.nunique()} origins)\n")
    print(table(arm_summary(long)))
    print(f"\n## Short-context arms, every origin ({short.origin.nunique()} origins)\n")
    print(table(arm_summary(short)))
    print("\n## Median MASE by class, long origin set\n")
    print(table(long.pivot_table(index="class", columns="arm", values="mase",
                                 aggfunc="median")[[a for a in ARM_ORDER if a in set(long.arm)]]))
    print("\n## WQL by class, long origin set\n")
    w = long.groupby(["class", "arm"]).apply(lambda g: g.wql_num.sum() / g.abs_sum.sum(),
                                            include_groups=False).unstack()
    print(table(w[[a for a in ARM_ORDER if a in w.columns]]))
    print("\n## New-high days (peak clears the prior week's max by 10% of its range), "
          "flagged by q90, long origin set\n")
    print(table(events(long, "new_high", "pred_new_high")))
    cal = conformal_peaks(df).dropna(subset=["pred_peak_cal"])
    first = cal.groupby("arm").origin.min().max()
    cal = complete(cal[cal.origin >= first])
    print(f"\n## Daily peak with conformal correction ({PEAK_COVERAGE:.0%} target, "
          f"origins {first}..{cal.origin.max()}, where every arm has "
          f"{MIN_CALIBRATION}+ earlier origins)\n")
    print(table(calibrated_summary(cal)))
    lim = long[long.limit_hit != ""]
    if len(lim):
        print("\n## Limit days (peak at or above 90% of the limit), flagged by q90, "
              "long origin set\n")
        print(table(events(lim, "limit_hit", "pred_limit_hit")))
    print("\n## Forecaster latency (CPU, one batch of up to 64 series, 288-step horizon)\n")
    print(table(lat.groupby(["arm", "bucket"]).seconds.describe()[["count", "50%", "max"]]))


if __name__ == "__main__":
    main()
