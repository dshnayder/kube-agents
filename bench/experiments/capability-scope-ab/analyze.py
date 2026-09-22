#!/usr/bin/env python3
"""Score the capability-scoping A/B from run files and the plugin's scoping record.

Inputs:
  --runs   one or more run directories written by run_ab.py (each is one arm × rung)
  --record the capability_scope.jsonl copied out of the agent pod (optional; adds per-call
           usage, time to first tool call, and working-set membership)
  --scenarios scenarios.json (gold and acceptable skills per probe)
  --grown  labels (comma-separated) that ran on the grown catalogue, so `acceptable_grown` applies

Per arm it reports: skill selection (first skill loaded is gold / acceptable / wrong / none),
any-gold-loaded, spurious loads on control probes, skill_view and tool_search counts, tokens per
run and per model call, cache-read share, wall time, time to first tool call, and, on every arm,
how often the skill the model loaded was outside the ranker's top-K working set (the shadow miss
rate on unscoped arms; the loads the shelf rescued on scoped ones).
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import csv
import math
import statistics

SKILL_TOOL = "skill_view"
LIST_TOOL = "skills_list"
SEARCH_TOOL = "tool_search"
CONTROL_DOMAIN = "control"
SKILL_ARG = "name"
CELL_RECORD_NAME = "capability_scope.jsonl"
GROWN_SUFFIX = "-grown"
Z_95 = 1.96
PCT_DECIMALS = 1
P_DECIMALS = 4
CSV_FIELDS = ("label", "scenario", "rep", "http_status", "incomplete", "first_skill", "skills_loaded", "tool_calls", "api_calls", "input_tokens", "cache_read_tokens", "output_tokens", "wall_seconds")


def _load_runs(run_dir: pathlib.Path) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(run_dir.glob("*-r[0-9]*.json"))]


def _first_skill(run: dict) -> str | None:
    for call in run.get("tool_calls") or []:
        if call.get("name") == SKILL_TOOL:
            args = call.get("arguments") or {}
            return str(args.get(SKILL_ARG)) if isinstance(args, dict) else None
    return None


def _skills_loaded(run: dict) -> list[str]:
    out = []
    for call in run.get("tool_calls") or []:
        if call.get("name") == SKILL_TOOL and isinstance(call.get("arguments"), dict):
            out.append(str(call["arguments"].get(SKILL_ARG)))
    return out


def _count(run: dict, tool: str) -> int:
    return sum(1 for c in run.get("tool_calls") or [] if c.get("name") == tool)


def _record_by_session(path: pathlib.Path | None) -> dict[str, list[dict]]:
    by: dict[str, list[dict]] = collections.defaultdict(list)
    if path is None or not path.exists():
        return by
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        by[str(ev.get("session_id") or "")].append(ev)
    return by


def _mean(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), PCT_DECIMALS) if xs else None


def _median(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), PCT_DECIMALS) if xs else None


def score_arm(runs: list[dict], scenarios: dict[str, dict], record: dict[str, list[dict]], grown: bool) -> dict:
    n = 0
    first_gold = first_acc = first_wrong = first_none = 0
    any_gold = 0
    control_runs = control_spurious = 0
    skill_views: list[int] = []
    searches: list[int] = []
    wall: list[float] = []
    tokens_in: list[float] = []
    tokens_out: list[float] = []
    cache_share: list[float] = []
    api_calls: list[float] = []
    first_call_prompt: list[float] = []
    ttft: list[float] = []
    loaded_outside_ws = loaded_total = 0
    errors = 0
    incomplete = 0
    per_scenario: dict[str, list[str]] = collections.defaultdict(list)
    for run in runs:
        sc = scenarios.get(run["scenario"])
        if sc is None:
            continue
        if run.get("http_status") == 0 or (run.get("http_status", 0) >= 400 and not run.get("session_id")):
            errors += 1
            continue
        n += 1
        if run.get("incomplete"):
            incomplete += 1
        gold = set(sc["gold"])
        acceptable = set(sc["acceptable"]) | (set(sc.get("acceptable_grown", [])) if grown else set())
        first = _first_skill(run)
        loaded = _skills_loaded(run)
        if sc["domain"] == CONTROL_DOMAIN:
            control_runs += 1
            if loaded:
                control_spurious += 1
            per_scenario[run["scenario"]].append(first or "-")
        else:
            if first is None:
                first_none += 1
            elif first in gold:
                first_gold += 1
            elif first in acceptable:
                first_acc += 1
            else:
                first_wrong += 1
            if any(s in gold for s in loaded):
                any_gold += 1
            per_scenario[run["scenario"]].append(first or "-")
        skill_views.append(len(loaded))
        searches.append(_count(run, SEARCH_TOOL))
        wall.append(run.get("wall_seconds"))
        sess = run.get("session") or {}
        if isinstance(sess.get("session"), dict):  # the API wraps the row in {"object": ..., "session": {...}}
            sess = sess["session"]
        tin = sess.get("input_tokens")
        tout = sess.get("output_tokens")
        cr = sess.get("cache_read_tokens") or 0
        if tin or cr:  # the session row keeps cached prompt tokens out of input_tokens
            tokens_in.append((tin or 0) + cr)
            cache_share.append(round(100.0 * cr / ((tin or 0) + cr), PCT_DECIMALS))
        if tout:
            tokens_out.append(tout)
        if sess.get("api_call_count"):
            api_calls.append(sess["api_call_count"])
        events = record.get(run.get("session_id") or "", [])
        apis = [e for e in events if e.get("event") == "api"]
        if apis:
            first_api = min(apis, key=lambda e: e.get("api_call_count") or 0)
            usage = first_api.get("usage") or {}
            if usage.get("prompt_tokens") or usage.get("input_tokens"):
                first_call_prompt.append(usage.get("prompt_tokens") or usage.get("input_tokens"))
        calls = [e for e in events if e.get("event") == "tool_call"]
        if calls:
            t0 = min((e.get("since_turn_start") for e in calls if e.get("since_turn_start") is not None), default=None)
            if t0 is not None:
                ttft.append(t0)
        for e in calls:
            if e.get("tool_name") == SKILL_TOOL and e.get("skill"):
                loaded_total += 1
                if e.get("skill_in_working_set") is False:
                    loaded_outside_ws += 1
    probes = n - control_runs
    return {
        "runs": n,
        "errors": errors,
        "incomplete_runs": incomplete,
        "probes": probes,
        "first_skill_gold_pct": round(100.0 * first_gold / probes, PCT_DECIMALS) if probes else None,
        "first_skill_acceptable_pct": round(100.0 * first_acc / probes, PCT_DECIMALS) if probes else None,
        "first_skill_wrong_pct": round(100.0 * first_wrong / probes, PCT_DECIMALS) if probes else None,
        "no_skill_loaded_pct": round(100.0 * first_none / probes, PCT_DECIMALS) if probes else None,
        "any_gold_loaded_pct": round(100.0 * any_gold / probes, PCT_DECIMALS) if probes else None,
        "control_spurious_loads": f"{control_spurious}/{control_runs}",
        "skill_views_per_run": _mean(skill_views),
        "tool_searches_per_run": _mean(searches),
        "api_calls_per_run": _mean(api_calls),
        "input_tokens_per_run_median": _median(tokens_in),
        "output_tokens_per_run_median": _median(tokens_out),
        "cache_read_share_pct_mean": _mean(cache_share),
        "first_call_prompt_tokens_median": _median(first_call_prompt),
        "wall_seconds_median": _median(wall),
        "time_to_first_tool_call_median": _median(ttft),
        "skill_loads_outside_working_set": f"{loaded_outside_ws}/{loaded_total}",
        "first_skill_by_scenario": dict(per_scenario),
    }


def wilson(k: int, n: int) -> tuple[float, float]:
    """95% Wilson interval for a proportion, as percentages."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + Z_95 ** 2 / n
    centre = (p + Z_95 ** 2 / (2 * n)) / denom
    half = Z_95 * math.sqrt(p * (1 - p) / n + Z_95 ** 2 / (4 * n * n)) / denom
    return (round(100 * (centre - half), PCT_DECIMALS), round(100 * (centre + half), PCT_DECIMALS))


def two_proportion_p(k1: int, n1: int, k2: int, n2: int) -> float:
    """Two-sided p-value of a pooled two-proportion z-test (normal approximation)."""
    if min(n1, n2) == 0:
        return 1.0
    p1, p2, p = k1 / n1, k2 / n2, (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return 1.0
    z = abs(p1 - p2) / se
    return round(2 * (1 - 0.5 * (1 + math.erf(z / math.sqrt(2)))), P_DECIMALS)


def write_csv(run_dirs: list[pathlib.Path], out: pathlib.Path) -> None:
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        w.writeheader()
        for d in run_dirs:
            for run in _load_runs(d):
                sess = run.get("session") or {}
                if isinstance(sess.get("session"), dict):
                    sess = sess["session"]
                w.writerow({
                    "label": run.get("label"), "scenario": run.get("scenario"), "rep": run.get("rep"),
                    "http_status": run.get("http_status"), "incomplete": run.get("incomplete"),
                    "first_skill": _first_skill(run) or "", "skills_loaded": " ".join(_skills_loaded(run)),
                    "tool_calls": " ".join(str(c.get("name")) for c in run.get("tool_calls") or []),
                    "api_calls": sess.get("api_call_count"), "input_tokens": sess.get("input_tokens"),
                    "cache_read_tokens": sess.get("cache_read_tokens"), "output_tokens": sess.get("output_tokens"),
                    "wall_seconds": run.get("wall_seconds"),
                })


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", nargs="+", required=True)
    ap.add_argument("--record", default=None)
    ap.add_argument("--scenarios", required=True)
    ap.add_argument("--grown", default="")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--csv", default=None, help="write one row per run to this file")
    ap.add_argument("--compare", nargs=2, metavar=("BASELINE", "TREATMENT"), help="report intervals and a p-value for first-skill-gold")
    args = ap.parse_args()
    scenarios = {s["id"]: s for s in json.loads(pathlib.Path(args.scenarios).read_text(encoding="utf-8"))["scenarios"]}
    record = _record_by_session(pathlib.Path(args.record) if args.record else None)
    grown_labels = set(x for x in args.grown.split(",") if x)
    report = {}
    for run_dir in args.runs:
        p = pathlib.Path(run_dir)
        runs = _load_runs(p)
        cell_record = record or _record_by_session(p / CELL_RECORD_NAME)  # run_matrix.sh drops it beside the runs
        report[p.name] = score_arm(runs, scenarios, cell_record, p.name in grown_labels or p.name.endswith(GROWN_SUFFIX))
    if args.csv:
        write_csv([pathlib.Path(r) for r in args.runs], pathlib.Path(args.csv))
    if args.json:
        print(json.dumps(report, indent=1))
        return
    if args.compare:
        base, treat = (report[x] for x in args.compare)
        for key in ("first_skill_gold_pct", "any_gold_loaded_pct", "no_skill_loaded_pct"):
            kb = round(base[key] * base["probes"] / 100); kt = round(treat[key] * treat["probes"] / 100)
            print(f"{key}: {args.compare[0]} {base[key]}% {wilson(kb, base['probes'])} vs {args.compare[1]} {treat[key]}% {wilson(kt, treat['probes'])}; p={two_proportion_p(kb, base['probes'], kt, treat['probes'])}")
        print()
    keys = [k for k in next(iter(report.values())).keys() if k != "first_skill_by_scenario"] if report else []
    arms = list(report.keys())
    print("| metric | " + " | ".join(arms) + " |")
    print("| --- | " + " | ".join("---" for _ in arms) + " |")
    for k in keys:
        print(f"| {k} | " + " | ".join(str(report[a][k]) for a in arms) + " |")
    print()
    for a in arms:
        print(f"### {a}: first skill loaded per scenario")
        for sid, firsts in sorted(report[a]["first_skill_by_scenario"].items()):
            print(f"- {sid}: {', '.join(firsts)}")


if __name__ == "__main__":
    main()
