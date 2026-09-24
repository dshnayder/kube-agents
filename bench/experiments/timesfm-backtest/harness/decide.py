#!/usr/bin/env python3
"""Answer the decision questions from backtest.py --dump: how often is a day-ahead forecast
inside a tolerance band, and how well does "warn when tomorrow's forecast peak reaches the
threshold" work, above all on days the series was still below the threshold the day before.

    python3 decide.py dump-r2/ [peaks.csv.gz] > results/decision.md

Thresholds are fixed per series, placed at a percentile of that series' own daily peaks over
the scored days, so a p90 threshold is one the series crossed on about one day in ten. The
forecaster never sees them. A rule warns for day T when the forecast's highest value of the
chosen quantile over day T reaches the threshold.
"""

import glob
import os
import sys

import numpy as np
import pandas as pd

from scope import included

DAY = 288
# Tolerance band: the forecast may sit up to BAND_OVER above the actual value (the direction
# that raises false alarms) and up to BAND_UNDER below it (the direction that misses, which the
# proactive agent catches when it happens).
BAND_OVER = 0.05
BAND_UNDER = 0.10
BAND_TARGET = 0.9
THRESHOLD_PERCENTILES = (90, 75)
# Quantile columns in the dump: 0 is the mean, 1..9 are q10..q90.
QUANTILE_COLUMNS = {f"q{10 * k}": k for k in range(1, 10)}
SWEEP = ["q10", "q30", "q50", "q70", "q90"]
# What "the day's peak" means: the highest single 5-minute point, or the highest hourly mean
# (load sustained for an hour, which is what a capacity alert usually fires on).
WINDOWS = {"5-minute peak": 1, "busiest hour": 12}
HEADLINE_QUANTILE = "q50"
MODEL_ARMS = ["tfm-7d", "tfm-28d"]
NO_MODEL_ARMS = ["snaive-1d", "snaive-7d", "linear-7d"]
# Points whose actual value is below this share of the series' weekly maximum are left out of
# the per-point band: a percentage of a near-idle value measures noise, not the forecast.
MIN_SHARE_OF_WEEK_MAX = 0.05
CLASS_GROUPS = {
    "cluster_cpu": "cluster CPU used", "cpu_requested": "cluster CPU requested",
    "node_count": "node count", "container_memory": "container memory",
    "container_cpu": "container CPU", "volume_used": "volume used",
}


def day_peak(x, window):
    """Highest mean over consecutive windows of `window` points; NaNs ignored."""
    if window == 1:
        return np.nanmax(x)
    with np.errstate(all="ignore"):
        return np.nanmax(np.nanmean(x.reshape(-1, window), axis=1))


def load(directory):
    """One row per (series, origin, arm) with the day's truth and forecast arrays."""
    rows = []
    for path in sorted(glob.glob(os.path.join(directory, "origin-*.npz"))):
        z = np.load(path)
        origin = int(os.path.basename(path)[7:9])
        for arm in MODEL_ARMS + NO_MODEL_ARMS:
            if arm not in z.files:
                continue
            f = z[arm]
            for i in range(len(z["ids"])):
                if np.isnan(f[i]).all() or not included(z["ids"][i]):
                    continue
                rows.append(dict(id=str(z["ids"][i]), cls=str(z["classes"][i]), origin=origin,
                                 arm=arm, truth=z["truth"][i], week=z["week"][i], fc=f[i]))
    return pd.DataFrame(rows)


def paired(df, arms):
    """Rows of the given arms, only where every one of them forecast the (series, origin)."""
    d = df[df.arm.isin(arms)]
    n = d.groupby(["id", "origin"]).arm.transform("nunique")
    return d[n == len(arms)]


def band_table(df, arm):
    """Share of forecasts inside the band, per class: every 5-minute point, and the day's peak."""
    out = []
    for cls, g in df[df.arm == arm].groupby("cls"):
        pts, peaks, hours = [], [], []
        for truth, week, fc in zip(g.truth, g.week, g.fc):
            pred = fc[:, QUANTILE_COLUMNS[HEADLINE_QUANTILE]]
            ok = ~np.isnan(truth) & (truth > MIN_SHARE_OF_WEEK_MAX * week.max())
            pts.append((pred[ok] - truth[ok]) / truth[ok])
            for out_list, window in ((peaks, 1), (hours, 12)):
                actual = day_peak(truth, window)
                if actual > 0:
                    out_list.append((day_peak(pred, window) - actual) / actual)
        pts, peaks, hours = np.concatenate(pts), np.array(peaks), np.array(hours)
        inside = lambda r: np.mean((r <= BAND_OVER) & (r >= -BAND_UNDER))  # noqa: E731
        out.append({"class": CLASS_GROUPS.get(cls, cls), "series-days": len(g),
                    "5-min points in band": inside(pts),
                    "5-min peak in band": inside(peaks),
                    "busiest hour in band": inside(hours),
                    "busiest hour error, 5th pct": np.percentile(hours, 5),
                    "busiest hour error, median": np.median(hours),
                    "busiest hour error, 95th pct": np.percentile(hours, 95)})
    return pd.DataFrame(out).set_index("class")


def peaks(df, quantile, window):
    """Per row: actual peak, forecast peak at the quantile, and yesterday's actual peak."""
    k = QUANTILE_COLUMNS[quantile]
    return df.assign(actual=[day_peak(t, window) for t in df.truth],
                     forecast=[day_peak(fc[:, k], window) for fc in df.fc],
                     yesterday=[day_peak(w[-DAY:], window) for w in df.week])


def thresholds(df, percentile, window):
    """Per series, a fixed threshold at a percentile of its own daily peaks."""
    per_day = df.drop_duplicates(["id", "origin"]).assign(
        actual=lambda d: [day_peak(t, window) for t in d.truth])
    return per_day.groupby("id").actual.apply(lambda a: np.percentile(a, percentile))


def confusion(p, theta, fresh_only):
    p = p.assign(theta=p.id.map(theta))
    p = p[p.actual != p.theta]  # the series whose peak is the threshold itself decide nothing
    if fresh_only:
        p = p[p.yesterday < p.theta]
    event, warn = p.actual > p.theta, p.forecast >= p.theta
    tp, fp = int((event & warn).sum()), int((~event & warn).sum())
    fn, tn = int((event & ~warn).sum()), int((~event & ~warn).sum())
    n = tp + fp + fn + tn
    return dict(cases=n, crossings=tp + fn, warnings=tp + fp, caught=tp,
                false_alarms=fp, missed=fn,
                precision=tp / (tp + fp) if tp + fp else float("nan"),
                recall=tp / (tp + fn) if tp + fn else float("nan"),
                false_alarm_rate=fp / n if n else float("nan"),
                missed_rate=fn / n if n else float("nan"))


def rule_table(df, arms, percentile, fresh_only, quantiles, window):
    theta = thresholds(df, percentile, window)
    out = []
    for arm in arms:
        for q in quantiles if arm.startswith("tfm") else [HEADLINE_QUANTILE]:
            label = f"{arm} {q}" if arm.startswith("tfm") else arm
            out.append({"rule": label,
                        **confusion(peaks(df[df.arm == arm], q, window), theta, fresh_only)})
    return pd.DataFrame(out).set_index("rule")


PEAK_RULES = {"yesterday's peak": "yesterday", "highest peak of the last 7 days": "max7",
              "trend of the last 7 daily peaks": "trend7"}
# The headline rule's quantile is chosen on the early origins and reported on the later ones,
# so the headline is not tuned on the days it reports. Chosen: the highest-recall quantile whose
# false alarms stay at or under this share of its warnings.
SPLIT_ORIGIN = 25
MAX_FALSE_ALARM_SHARE = 0.1


def peak_thresholds(p, percentile):
    return p.groupby("id").actual.apply(lambda a: np.percentile(a, percentile))


def peak_confusion(p, column, theta, fresh_only):
    return confusion(p.assign(forecast=p[column]), theta, fresh_only)


def peak_rules(p, percentile, fresh_only, quantiles):
    theta = peak_thresholds(p, percentile)
    out = [{"rule": f"TimesFM, tomorrow's peak, {q}", **peak_confusion(p, q, theta, fresh_only)}
           for q in quantiles]
    out += [{"rule": name, **peak_confusion(p, col, theta, fresh_only)}
            for name, col in PEAK_RULES.items()]
    return pd.DataFrame(out).set_index("rule")


def peak_band(p):
    out = []
    for cls, g in p.groupby("class"):
        row = {"class": CLASS_GROUPS.get(cls, cls), "series-days": len(g)}
        g = g[g.actual > 0]
        for label, col in (("TimesFM", "q50"), ("yesterday's peak", "yesterday")):
            r = (g[col] - g.actual) / g.actual
            row[f"{label}: in band"] = np.mean((r <= BAND_OVER) & (r >= -BAND_UNDER))
            row[f"{label}: 90% of errors within"] = (f"{np.percentile(r, 5):+.0%} .. "
                                                     f"{np.percentile(r, 95):+.0%}")
        out.append(row)
    return pd.DataFrame(out).set_index("class")


def headline(p, percentile):
    """Pick the quantile on origins before SPLIT_ORIGIN, report it on the rest."""
    early, late = p[p.origin < SPLIT_ORIGIN], p[p.origin >= SPLIT_ORIGIN]
    theta = peak_thresholds(p, percentile)
    best = None
    for q in QUANTILE_COLUMNS:
        c = peak_confusion(early, q, theta, True)
        if c["warnings"] and c["false_alarms"] <= MAX_FALSE_ALARM_SHARE * c["warnings"]:
            if best is None or c["recall"] > best[1]["recall"]:
                best = (q, c)
    if best is None:
        return None, None, None
    return best[0], best[1], {"all days": peak_confusion(late, best[0], theta, False),
                              "fresh crossings": peak_confusion(late, best[0], theta, True)}


def peak_section(path):
    p = pd.read_csv(path)
    p = p[p.id.map(included)]
    print("## Forecasting tomorrow's peak directly\n")
    print(f"TimesFM reads the series of daily peaks up to the origin and forecasts one step. "
          f"{p.id.nunique()} series, origins {p.origin.min()}..{p.origin.max()}.\n")
    for kind, g in p.groupby("kind", sort=False):
        print(f"### {kind}: forecast inside −{BAND_UNDER:.0%}/+{BAND_OVER:.0%} of the actual\n")
        show(peak_band(g))
        for pct in THRESHOLD_PERCENTILES:
            for fresh in (False, True):
                scope = ("days the series was below the threshold the day before"
                         if fresh else "all days")
                print(f"### {kind}, threshold at each series' p{pct}, {scope}\n")
                show(peak_rules(g, pct, fresh, list(QUANTILE_COLUMNS)))
            q, chosen, late = headline(g, pct)
            print(f"### {kind}, p{pct}: headline rule chosen on origins < {SPLIT_ORIGIN}, "
                  f"reported on origins ≥ {SPLIT_ORIGIN}\n")
            if q is None:
                print("No quantile kept false alarms under the limit on the early origins.\n")
                continue
            print(f"Chosen quantile: {q} (early origins: precision {chosen['precision']:.2f}, "
                  f"recall {chosen['recall']:.2f}).\n")
            show(pd.DataFrame(late).T)
        for cls, c in g.groupby("class"):
            print(f"### {kind}, p90 threshold, fresh crossings, {CLASS_GROUPS.get(cls, cls)}\n")
            show(peak_rules(c, 90, True, ["q30", "q50", "q70", "q90"]))


def show(frame):
    print(frame.to_markdown(floatfmt=".3f"))
    print()


def main():
    df = load(sys.argv[1])
    print("# Decision tables\n")
    for model in MODEL_ARMS:
        d = paired(df, [model] + NO_MODEL_ARMS)
        if d.empty:
            continue
        print(f"## {model} against the no-model rules: origins {d.origin.min()}..{d.origin.max()}, "
              f"{d.id.nunique()} series, {len(d) // (1 + len(NO_MODEL_ARMS))} series-days\n")
        print(f"### Forecast inside −{BAND_UNDER:.0%}/+{BAND_OVER:.0%} of the actual value "
              f"({HEADLINE_QUANTILE} path)\n")
        show(band_table(d, model))
        print("### The same for repeat-yesterday\n")
        show(band_table(d, "snaive-1d"))
        for peak_name, window in WINDOWS.items():
            for pct in THRESHOLD_PERCENTILES:
                for fresh in (False, True):
                    scope = ("days the series was below the threshold the day before"
                             if fresh else "all days")
                    print(f"### {peak_name}, threshold at each series' p{pct}, {scope}\n")
                    show(rule_table(d, [model] + NO_MODEL_ARMS, pct, fresh, SWEEP, window))
        for cls in sorted(d.cls.unique()):
            print(f"### busiest hour, p90 threshold, fresh crossings only, "
                  f"{CLASS_GROUPS.get(cls, cls)}\n")
            show(rule_table(d[d.cls == cls], [model] + NO_MODEL_ARMS, 90, True,
                            ["q30", "q50", "q70", "q90"], WINDOWS["busiest hour"]))
    if len(sys.argv) > 2:
        peak_section(sys.argv[2])


if __name__ == "__main__":
    main()
