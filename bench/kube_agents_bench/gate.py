# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""``bench-gate``: the presubmit's verdict, moved out of the shell.

Two subcommands. ``bench-gate case`` grades one task's repetitions and writes
a JSON hand-off; ``bench-gate suite`` reads those hand-offs and decides the
job's exit status. The split exists because the shell loop already knows how
to run a task and diff the results directory — those are genuinely shell
concerns — while the ladder, the collapse rule and the aggregate are not, and
were previously four inline ``python3 -c`` heredocs that no test could reach.

EXIT CODES. ``case`` exits 0 whenever it produced a verdict, including a
blocking one: the loop must keep going so the summary covers every task, and
the blocking flag rides in the JSON. It exits 2 when it could not grade at all
(an unreadable task file, a bad flag). ``suite`` exits 0 green, 1 red.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from kube_agents_bench.baselines import AdmissionBar, BaselineStore, VersionKey, load_versions
from kube_agents_bench.cases import CaseSpecError, load_case
from kube_agents_bench.scoring import (
    DEFAULT_CORRECTNESS_FLOOR,
    MISSING,
    Rung,
    grade_case,
    grade_suite,
    load_run,
)

__all__ = ["main"]

_DEFAULT_BASELINE_DIR = "baselines"


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _bootstrap_admitted() -> frozenset[str]:
    """Cases that keep blocking through the transition, from the environment.

    Whitespace- or comma-separated, so the shell can write either.
    """
    raw = os.environ.get("BOOTSTRAP_ADMITTED", "")
    return frozenset(part for part in raw.replace(",", " ").split() if part)


def _label(case: dict[str, Any]) -> str:
    """The build-log word for a case verdict.

    Four labels, not two, because the rate rules create a state a two-label
    scheme cannot say. UNSTABLE is a case that failed repetitions -- but not
    all of them, or not with the screening evidence to red a merge. Calling
    that PASSED would report a case as passing on a run where it passed
    nothing, which is the sort of quiet lie that gets a gate switched off.

    FAILED and RESOURCE_PREPARATION_FAILED keep their historical spellings:
    people and scripts grep build logs for both.
    """
    if int(case.get("rung") or Rung.GREEN) == int(Rung.INFRA):
        return "RESOURCE_PREPARATION_FAILED"
    if case.get("blocking"):
        return "FAILED"
    scored = int(case.get("scored") or 0)
    if case.get("expected_fail"):
        # Failing is the declared intent, so neither PASSED nor UNSTABLE fits.
        return "EXPECTED_FAIL"
    if scored and int(case.get("passes") or 0) == scored:
        return "PASSED"
    return "UNSTABLE"


def _cmd_case(args: argparse.Namespace) -> int:
    try:
        spec = load_case(args.task)
    except CaseSpecError as exc:
        print(f"Task {args.task} Result: [FAILED] {exc}", file=sys.stderr)
        return 2

    # The shell knows the deployer too (it echoes it into the log). Prefer the
    # task file, which is parsed properly, and accept the flag as an override
    # for the local-run case where someone is testing a variant.
    deployer = args.deployer or spec.deployer
    if deployer != spec.deployer:
        spec = dataclasses.replace(spec, deployer=deployer)

    run_dirs: list[str | None] = [
        None if d == MISSING else d for d in (args.result or [])
    ]

    # The version key comes off the first repetition that produced a readable
    # record. All repetitions of one case run on the same software, so any of
    # them answers; taking the first readable one tolerates a lead-off infra
    # failure without losing the key.
    key: VersionKey | None = None
    admission_reason = "no readable record, so no version key"
    try:
        versions = load_versions(Path(args.baseline_dir) / "VERSIONS.json")
    except (FileNotFoundError, ValueError) as exc:
        print(f"Task {spec.case_id} Result: [FAILED] {exc}", file=sys.stderr)
        return 2

    for run_dir in run_dirs:
        record = load_run(run_dir) if run_dir else None
        if record is None or record.empty_record:
            continue
        key = VersionKey.from_run(
            setup_id=record.setup_id,
            scoring_version=record.scoring_version,
            judge_model=args.judge_model or os.environ.get("JUDGE_MODEL"),
            versions=versions,
        )
        break

    store = BaselineStore.load(args.baseline_dir)
    admitted, admission_reason = store.is_admitted(
        spec.case_id, key, bar=AdmissionBar.from_env(), bootstrap=_bootstrap_admitted()
    )

    verdict = grade_case(
        spec,
        list(run_dirs),
        admitted=admitted,
        correctness_floor=args.correctness_floor,
    )

    payload = verdict.to_dict()
    payload["admission_reason"] = admission_reason
    payload["version_key"] = key.to_dict() if key else None
    # The shell used to grep this out of the task file itself and echo it; it
    # is reported here instead so there is one parser, not two that can
    # disagree about which task provisions infrastructure.
    payload["deployer"] = spec.deployer
    payload["label"] = _label(payload)

    # The one-line log the presubmit has always printed, in the same shape so
    # anyone grepping build logs for "Result:" keeps finding them.
    print(f"Task {spec.case_id} Result: [{payload['label']}] {verdict.reason}")
    print(f"  deployer: {spec.deployer}")
    for rep in verdict.reps:
        judged = " ".join(f"{k}={v}" for k, v in sorted(rep.judged.items()))
        print(f"  rep {rep.index}: {rep.outcome} -- {rep.reason}" + (f" [{judged}]" if judged else ""))
    print(f"  admission: {admission_reason}")

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    return 0


def _markdown(verdict: Any, cases: list[dict[str, Any]]) -> str:
    lines = [
        "## Evaluation verdict",
        "",
        f"**{'GREEN' if verdict.green else 'RED'}**",
        "",
    ]
    if verdict.pass_rate is not None:
        rate = f"{verdict.pass_rate:.1%}"
        if verdict.baseline_rate is not None:
            rate += f" (main: {verdict.baseline_rate:.1%}, margin {verdict.margin:.1%})"
        else:
            rate += " (no baseline at the current version key -- advisory)"
        lines += [f"Admitted-case pass rate: {rate}", ""]
    if verdict.reasons:
        lines += ["### Why it is red", ""]
        lines += [f"- {r}" for r in verdict.reasons]
        lines += [""]
    lines += [
        "| Case | Domain | Verdict | Passes | Detail |",
        "| --- | --- | --- | --- | --- |",
    ]
    for case in cases:
        rung = Rung(int(case.get("rung") or Rung.GREEN))
        # `label` is written by `case`; recompute for a hand-authored file.
        mark = case.get("label") or _label(case)
        scored = case.get("scored") or 0
        # A verifier's reason can contain a pipe (a required-phrase list, a
        # kubectl selector), which would silently split the table cell.
        detail = str(case.get("reason") or "").replace("|", "\\|")
        lines.append(
            f"| `{case.get('case')}` | {case.get('domain') or '--'} | {mark} "
            f"(rung {int(rung)}) | {case.get('passes')}/{scored} | {detail} |"
        )
    return "\n".join(lines) + "\n"


def _cmd_suite(args: argparse.Namespace) -> int:
    cases: list[dict[str, Any]] = []
    for path in args.case_result or []:
        p = Path(path)
        if not p.is_file():
            # A per-case file the loop never wrote means the loop died partway.
            # Louder than a missing entry in a table: it is unaccounted work.
            print(f"::error::missing case result {p}", file=sys.stderr)
            return 1
        try:
            cases.append(json.loads(p.read_text(encoding="utf-8")))
        except ValueError as exc:
            print(f"::error::unreadable case result {p}: {exc}", file=sys.stderr)
            return 1

    verdict = grade_suite(
        cases,
        baseline_rate=args.baseline_rate,
        margin=args.margin,
    )

    text = _markdown(verdict, cases)
    print(text)
    if args.markdown_out:
        out = Path(args.markdown_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(verdict.to_dict(), indent=2) + "\n", encoding="utf-8")

    return 0 if verdict.green else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bench-gate",
        description="Grade devops-bench runs against the rate-based eval gate.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    case = sub.add_parser("case", help="grade one task's repetitions")
    case.add_argument("--task", required=True, help="path to bench/tasks/<id>/task.yaml")
    case.add_argument(
        "--deployer",
        default=None,
        help="override the task file's infrastructure.deployer",
    )
    case.add_argument(
        "--result",
        action="append",
        default=[],
        metavar="RUN_DIR",
        help=f"a run directory, or the literal {MISSING}; repeat once per repetition",
    )
    case.add_argument("--json-out", default=None, help="write the case hand-off here")
    case.add_argument(
        "--baseline-dir",
        default=_DEFAULT_BASELINE_DIR,
        help="the checked-in baseline store (default: %(default)s)",
    )
    case.add_argument(
        "--judge-model",
        default=None,
        help="judge model for the version key (default: $JUDGE_MODEL)",
    )
    case.add_argument(
        "--correctness-floor",
        type=float,
        default=_env_float(
            "DETERMINISTIC_CORRECTNESS_FLOOR", DEFAULT_CORRECTNESS_FLOOR
        ),
        help="VerificationCorrectness a repetition must meet (default: %(default)s)",
    )
    case.set_defaults(func=_cmd_case)

    suite = sub.add_parser("suite", help="combine case hand-offs into the job verdict")
    suite.add_argument(
        "--case-result",
        action="append",
        default=[],
        metavar="JSON",
        help="a file written by `bench-gate case --json-out`; repeat per case",
    )
    suite.add_argument("--markdown-out", default=None)
    suite.add_argument("--json-out", default=None)
    suite.add_argument(
        "--baseline-rate",
        type=float,
        default=None,
        help="main's admitted-case pass rate; omit while no baseline exists",
    )
    suite.add_argument(
        "--margin",
        type=float,
        default=_env_float("EVAL_AGGREGATE_MARGIN", 0.05),
        help="non-inferiority margin on the aggregate (default: %(default)s)",
    )
    suite.set_defaults(func=_cmd_suite)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
