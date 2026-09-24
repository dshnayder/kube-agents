#!/usr/bin/env python3
"""8 hours ahead or 24? Give TimesFM the last 24 hours (or 7 days), let it forecast ahead, and
find the largest overshoot and undershoot against what actually happened.

    python3 window.py forecast --data series.jsonl.gz --forecaster http://localhost:8080 \
        [--forecaster http://localhost:8081] --context-days 1 --every-hours 2 --dump dump-1d/
    python3 window.py report dump-1d/ dump-7d/ > results/window.md

Forecasts start every few hours across the scored days, so no one time of day decides the
result. Each forecast runs 24 hours; its first 8 hours are the 8-hour forecast, because TimesFM
decodes forward and a longer horizon does not change the earlier steps. The report compares
dumps only on the (start time, series) pairs they all cover. Values are hourly means. Per
forecast window, "largest overshoot" is the highest (P - V) / V over its hours and "largest undershoot" the lowest; a typical day is the median window, a bad day the
worst tenth.
"""

import argparse
import concurrent.futures
import glob
import gzip
import json
import os

import numpy as np
import pandas as pd

from backtest import BATCH, DAY, FIRST_ORIGIN, HOUR, MAX_MISSING, fill, post
from pointwise import CLASSES, MIN_SHARE_OF_WEEK_MAX, coarsen

# Window name -> (hours, offset in hours from the start of the forecast). The last one is the
# 24-hour forecast's final 8 hours: the clock hours an 8-hour forecast made 16 hours later covers.
CASES = {"8 hours": (8, 0), "24 hours": (24, 0), "last 8 of 24 hours": (8, 16)}
FIRST_CASE = "8 hours"
WEEK = 7 * DAY
# Columns of a forecaster response: 0 is the mean, 5 the median.
Q50 = 5
TYPICAL, BAD = 50, 90
PERCENT = 100


def forecast(a):
    series = [json.loads(line) for line in gzip.open(a.data, "rt")]
    ys = [np.array([np.nan if v is None else v for v in s["values"]]) for s in series]
    os.makedirs(a.dump, exist_ok=True)
    n = len(ys[0])
    context = a.context_days * DAY
    origins = [t for t in range(FIRST_ORIGIN * DAY, n - DAY + 1, a.every_hours * HOUR)]
    pool = concurrent.futures.ThreadPoolExecutor(len(a.forecaster))
    for t in origins:
        path = os.path.join(a.dump, f"origin-{t:05d}.npz")
        if os.path.exists(path):
            continue
        live = [(s, y) for s, y in zip(series, ys)
                if np.isnan(y[t:t + DAY]).mean() <= MAX_MISSING
                and np.isnan(y[t - context:t]).mean() <= MAX_MISSING]
        if not live:
            continue
        inputs = [fill(y[t - context:t]) for _, y in live]
        chunks = [inputs[i:i + BATCH] for i in range(0, len(inputs), BATCH)]
        urls = [a.forecaster[i % len(a.forecaster)] for i in range(len(chunks))]
        results = list(pool.map(lambda c: post(c[0], c[1], horizon=DAY)[0], zip(urls, chunks)))
        np.savez_compressed(
            path, context_days=a.context_days, ids=np.array([s["id"] for s, _ in live]),
            classes=np.array([s["class"] for s, _ in live]),
            truth=np.stack([y[t:t + DAY] for _, y in live]),
            floor=np.array([MIN_SHARE_OF_WEEK_MAX * np.nanmax(y[t - WEEK:t]) for _, y in live]),
            pred=np.concatenate(results)[:, :, Q50])
        print(f"origin {t // DAY}d {t % DAY // HOUR:02d}h: {len(live)} series", flush=True)


def extremes(truth, pred, floor, hours, offset=0):
    """Largest overshoot and undershoot, as fractions of the actual value, over the window."""
    v = coarsen(truth, HOUR)[offset:offset + hours]
    p = coarsen(pred, HOUR)[offset:offset + hours]
    keep = ~np.isnan(v) & ~np.isnan(p) & (v > floor)
    if keep.sum() < hours // 2:
        return None
    err = (p[keep] - v[keep]) / v[keep]
    return max(err.max(), 0), min(err.min(), 0)


def fmt(x):
    return f"{PERCENT * x:+.0f}%"


def report(a):
    rows = []
    for directory in a.dumps:
        for path in sorted(glob.glob(os.path.join(directory, "origin-*.npz"))):
            z = np.load(path)
            days = int(z["context_days"]) if "context_days" in z.files else 1
            history = "24 hours of history" if days == 1 else f"{days} days of history"
            for k in range(len(z["ids"])):
                for case, (hours, offset) in CASES.items():
                    e = extremes(z["truth"][k], z["pred"][k], z["floor"][k], hours, offset)
                    if e is not None:
                        rows.append(dict(cls=str(z["classes"][k]), history=history, case=case,
                                         up=e[0], down=e[1], id=str(z["ids"][k]),
                                         origin=os.path.basename(path)))
    df = pd.DataFrame(rows)
    # Keep only the (start time, series) pairs every dump forecast, so the histories compare
    # on the same windows.
    df = df[df.groupby(["origin", "id"]).history.transform("nunique") == df.history.nunique()]

    def table(pctl, history, cases):
        out = {}
        for c in CLASSES:
            g = df[(df.cls == c) & (df.history == history)]
            row = {}
            for case in cases:
                h = g[g.case == case]
                row[f"{case}: up"] = fmt(np.percentile(h.up, pctl))
                row[f"{case}: down"] = fmt(np.percentile(h.down, PERCENT - pctl))
            out[CLASSES[c]] = row
        return pd.DataFrame(out).T

    windows = df[df.case == FIRST_CASE].drop_duplicates(["origin", "id"])
    print("# Largest miss: 8-hour against 24-hour forecasts\n")
    print("Generated by `harness/window.py`. Hourly values. `up` is the largest overshoot in a "
          "forecast window (forecast above actual), `down` the largest undershoot, each as a "
          f"share of the actual value. {windows.origin.nunique()} start times, "
          f"{windows.id.nunique()} series, {len(windows)} windows per history and horizon.\n")
    for history in dict.fromkeys(df.history):
        print(f"## {history.capitalize()}\n")
        for pctl, title in ((TYPICAL, "Typical window (median)"),
                            (BAD, "Bad window (worst 1 in 10)")):
            print(f"### {title}\n")
            print(table(pctl, history, list(CASES)).to_markdown())
            print()


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("forecast")
    f.add_argument("--data", required=True)
    f.add_argument("--forecaster", action="append", required=True)
    f.add_argument("--dump", required=True)
    f.add_argument("--context-days", type=int, default=1)
    f.add_argument("--every-hours", type=int, default=2)
    r = sub.add_parser("report")
    r.add_argument("dumps", nargs="+")
    a = ap.parse_args()
    forecast(a) if a.cmd == "forecast" else report(a)


if __name__ == "__main__":
    main()
