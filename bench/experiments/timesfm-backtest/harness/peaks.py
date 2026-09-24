#!/usr/bin/env python3
"""Forecast tomorrow's peak directly: TimesFM reads the series of daily peaks up to the origin
and forecasts one step. A per-step forecast smooths a burst whose timing is uncertain, so the
maximum of its path understates the day's peak; forecasting the peak series asks the model the
question the threshold decision asks.

    python3 peaks.py --data series.jsonl.gz --forecaster http://localhost:8080 \
        --out results/peaks.csv.gz

Two kinds of peak: the highest 5-minute point, and the highest hourly mean. A day missing more
than MAX_MISSING of its points is a gap in the peak series, interpolated in the context and
never scored.
"""

import argparse
import csv
import gzip
import io
import json

import numpy as np

from backtest import DAY, FIRST_ORIGIN, MAX_MISSING, QUANTILES, fill, post, write_bytes

HOUR = 12
# The peak series needs this many real days behind an origin before it is forecast.
MIN_CONTEXT_DAYS = 7
BATCH = 64
KINDS = {"5-minute peak": 1, "busiest hour": HOUR}
# The no-model rules read the last week of daily peaks.
BASELINE_DAYS = 7
FIELDS = (["id", "class", "origin", "kind", "context_days", "actual", "yesterday", "max7",
           "trend7"]
          + [f"q{int(q * 100)}" for q in QUANTILES])


def daily_peaks(y, window):
    """Per day, the highest mean over `window` consecutive points; NaN for a day too sparse."""
    days = y.reshape(-1, DAY)
    out = np.full(len(days), np.nan)
    for i, day in enumerate(days):
        if np.isnan(day).mean() > MAX_MISSING:
            continue
        with np.errstate(all="ignore"):
            out[i] = np.nanmax(np.nanmean(day.reshape(-1, window), axis=1))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--forecaster", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    series = [json.loads(line) for line in gzip.open(a.data, "rt")]
    rows = []
    for kind, window in KINDS.items():
        peaks = []
        for s in series:
            y = np.array([np.nan if v is None else v for v in s["values"]], dtype=np.float64)
            peaks.append(daily_peaks(y, window))
        days = len(peaks[0])
        for d in range(FIRST_ORIGIN, days):
            jobs = []
            for s, p in zip(series, peaks):
                hist = p[:d]
                first = np.flatnonzero(~np.isnan(hist))
                if np.isnan(p[d]) or len(first) == 0:
                    continue
                hist = hist[first[0]:]
                if (~np.isnan(hist)).sum() < MIN_CONTEXT_DAYS or np.isnan(hist[-1]):
                    continue
                jobs.append((s, p[d], hist[-1], fill(hist)))
            for i in range(0, len(jobs), BATCH):
                chunk = jobs[i:i + BATCH]
                quant, _, _ = post(a.forecaster, [c[3] for c in chunk], horizon=1)
                for (s, actual, yesterday, ctx), q in zip(chunk, quant):
                    week = ctx[-BASELINE_DAYS:]
                    slope, intercept = np.polyfit(np.arange(len(week)), week, 1)
                    rows.append(dict(id=s["id"], origin=d, kind=kind, context_days=len(ctx),
                                     actual=actual, yesterday=yesterday, max7=week.max(),
                                     trend7=intercept + slope * len(week),
                                     **{"class": s["class"]},
                                     **{f"q{int(v * 100)}": q[0, k + 1]
                                        for k, v in enumerate(QUANTILES)}))
            print(f"{kind} origin {d}: {len(jobs)} series", flush=True)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS)
    w.writeheader()
    w.writerows(rows)
    write_bytes(a.out, gzip.compress(buf.getvalue().encode()))
    print(f"wrote {len(rows)} rows to {a.out}", flush=True)


if __name__ == "__main__":
    main()
