#!/usr/bin/env python3
"""Build the comparison tables from the saved runs.

Two tables, because the experiment has two halves that answer different
questions. The agent table is the experiment: same prompt, same model,
different access design, scored on what the agent said and which route it took
to say it. The ladder table is the control: what each design could have put in
front of a model, with no model involved. Reading them together is what
separates "the design cannot express this" from "the design can express it and
the agent still did not get there" -- and, in the other direction, "the ladder
says impossible and the agent answered anyway", which is a route the design did
not intend.

Usage:
    compare.py --results results --out results/comparison.md
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from pathlib import Path

ARMS = {"A": "shared volume, git + gh", "B": "content passing", "C": "VCS verbs"}
# The mechanism each arm is designed to be answered on. inspect-repository is
# the sanctioned door in arms A and B -- in A it ends in a real checkout, in B
# the skill IS the content protocol and nothing else exists. Arm C replaces the
# door: version-control is present and inspect-repository is taken away, so its
# intended route is the one `vcs.py` leaves in the log.
INTENDED = {"A": "git", "B": "skill", "C": "vcs"}


def load(directory: Path, pattern: str) -> list[dict]:
    """Every saved run, with the rung relabelled from the filename where it lies.

    A write run was launched with `--rung 200` because the corpus it writes to
    is the 200-file one, so its `rung` field says 200 and it sorted into the
    read row beside the real 200 rung. The set a run belongs to is in its name.
    """
    out = []
    for path in sorted(directory.glob(pattern)):
        if "smoke" in path.name:
            continue
        run = json.loads(path.read_text())
        if path.stem.endswith("-write"):
            run["rung"] = "write"
        out.append(run)
    return out


def _order(run: dict) -> tuple:
    """Arm, then rung, with the write set last. `rung` is an int or "write"."""
    rung = run["rung"]
    return (run["arm"], 1, 0) if isinstance(rung, str) else (run["arm"], 0, rung)


def median(values: list[float]) -> float:
    return round(statistics.median(values), 1) if values else 0.0


def agent_table(runs: list[dict]) -> list[str]:
    lines = [
        "| arm | rung | answered | contested | history | fidelity | negative | "
        "stayed on route | opened a PR | left for `gh api` | median s | median turns |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for run in sorted(runs, key=_order):
        rows = [r for r in run["rows"] if "error" not in r]
        if not rows:
            continue
        arm = run["arm"]
        by_class: dict[str, list[dict]] = {}
        for row in rows:
            by_class.setdefault(row["class"], []).append(row)

        def share(name: str) -> str:
            group = by_class.get(name, [])
            if not group:
                return "-"
            return f"{sum(1 for r in group if r['answered'])}/{len(group)}"

        intended = INTENDED[arm]
        on_route = sum(1 for r in rows if intended in r.get("routes_used", []))
        proposed = sum(1 for r in rows if "propose" in r.get("routes_used", []))
        escaped = sum(1 for r in rows if "ghapi" in r.get("routes_used", []))
        lines.append(
            f"| {arm} | {run['rung']} | "
            f"{sum(1 for r in rows if r['answered'])}/{len(rows)} | "
            f"{share('contested')} | {share('history')} | {share('fidelity')} | "
            f"{share('negative')} | {on_route}/{len(rows)} | {proposed}/{len(rows)} | "
            f"{escaped}/{len(rows)} | "
            f"{median([r['seconds'] for r in rows])} | "
            f"{median([r['turns'] for r in rows])} |"
        )
    return lines


def ladder_table(runs: list[dict]) -> list[str]:
    lines = [
        "| arm | rung | mean reach | contamination | context bytes | "
        "not expressible |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for run in sorted(runs, key=_order):
        rows = [r for r in run["rows"] if "error" not in r]
        if not rows:
            continue
        reach = [r.get("reach", 0.0) for r in rows]
        contaminated = sum(1 for r in rows if r.get("contamination", 0))
        blocked = sum(1 for r in rows if r.get("notes"))
        lines.append(
            f"| {run['arm']} | {run['rung']} | "
            f"{round(sum(reach) / len(reach), 3)} | "
            f"{contaminated}/{len(rows)} | "
            f"{sum(r.get('context_bytes', 0) for r in rows):,} | "
            f"{blocked}/{len(rows)} |"
        )
    return lines


def disagreements(agent_runs: list[dict], ladder_runs: list[dict]) -> list[str]:
    """Probes where the two halves of the experiment do not agree.

    The interesting cell is `ladder says no, agent says yes`: the design could
    not express the probe, and the agent answered it regardless by leaving the
    protocol. That is invisible to a prompt-free measurement and it is the
    reason this experiment needs a model in it.
    """
    ladder_by = {}
    for run in ladder_runs:
        # The directory ladder is arm A's ceiling. The content ladder is arm
        # B's, and it is also the floor arm C has to beat: C's whole claim is
        # that the six probes the content protocol cannot express are the ones
        # it turns into verbs, so scoring C against the same control is the
        # comparison, not a shortcut.
        arms = ("A",) if run["arm"] == "directory" else ("B", "C")
        for arm in arms:
            for row in run["rows"]:
                ladder_by[(arm, run["rung"], row["probe"])] = row
    lines = [
        "| probe | arm | rung | ladder | agent | route the agent used |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    found = False
    for run in agent_runs:
        for row in run["rows"]:
            if "error" in row:
                continue
            control = ladder_by.get((run["arm"], run["rung"], row["probe"]))
            if not control:
                continue
            blocked = bool(control.get("notes"))
            if blocked == (not row["answered"]):
                continue
            found = True
            lines.append(
                f"| {row['probe']} | {run['arm']} | {run['rung']} | "
                f"{'not expressible' if blocked else 'expressible'} | "
                f"{'answered' if row['answered'] else 'missed'} | "
                f"{', '.join(row.get('routes_used', [])) or 'none'} |"
            )
    return lines if found else ["(none)"]


def route_detail(runs: list[dict]) -> list[str]:
    lines = ["| arm | rung | workspace calls | vcs calls | git calls | "
             "`gh pr create` | gh api calls |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for run in sorted(runs, key=_order):
        totals: Counter[str] = Counter()
        for row in run["rows"]:
            for name, count in (row.get("route") or {}).items():
                totals[name] += count
        lines.append(
            f"| {run['arm']} | {run['rung']} | {totals['workspace_http']} | "
            f"{totals['vcs']} | {totals['git']} | {totals['propose']} | "
            f"{totals['ghapi']} |"
        )
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=Path("results"))
    parser.add_argument("--out", type=Path, default=Path("results/comparison.md"))
    arguments = parser.parse_args()

    agent_runs = load(arguments.results, "agent-*.json")
    ladder_runs = load(arguments.results, "content-r*.json") + load(
        arguments.results, "directory-r*.json"
    )
    for run in ladder_runs:
        run.setdefault("arm", "?")

    out = ["# git access experiment: results", ""]
    out += ["## The experiment: same prompt, different access design", ""]
    out += agent_table(agent_runs) + [""]
    out += ["## Which route the agent actually took", ""]
    out += route_detail(agent_runs) + [""]
    out += ["## Where the control and the agent disagree", ""]
    out += disagreements(agent_runs, ladder_runs) + [""]
    out += ["## Control: what each design could deliver, with no model in the loop", ""]
    out += ladder_table(ladder_runs) + [""]

    arguments.out.write_text("\n".join(out))
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
