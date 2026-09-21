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
run and per model call, cache-read share, wall time, time to first tool call, and, for scoped
arms, how often the skill the model loaded was outside the injected working set (a miss the shelf
rescued).
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import statistics

SKILL_TOOL = "skill_view"
LIST_TOOL = "skills_list"
SEARCH_TOOL = "tool_search"
CONTROL_DOMAIN = "control"
SKILL_ARG = "name"


def _load_runs(run_dir: pathlib.Path) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(run_dir.glob("*.json"))]


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
    return round(statistics.mean(xs), 1) if xs else None


def _median(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 1) if xs else None


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
            cache_share.append(round(100.0 * cr / ((tin or 0) + cr), 1))
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
        "first_skill_gold_pct": round(100.0 * first_gold / probes, 1) if probes else None,
        "first_skill_acceptable_pct": round(100.0 * first_acc / probes, 1) if probes else None,
        "first_skill_wrong_pct": round(100.0 * first_wrong / probes, 1) if probes else None,
        "no_skill_loaded_pct": round(100.0 * first_none / probes, 1) if probes else None,
        "any_gold_loaded_pct": round(100.0 * any_gold / probes, 1) if probes else None,
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", nargs="+", required=True)
    ap.add_argument("--record", default=None)
    ap.add_argument("--scenarios", required=True)
    ap.add_argument("--grown", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    scenarios = {s["id"]: s for s in json.loads(pathlib.Path(args.scenarios).read_text(encoding="utf-8"))["scenarios"]}
    record = _record_by_session(pathlib.Path(args.record) if args.record else None)
    grown_labels = set(x for x in args.grown.split(",") if x)
    report = {}
    for run_dir in args.runs:
        p = pathlib.Path(run_dir)
        runs = _load_runs(p)
        report[p.name] = score_arm(runs, scenarios, record, p.name in grown_labels)
    if args.json:
        print(json.dumps(report, indent=1))
        return
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
