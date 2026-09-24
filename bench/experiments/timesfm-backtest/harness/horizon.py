#!/usr/bin/env python3
"""Does a shorter horizon help? Forecast 8 hours ahead from three origins a day (00, 08 and 16
UTC) and compare each value with the 24-hour forecast made at midnight for the same hours.

    python3 horizon.py forecast --data series.jsonl.gz --forecaster http://localhost:8080 \
        --dump dump-8h/
    python3 horizon.py report dump-8h/ dump/ > results/horizon.md

Comparing on the same clock hours keeps time of day out of it: the first 8 hours of a midnight
forecast are always the night, which may be easier to forecast than the afternoon. The series
and days eligible are the ones backtest.py scores, and the band is pointwise.py's.
"""

import argparse
import glob
import gzip
import json
import os
import sys

import numpy as np
import pandas as pd

from backtest import BATCH, DAY, FIRST_ORIGIN, HOUR, MAX_MISSING, RESIDUAL_DAYS, fill, post
from pointwise import CLASSES, MIN_SHARE_OF_WEEK_MAX, coarsen, pct, present, split
from scope import included

HORIZON_HOURS = 8
STEPS = HORIZON_HOURS * HOUR
ORIGIN_HOURS = (0, 8, 16)
CONTEXT_DAYS = 7
# Columns of the 24-hour dump: 0 is the mean, 5 the median.
Q50 = 5
LONG_ARM = "tfm-7d"


def forecast(a):
    series = [json.loads(line) for line in gzip.open(a.data, "rt")]
    os.makedirs(a.dump, exist_ok=True)
    days = len(series[0]["values"]) // DAY
    ys = [np.array([np.nan if v is None else v for v in s["values"]]) for s in series]
    for d in range(FIRST_ORIGIN, days):
        for hour in ORIGIN_HOURS:
            t = d * DAY + hour * HOUR
            live = []
            for s, y in zip(series, ys):
                truth, hist = y[t:t + STEPS], y[:t]
                # The same eligibility as backtest.py at midnight, so the two compare like for
                # like: a mostly real truth window, two weeks of mostly real history, and a real
                # week for the 7-day context.
                if (np.isnan(truth).mean() > MAX_MISSING
                        or np.isnan(y[d * DAY:(d + 1) * DAY]).mean() > MAX_MISSING
                        or np.isnan(hist[-(RESIDUAL_DAYS + 7) * DAY:]).mean() > MAX_MISSING
                        or np.isnan(hist[-CONTEXT_DAYS * DAY:]).mean() > MAX_MISSING):
                    continue
                ctx = fill(hist)
                if ctx is not None:
                    live.append((s, truth, ctx))
            if not live:
                continue
            quant = []
            for i in range(0, len(live), BATCH):
                chunk = [c[-CONTEXT_DAYS * DAY:] for _, _, c in live[i:i + BATCH]]
                quant.append(post(a.forecaster, chunk, horizon=STEPS)[0])
            quant = np.concatenate(quant)
            snaive = []
            for _, _, ctx in live:
                recent = ctx[-RESIDUAL_DAYS * DAY:]
                snaive.append(ctx[-DAY:-DAY + STEPS] + np.median(recent[DAY:] - recent[:-DAY]))
            np.savez_compressed(
                os.path.join(a.dump, f"origin-{d:02d}-{hour:02d}.npz"),
                ids=np.array([s["id"] for s, _, _ in live]),
                classes=np.array([s["class"] for s, _, _ in live]),
                truth=np.stack([t for _, t, _ in live]),
                floor=np.array([MIN_SHARE_OF_WEEK_MAX * c[-RESIDUAL_DAYS * DAY:].max()
                                for _, _, c in live]),
                tfm=quant[:, :, Q50], snaive=np.stack(snaive))
            print(f"origin {d} {hour:02d}:00: {len(live)} series", flush=True)


def errors(truth, pred, floor):
    v, p = coarsen(truth, HOUR), coarsen(pred, HOUR)
    keep = ~np.isnan(v) & ~np.isnan(p) & (v > floor)
    return (p[keep] - v[keep]) / v[keep]


def report(a):
    rows = []
    for path in sorted(glob.glob(os.path.join(a.short, "origin-*-*.npz"))):
        d, hour = map(int, os.path.basename(path)[7:12].split("-"))
        z = np.load(path)
        longpath = os.path.join(a.long, f"origin-{d:02d}.npz")
        if not os.path.exists(longpath):
            continue
        lz = np.load(longpath)
        index = {str(i): k for k, i in enumerate(lz["ids"])}
        for k, sid in enumerate(z["ids"].astype(str)):
            j = index.get(sid)
            if not included(sid) or j is None or np.isnan(lz[LONG_ARM][j, :, Q50]).all():
                continue
            long24 = lz[LONG_ARM][j, hour * HOUR:hour * HOUR + STEPS, Q50]
            for method, pred in (("8-hour forecast", z["tfm"][k]),
                                 ("24-hour forecast", long24),
                                 ("repeat yesterday", z["snaive"][k])):
                err = errors(z["truth"][k], pred, z["floor"][k])
                rows.append(pd.DataFrame({"err": err, "method": method, "hour": hour,
                                          "cls": str(z["classes"][k]), "id": sid}))
    df = pd.concat(rows, ignore_index=True)
    methods = ["8-hour forecast", "24-hour forecast", "repeat yesterday"]

    def table(d):
        out = {}
        for m in methods:
            g = d[d.method == m]
            col = {CLASSES[c]: "{} / {} / {}".format(*map(pct, split(g[g.cls == c].err)))
                   for c in present(g)}
            col["all"] = "{} / {} / {}".format(*map(pct, split(g.err)))
            out[m] = col
        return pd.DataFrame(out)

    print("# Forecast horizon: 8 hours against 24 hours\n")
    print("Generated by `harness/horizon.py`. Hourly values; each cell is in band / more than 5% "
          "too high / more than 10% too low. The 24-hour column is the midnight forecast scored "
          "on the same hours, so both columns cover the same series, days and clock hours. "
          f"{df[df.method == methods[0]].id.nunique()} series.\n")
    print("## All three 8-hour windows\n")
    print(table(df).to_markdown())
    print()
    for hour in ORIGIN_HOURS:
        print(f"## {hour:02d}:00-{hour + HORIZON_HOURS:02d}:00 UTC\n")
        print(table(df[df.hour == hour]).to_markdown())
        print()


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("forecast")
    f.add_argument("--data", required=True)
    f.add_argument("--forecaster", required=True)
    f.add_argument("--dump", required=True)
    r = sub.add_parser("report")
    r.add_argument("short")
    r.add_argument("long")
    a = ap.parse_args()
    forecast(a) if a.cmd == "forecast" else report(a)


if __name__ == "__main__":
    sys.exit(main())
