#!/usr/bin/env python3
"""Rolling-origin backtest: forecast day T from the days before it, score against day T.

Every origin is a UTC midnight on the collector's 5-minute grid, so day d is the 288 points
[d*288, (d+1)*288). An arm sees only points before its origin. TimesFM arms go to the
forecaster service; baselines are computed here. One CSV row per (series, origin, arm).

    python3 backtest.py --data gs://BUCKET/series.jsonl.gz --forecaster http://forecaster:8080 \
        --out gs://BUCKET/results/RUN
"""

import argparse
import concurrent.futures
import csv
import gzip
import io
import json
import os
import sys
import time
import urllib.request

import numpy as np

DAY = 288
HORIZON = DAY
HOUR = 12
BATCH = 64
# Series whose truth day or context is missing more than this are not scored for that origin.
MAX_MISSING = 0.1
# Baselines read their residuals, and MASE its scale, from this much history.
RESIDUAL_DAYS = 7
# The first origin every arm but tfm-28d and the ensemble can score: snaive-7d needs a week
# of lag-7 residuals behind a week of lag.
FIRST_ORIGIN = 8
# A "new high" is a day whose peak clears the context week's max by this share of its range.
NEW_HIGH_MARGIN = 0.1
LIMIT_SHARE = 0.9
QUANTILES = np.arange(1, 10) / 10
Q10, Q50, Q90 = 1, 5, 9
REQUEST_TIMEOUT_SECONDS = 900
MAX_RETRIES = 5
RETRY_SLEEP_SECONDS = 10

TFM_ARMS = {"tfm-1d": 1, "tfm-7d": 7, "tfm-28d": 28}
ENSEMBLE = "tfm-ens"
FIELDS = ["id", "cluster", "class", "origin", "arm", "n", "mae", "scale", "mase", "mase_1h",
          "wql_num", "abs_sum", "cov80", "truth_max", "pred_max", "pred_max_q90", "ctx_max", "ctx_min",
          "new_high", "pred_new_high", "limit", "limit_hit", "pred_limit_hit"]


def read_bytes(path):
    if path.startswith("gs://"):
        from google.cloud import storage
        bucket, _, name = path[5:].partition("/")
        return storage.Client().bucket(bucket).blob(name).download_as_bytes()
    with open(path, "rb") as f:
        return f.read()


def write_bytes(path, data):
    if path.startswith("gs://"):
        from google.cloud import storage
        bucket, _, name = path[5:].partition("/")
        storage.Client().bucket(bucket).blob(name).upload_from_string(data)
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def fill(x):
    """Linear interpolation inside, nearest value at the edges. TimesFM takes no NaNs."""
    x = x.copy()
    bad = np.isnan(x)
    if bad.all():
        return None
    idx = np.arange(len(x))
    x[bad] = np.interp(idx[bad], idx[~bad], x[~bad])
    return x


def post(url, inputs):
    body = json.dumps({"inputs": [x.tolist() for x in inputs], "horizon": HORIZON}).encode()
    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(url + "/forecast", data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as r:
                out = json.load(r)
            return np.asarray(out["quantiles"], dtype=np.float32), out["seconds"], out["bucket"]
        except Exception as e:  # noqa: BLE001 - a restarted forecaster pod is worth a retry
            print(f"forecast retry {attempt}: {e}", file=sys.stderr)
            time.sleep(RETRY_SLEEP_SECONDS * (attempt + 1))
    raise RuntimeError("forecaster unreachable")


def baseline(ctx, arm):
    """Point forecast plus empirical quantiles of the same method's in-sample residuals."""
    recent = ctx[-RESIDUAL_DAYS * DAY:]
    if arm == "snaive-1d":
        point = ctx[-DAY:]
        resid = recent[DAY:] - recent[:-DAY]
    elif arm == "snaive-7d":
        point = ctx[-7 * DAY:-6 * DAY]
        span = ctx[-(RESIDUAL_DAYS + 7) * DAY:]
        resid = span[7 * DAY:] - span[:-7 * DAY]
    else:  # linear-7d
        t = np.arange(len(recent))
        b, a = np.polyfit(t, recent, 1)
        point = a + b * (len(recent) + np.arange(HORIZON))
        resid = recent - (a + b * t)
    q = point[:, None] + np.quantile(resid, QUANTILES)[None, :]
    return np.concatenate([point[:, None], q], axis=1)


def score(s, d, arm, quant, truth, ctx, limit):
    ok = ~np.isnan(truth)
    point = quant[:, Q50]
    err = np.abs(point - truth)
    recent = ctx[-RESIDUAL_DAYS * DAY:]
    scale = float(np.mean(np.abs(recent[DAY:] - recent[:-DAY])))
    mae = float(err[ok].mean())
    mae_1h = float(err[:HOUR][ok[:HOUR]].mean()) if ok[:HOUR].any() else float("nan")
    y = truth[ok]
    pin = []
    for i, q in enumerate(QUANTILES):
        diff = y - quant[ok, i + 1]
        pin.append(2 * np.sum(np.maximum(q * diff, (q - 1) * diff)))
    ctx_max, ctx_min = float(recent.max()), float(recent.min())
    threshold = ctx_max + NEW_HIGH_MARGIN * (ctx_max - ctx_min)
    truth_max, pred_max, pred_max_q90 = float(y.max()), float(point.max()), float(quant[:, Q90].max())
    row = dict(id=s["id"], cluster=s["cluster"], **{"class": s["class"]}, origin=d, arm=arm,
               n=int(ok.sum()), mae=mae, scale=scale,
               mase=mae / scale if scale > 0 else float("nan"),
               mase_1h=mae_1h / scale if scale > 0 else float("nan"),
               wql_num=float(np.mean(pin)), abs_sum=float(np.abs(y).sum()),
               cov80=float(np.mean((y >= quant[ok, Q10]) & (y <= quant[ok, Q90]))),
               truth_max=truth_max, pred_max=pred_max, pred_max_q90=pred_max_q90,
               ctx_max=ctx_max, ctx_min=ctx_min, new_high=int(truth_max > threshold),
               pred_new_high=int(pred_max_q90 > threshold), limit="", limit_hit="",
               pred_limit_hit="")
    if limit is not None:
        lim = np.nanmax(limit[:(d + 1) * DAY]) if not np.isnan(limit[:(d + 1) * DAY]).all() else 0
        if lim > 0:
            row.update(limit=float(lim), limit_hit=int(truth_max >= LIMIT_SHARE * lim),
                       pred_limit_hit=int(pred_max_q90 >= LIMIT_SHARE * lim))
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--forecaster", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=1,
                    help="concurrent requests; one per forecaster replica")
    ap.add_argument("--limit-series", type=int, default=0, help="smoke runs only")
    a = ap.parse_args()

    series = [json.loads(line) for line in gzip.decompress(read_bytes(a.data)).decode().splitlines()]
    if a.limit_series:
        series = series[:a.limit_series]
    for s in series:
        s["y"] = np.array([np.nan if v is None else v for v in s["values"]], dtype=np.float64)
        s["lim"] = None if s["limit"] is None else np.array(
            [np.nan if v is None else v for v in s["limit"]], dtype=np.float64)
        del s["values"], s["limit"]
    days = len(series[0]["y"]) // DAY
    print(f"{len(series)} series, {days} days, origins {FIRST_ORIGIN}..{days - 1}", flush=True)

    rows, latency = [], []
    pool = concurrent.futures.ThreadPoolExecutor(a.workers)
    for d in range(FIRST_ORIGIN, days):
        t0 = time.time()
        live = []
        for s in series:
            truth = s["y"][d * DAY:(d + 1) * DAY]
            hist = s["y"][:d * DAY]
            if np.isnan(truth).mean() > MAX_MISSING:
                continue
            if np.isnan(hist[-(RESIDUAL_DAYS + 7) * DAY:]).mean() > MAX_MISSING:
                continue
            ctx = fill(hist)
            if ctx is None:
                continue
            live.append((s, truth, ctx))
        # forecasts[arm][i] for the series at live[i]; an arm skips a series whose context
        # window is not real data (a cluster younger than the window), so nothing is scored on
        # an interpolated history.
        forecasts = {}
        for arm, ctx_days in TFM_ARMS.items():
            if d < ctx_days:
                continue
            members = [i for i, (s, _, _) in enumerate(live)
                       if np.isnan(s["y"][(d - ctx_days) * DAY:d * DAY]).mean() <= MAX_MISSING]
            if not members:
                continue
            inputs = [live[i][2][-ctx_days * DAY:] for i in members]
            chunks = [inputs[i:i + BATCH] for i in range(0, len(inputs), BATCH)]
            results = list(pool.map(lambda c: post(a.forecaster, c), chunks))
            forecasts[arm] = dict(zip(members, np.concatenate([r[0] for r in results])))
            for (chunk, (_, secs, bucket)) in zip(chunks, results):
                latency.append(dict(arm=arm, origin=d, batch=len(chunk), bucket=bucket,
                                    seconds=secs))
        if all(arm in forecasts for arm in TFM_ARMS):
            forecasts[ENSEMBLE] = {i: np.mean([forecasts[arm][i] for arm in TFM_ARMS], axis=0)
                                   for i in forecasts["tfm-28d"]
                                   if all(i in forecasts[arm] for arm in TFM_ARMS)}
        for i, (s, truth, ctx) in enumerate(live):
            for arm in ("snaive-1d", "snaive-7d", "linear-7d"):
                rows.append(score(s, d, arm, baseline(ctx, arm), truth, ctx, s["lim"]))
            for arm, f in forecasts.items():
                if i in f:
                    rows.append(score(s, d, arm, f[i], truth, ctx, s["lim"]))
        print(f"origin {d}: {len(live)} series, arms {sorted(forecasts)}, "
              f"{time.time() - t0:.0f}s", flush=True)

    for name, data, fields in (("metrics.csv.gz", rows, FIELDS),
                               ("latency.csv.gz", latency, ["arm", "origin", "batch", "bucket",
                                                            "seconds"])):
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=fields)
        w.writeheader()
        w.writerows(data)
        write_bytes(a.out.rstrip("/") + "/" + name, gzip.compress(buf.getvalue().encode()))
    print(f"wrote {len(rows)} rows to {a.out}", flush=True)


if __name__ == "__main__":
    main()
