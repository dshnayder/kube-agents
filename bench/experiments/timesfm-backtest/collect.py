#!/usr/bin/env python3
"""Pull the backtest corpus from Cloud Monitoring into one gzipped JSON-lines file.

Every series is aligned to a fixed 5-minute grid over a fixed window that ends at a UTC
midnight, so a day is exactly 288 points and a rolling origin is an index, not a timestamp
search. Missing grid points are written as null; the backtest decides what to do with them.

Authentication is the caller's Application Default Credentials; the quota project is passed
explicitly because the eval-pool projects do not grant serviceusage to a reader.

    python3 collect.py --end 2026-09-23 --days 41 --out data/series.jsonl.gz
"""

import argparse
import concurrent.futures
import datetime as dt
import gzip
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

MONITORING_URL = "https://monitoring.googleapis.com/v3/projects/{project}/timeSeries"
QUOTA_PROJECT = "agentic-harness-demo"
STEP_SECONDS = 300
PAGE_SIZE = 100000
MAX_RETRIES = 5
RETRY_SLEEP_SECONDS = 5
REQUEST_TIMEOUT_SECONDS = 300
PARALLEL_TARGETS = 12
# Full-resolution retention for GKE system metrics is six weeks; points older than that are
# ten-minute downsamples and would leave every other 5-minute slot empty.
MAX_DAYS_AT_FULL_RESOLUTION = 42
MIN_COVERAGE = 0.9
# Most eval-pool clusters are younger than the window, so coverage is measured from a series'
# first point, and a series needs this many days from there to feed two weeks of origins.
MIN_SPAN_DAYS = 21
MIN_VOLUME_BYTES = 1 << 30
NODE_EPHEMERAL_BYTES = 50 << 30

# (project, cluster): the production install, and the eval pool's hosts, which every
# presubmit and nightly deploys into.
TARGETS = [("agentic-harness-demo", "kage-management")] + [
    (p, "platform-agent-host")
    for p in ["kube-agents-evals"] + [f"kube-agents-evals-{n}" for n in range(2, 12)]
]

# Namespaces whose containers are GKE's own plumbing, not a workload anyone sizes.
SYSTEM_NAMESPACES = {"kube-system", "gmp-system", "gke-managed-cim", "gke-managed-otel",
                     "gke-gmp-system", "gke-managed-system", "kube-node-lease", "gke-connect"}
TOKEN_VOLUME_MARKERS = ("kube-api-access", "token", "secret", "kubeconfig", "ssh", "config")

CONTAINER_GROUP = ["resource.label.namespace_name",
                   "metadata.system_labels.top_level_controller_name",
                   "resource.label.container_name"]
VOLUME_GROUP = ["resource.label.namespace_name",
                "metadata.system_labels.top_level_controller_name",
                "metric.label.volume_name"]
CLUSTER_GROUP = ["resource.label.cluster_name"]

# class -> query. `limit` is the reference series a threshold is read from, where one exists.
CLASSES = {
    "cluster_cpu": dict(
        metric="kubernetes.io/container/cpu/core_usage_time", aligner="ALIGN_RATE",
        reducer="REDUCE_SUM", group=CLUSTER_GROUP, unit="cores",
        limit=dict(metric="kubernetes.io/node/cpu/allocatable_cores", aligner="ALIGN_MEAN",
                   reducer="REDUCE_SUM", group=CLUSTER_GROUP)),
    "cpu_requested": dict(
        metric="kubernetes.io/container/cpu/request_cores", aligner="ALIGN_MEAN",
        reducer="REDUCE_SUM", group=CLUSTER_GROUP, unit="cores",
        limit=dict(metric="kubernetes.io/node/cpu/allocatable_cores", aligner="ALIGN_MEAN",
                   reducer="REDUCE_SUM", group=CLUSTER_GROUP)),
    "node_count": dict(
        metric="kubernetes.io/node/cpu/allocatable_cores", aligner="ALIGN_MEAN",
        reducer="REDUCE_COUNT", group=CLUSTER_GROUP, unit="nodes"),
    "container_memory": dict(
        metric="kubernetes.io/container/memory/used_bytes",
        extra='metric.label.memory_type="non-evictable"', aligner="ALIGN_MAX",
        reducer="REDUCE_MAX", group=CONTAINER_GROUP, unit="bytes",
        limit=dict(metric="kubernetes.io/container/memory/limit_bytes", aligner="ALIGN_MAX",
                   reducer="REDUCE_MAX", group=CONTAINER_GROUP)),
    "container_cpu": dict(
        metric="kubernetes.io/container/cpu/core_usage_time", aligner="ALIGN_RATE",
        reducer="REDUCE_SUM", group=CONTAINER_GROUP, unit="cores",
        limit=dict(metric="kubernetes.io/container/cpu/limit_cores", aligner="ALIGN_MAX",
                   reducer="REDUCE_SUM", group=CONTAINER_GROUP)),
    "volume_used": dict(
        metric="kubernetes.io/pod/volume/used_bytes", aligner="ALIGN_MAX",
        reducer="REDUCE_MAX", group=VOLUME_GROUP, unit="bytes",
        limit=dict(metric="kubernetes.io/pod/volume/total_bytes", aligner="ALIGN_MAX",
                   reducer="REDUCE_MAX", group=VOLUME_GROUP)),
}


_token = {"value": None, "at": 0.0}
_token_lock = threading.Lock()
TOKEN_TTL_SECONDS = 1800


def token():
    # One refresh at a time: concurrent gcloud invocations race on its credential files.
    with _token_lock:
        return _refresh_token()


def _refresh_token():
    if time.time() - _token["at"] > TOKEN_TTL_SECONDS:
        _token["value"] = subprocess.check_output(
            ["gcloud", "auth", "application-default", "print-access-token"], text=True).strip()
        _token["at"] = time.time()
    return _token["value"]


def rfc3339(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def list_series(project, cluster, q, start, end):
    flt = f'metric.type="{q["metric"]}" AND resource.label.cluster_name="{cluster}"'
    if q.get("extra"):
        flt += " AND " + q["extra"]
    params = [("filter", flt), ("interval.startTime", rfc3339(start)),
              ("interval.endTime", rfc3339(end)),
              ("aggregation.alignmentPeriod", f"{STEP_SECONDS}s"),
              ("aggregation.perSeriesAligner", q["aligner"]),
              ("aggregation.crossSeriesReducer", q["reducer"]),
              ("pageSize", str(PAGE_SIZE))]
    params += [("aggregation.groupByFields", g) for g in q["group"]]
    out, page = [], None
    while True:
        p = params + ([("pageToken", page)] if page else [])
        url = MONITORING_URL.format(project=project) + "?" + urllib.parse.urlencode(p)
        for attempt in range(MAX_RETRIES):
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {token()}", "x-goog-user-project": QUOTA_PROJECT})
            try:
                with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as r:
                    body = json.load(r)
                break
            except urllib.error.HTTPError as e:
                if e.code < 500 and e.code != 429:
                    raise
                time.sleep(RETRY_SLEEP_SECONDS * (attempt + 1))
            except (urllib.error.URLError, TimeoutError) as e:
                print(f"{project}/{cluster} retry {attempt}: {e}", file=sys.stderr, flush=True)
                time.sleep(RETRY_SLEEP_SECONDS * (attempt + 1))
        else:
            raise RuntimeError(f"gave up on {project}/{cluster} {q['metric']}")
        out += body.get("timeSeries", [])
        page = body.get("nextPageToken")
        if not page:
            return out


def key_of(ts, group):
    labels = {}
    for g in group:
        name = g.split(".")[-1]
        if g.startswith("resource.label."):
            labels[name] = ts["resource"]["labels"].get(name, "")
        elif g.startswith("metric.label."):
            labels[name] = ts["metric"].get("labels", {}).get(name, "")
        else:
            labels[name] = ts.get("metadata", {}).get("systemLabels", {}).get(name, "")
    return labels


def to_grid(ts, start, n):
    grid = [None] * n
    for p in ts["points"]:
        t = dt.datetime.strptime(p["interval"]["endTime"][:19], "%Y-%m-%dT%H:%M:%S")
        i = int((t.replace(tzinfo=dt.timezone.utc) - start).total_seconds() // STEP_SECONDS) - 1
        if 0 <= i < n:
            v = p["value"]
            grid[i] = float(v.get("doubleValue", v.get("int64Value", 0)))
    return grid


def keep(cls, labels, values, limit):
    first = next((i for i, v in enumerate(values) if v is not None), len(values))
    span = values[first:]
    present = [v for v in span if v is not None]
    if len(span) < MIN_SPAN_DAYS * 86400 // STEP_SECONDS:
        return "short_span"
    if len(present) < MIN_COVERAGE * len(span):
        return "short_coverage"
    if max(present) == min(present):
        return "constant"
    if labels.get("namespace_name") in SYSTEM_NAMESPACES:
        return "system_namespace"
    if cls == "volume_used":
        name = labels.get("volume_name", "")
        cap = max((v for v in (limit or []) if v is not None), default=0)
        if any(m in name for m in TOKEN_VOLUME_MARKERS):
            return "not_a_data_volume"
        if not MIN_VOLUME_BYTES <= cap < NODE_EPHEMERAL_BYTES:
            return "not_a_data_volume"
    return None


def collect_target(project, cluster, start, end, n):
    """Every class for one cluster: (JSON lines kept, drop reasons counted)."""
    lines, dropped = [], {}
    for cls, q in CLASSES.items():
        try:
            series = list_series(project, cluster, q, start, end)
            limits = {}
            if q.get("limit"):
                for ts in list_series(project, cluster, q["limit"], start, end):
                    limits[json.dumps(key_of(ts, q["group"]), sort_keys=True)] = \
                        to_grid(ts, start, n)
        except urllib.error.HTTPError as e:
            print(f"{project}/{cluster} {cls}: HTTP {e.code}", file=sys.stderr, flush=True)
            continue
        for ts in series:
            labels = key_of(ts, q["group"])
            values = to_grid(ts, start, n)
            limit = limits.get(json.dumps(labels, sort_keys=True))
            why = keep(cls, labels, values, limit)
            if why:
                dropped[why] = dropped.get(why, 0) + 1
                continue
            sid = "/".join([project, cluster, cls] +
                           [labels[k] for k in sorted(labels) if k != "cluster_name"])
            lines.append(json.dumps({
                "id": sid, "project": project, "cluster": cluster, "class": cls,
                "labels": labels, "unit": q["unit"], "start": rfc3339(start),
                "step_seconds": STEP_SECONDS, "values": values, "limit": limit}))
        print(f"{project}/{cluster} {cls}: {len(series)} listed", file=sys.stderr, flush=True)
    return lines, dropped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--end", required=True, help="UTC date the window ends at (midnight)")
    ap.add_argument("--days", type=int, default=41)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    if a.days > MAX_DAYS_AT_FULL_RESOLUTION:
        sys.exit(f"--days above {MAX_DAYS_AT_FULL_RESOLUTION} reaches downsampled data")
    end = dt.datetime.strptime(a.end, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    start = end - dt.timedelta(days=a.days)
    n = a.days * 86400 // STEP_SECONDS
    kept, dropped = 0, {}
    with concurrent.futures.ThreadPoolExecutor(PARALLEL_TARGETS) as pool, \
            gzip.open(a.out, "wt") as f:
        jobs = [pool.submit(collect_target, p, c, start, end, n) for p, c in TARGETS]
        for job in jobs:
            lines, why = job.result()
            for line in lines:
                f.write(line + "\n")
            kept += len(lines)
            for k, v in why.items():
                dropped[k] = dropped.get(k, 0) + v
    print(json.dumps({"kept": kept, "dropped": dropped, "start": rfc3339(start),
                      "end": rfc3339(end), "points_per_series": n}, indent=2))


if __name__ == "__main__":
    main()
