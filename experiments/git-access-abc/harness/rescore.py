#!/usr/bin/env python3
"""Recompute routes and scores on saved runs, re-reading the workers' logs.

Card logs outlive the card, so a run can be re-derived without putting the
probes to the agent again. Used when the route classifier or the answer key
changes: the expensive part of the experiment is the agent turn, and nothing
about it needs repeating to fix an off-by-one in the scoring.

Usage:
    rescore.py results/agent-A-r200.json results/agent-B-r200.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from agent_probe import card_log, classify_route, score  # noqa: E402


def main(paths: list[str]) -> int:
    probes = {
        p["id"]: p
        for p in json.loads(Path("probes.json").read_text())["probes"]
    }
    for name in paths:
        path = Path(name)
        run = json.loads(path.read_text())
        for row in run["rows"]:
            if "error" in row:
                continue
            log = card_log(row.get("task_ids", []))
            route = classify_route(log)
            fresh = score(probes[row["probe"]], row["answer"], route, log)
            row.update(fresh)
        path.write_text(json.dumps(run, ensure_ascii=False, indent=2))
        answered = sum(1 for r in run["rows"] if r.get("answered"))
        escaped = sum(1 for r in run["rows"] if "ghapi" in r.get("routes_used", []))
        print(f"{path.name}: {answered}/{len(run['rows'])} answered, "
              f"{escaped} left for gh api")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
