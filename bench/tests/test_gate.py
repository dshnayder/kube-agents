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

"""Tests for the ``bench-gate`` CLI: the contract with ``hack/ci-eval-pr.sh``.

The shell can only see three things -- the exit code, the printed lines, and
the JSON hand-off -- so those are what this module pins. In particular:

1. **`case` exits 0 even on a blocking verdict.** The loop has to keep going
   so the summary covers every task; the blocking flag rides in the JSON and
   `suite` is what turns it into an exit code. A `case` that exited non-zero
   would abort the loop under `set -e` and silently drop the remaining tasks.
   It exits 2 only when it could not grade at all.
2. **`suite` exits 1 on red, 0 on green**, and reds on a case result the loop
   never wrote -- unaccounted work is not a pass.
3. **The `Task <id> Result: [...]` line keeps its shape**, because people and
   scripts grep build logs for it.
4. **The environment carries every threshold**, since all of them are meant
   to be tuned against observed movement on main.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kube_agents_bench.gate import main
from kube_agents_bench.scoring import MISSING

from conftest import FIXTURE_RUNS, GREEN_RUNS, RED_RUNS, read_fixture, write_run

BASELINES = Path(__file__).resolve().parents[1] / "baselines"
JUDGE = "gemini-3.1-pro-preview"

KEY = {
    "setup_id": "gemini-3-1-pro-preview-kubeagents-mcp",
    "scoring_version": "v1",
    "judge_model": JUDGE,
    "fleet": 1,
    "verifiers": 1,
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """The gate reads the environment; CI's must not leak into a test."""
    for name in (
        "BOOTSTRAP_ADMITTED",
        "JUDGE_MODEL",
        "DETERMINISTIC_CORRECTNESS_FLOOR",
        "EVAL_AGGREGATE_MARGIN",
        "EVAL_ADMISSION_RATE",
        "EVAL_ADMISSION_MIN_RUNS",
    ):
        monkeypatch.delenv(name, raising=False)


def run_case(kanban_task, runs, out: Path, *extra) -> int:
    argv = ["case", "--task", str(kanban_task), "--baseline-dir", str(BASELINES)]
    for r in runs:
        argv += ["--result", str(r)]
    argv += ["--json-out", str(out), *extra]
    return main(argv)


def payload(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# `bench-gate case`
# --------------------------------------------------------------------------


def test_a_green_case_prints_passed_and_writes_the_hand_off(kanban_task, tmp_path, capsys):
    out = tmp_path / "case.json"
    assert run_case(kanban_task, [FIXTURE_RUNS / n for n in GREEN_RUNS], out) == 0
    printed = capsys.readouterr().out
    assert "Task agent-kanban-smoke Result: [PASSED]" in printed
    doc = payload(out)
    assert doc["blocking"] is False
    assert doc["passes"] == 2 and doc["scored"] == 2


def test_a_blocking_case_still_exits_zero(kanban_task, tmp_path, monkeypatch, capsys):
    """The loop must survive a red task and go on to grade the next one."""
    monkeypatch.setenv("BOOTSTRAP_ADMITTED", "agent-kanban-smoke")
    out = tmp_path / "case.json"
    assert run_case(kanban_task, [FIXTURE_RUNS / n for n in RED_RUNS], out) == 0
    assert "Task agent-kanban-smoke Result: [FAILED]" in capsys.readouterr().out
    assert payload(out)["blocking"] is True


def test_an_infra_case_gets_its_own_label(write_task, tmp_path, capsys):
    task = write_task(
        "planted-pdb",
        {"id": "planted-pdb", "name": "x", "infrastructure": {"deployer": "tofu"}},
    )
    out = tmp_path / "case.json"
    assert main(
        [
            "case", "--task", str(task), "--baseline-dir", str(BASELINES),
            "--result", MISSING, "--json-out", str(out),
        ]
    ) == 0
    assert "[RESOURCE_PREPARATION_FAILED]" in capsys.readouterr().out
    assert payload(out)["blocking"] is False


def test_a_case_that_failed_everything_without_collapsing_is_not_passed(
    kanban_task, tmp_path, capsys
):
    """The state the rate rules create, and the one a two-label scheme lies about.

    Three repetitions failed; the case is unadmitted, so it does not red the
    merge. Printing `[PASSED]` on a run where nothing passed is how a gate
    earns the reputation that gets it switched off.
    """
    out = tmp_path / "case.json"
    assert run_case(kanban_task, [FIXTURE_RUNS / n for n in RED_RUNS], out) == 0
    assert "Task agent-kanban-smoke Result: [UNSTABLE]" in capsys.readouterr().out
    doc = payload(out)
    assert doc["label"] == "UNSTABLE"
    assert doc["blocking"] is False
    assert doc["passes"] == 0


def test_a_partially_passing_case_is_unstable(kanban_task, tmp_path):
    out = tmp_path / "case.json"
    run_case(kanban_task, [FIXTURE_RUNS / GREEN_RUNS[0], FIXTURE_RUNS / RED_RUNS[0]], out)
    assert payload(out)["label"] == "UNSTABLE"


def test_an_expected_fail_case_failing_is_labelled_as_such(write_task, tmp_path, capsys):
    """Failing is the declared intent: neither PASSED nor UNSTABLE fits."""
    task = write_task(
        "edd-case",
        {
            "id": "edd-case",
            "name": "x",
            "expected_fail": True,
            "verification_spec": [{"report_contains": {"phrases": ["x"]}}],
        },
    )
    out = tmp_path / "case.json"
    main(
        ["case", "--task", str(task), "--baseline-dir", str(BASELINES),
         "--result", str(FIXTURE_RUNS / RED_RUNS[0]), "--json-out", str(out)]
    )
    assert "[EXPECTED_FAIL]" in capsys.readouterr().out
    assert payload(out)["label"] == "EXPECTED_FAIL"


def test_the_label_reaches_the_markdown_table(kanban_task, tmp_path):
    case_out = tmp_path / "case.json"
    run_case(kanban_task, [FIXTURE_RUNS / n for n in RED_RUNS], case_out)
    md = tmp_path / "verdict.md"
    main(["suite", "--case-result", str(case_out), "--markdown-out", str(md)])
    assert "UNSTABLE" in md.read_text(encoding="utf-8")


def test_an_unreadable_task_file_exits_two(tmp_path):
    """Distinct from a red verdict: nothing was graded, so nothing is known."""
    assert main(["case", "--task", str(tmp_path / "gone" / "task.yaml")]) == 2


def test_a_broken_versions_file_exits_two(kanban_task, tmp_path):
    """Better to stop than to score every case against an assumed version 1."""
    (tmp_path / "VERSIONS.json").write_text("{}", encoding="utf-8")
    assert main(
        ["case", "--task", str(kanban_task), "--baseline-dir", str(tmp_path),
         "--result", str(FIXTURE_RUNS / GREEN_RUNS[0])]
    ) == 2


def test_the_per_repetition_detail_is_printed(kanban_task, tmp_path, capsys):
    out = tmp_path / "case.json"
    run_case(kanban_task, [FIXTURE_RUNS / n for n in RED_RUNS], out)
    printed = capsys.readouterr().out
    assert "rep 1: fail" in printed and "rep 3: fail" in printed
    # The judged scores are reported, in brackets, and did not gate: the three
    # identical runs disagree by 0.8 while the verdict is the same on all three.
    assert "OutcomeValidity=0.2" in printed and "OutcomeValidity=0.9" in printed
    assert "admission:" in printed


def test_missing_is_accepted_as_a_repetition_placeholder(kanban_task, tmp_path):
    """devops-bench can die before writing a run directory at all.

    The shell has no other way to say "this repetition produced nothing", and
    positional alignment of the remaining repetitions has to survive it.
    """
    out = tmp_path / "case.json"
    assert run_case(
        kanban_task, [FIXTURE_RUNS / GREEN_RUNS[0], MISSING, FIXTURE_RUNS / GREEN_RUNS[1]], out
    ) == 0
    doc = payload(out)
    assert [r["outcome"] for r in doc["reps"]] == ["pass", "blocked", "pass"]


def test_the_version_key_rides_in_the_hand_off(kanban_task, tmp_path, monkeypatch):
    monkeypatch.setenv("JUDGE_MODEL", JUDGE)
    out = tmp_path / "case.json"
    run_case(kanban_task, [FIXTURE_RUNS / GREEN_RUNS[0]], out)
    assert payload(out)["version_key"] == KEY


def test_no_judge_model_means_no_key(kanban_task, tmp_path):
    """`JUDGE_MODEL` unset is not a key of four components; it is no key.

    Comparing against a baseline without knowing which judge produced this
    run is the drift the key exists to catch.
    """
    out = tmp_path / "case.json"
    run_case(kanban_task, [FIXTURE_RUNS / GREEN_RUNS[0]], out)
    doc = payload(out)
    assert doc["version_key"] is None
    assert "no version key" in doc["admission_reason"]


def test_the_key_survives_a_lead_off_infra_repetition(kanban_task, tmp_path, monkeypatch):
    """All repetitions of one case run on the same software.

    Taking the key off the first READABLE record, rather than the first,
    means one dead repetition does not cost the case its baseline match.
    """
    monkeypatch.setenv("JUDGE_MODEL", JUDGE)
    out = tmp_path / "case.json"
    run_case(kanban_task, [MISSING, FIXTURE_RUNS / GREEN_RUNS[0]], out)
    assert payload(out)["version_key"] == KEY


def test_an_empty_record_does_not_supply_the_key(kanban_task, tmp_path, monkeypatch):
    """The empty-list record has a manifest but evaluated nothing.

    Its `setupId` is written before the run, so it survives a resource
    preparation failure and would key the case against a baseline for work
    that never happened. The manifest below is deliberately branded so a
    regression here is visible rather than coincidentally right.
    """
    monkeypatch.setenv("JUDGE_MODEL", JUDGE)
    doc = read_fixture(GREEN_RUNS[0])
    doc["results"] = []
    doc["manifest"]["setupId"] = "died-before-the-agent-ran"
    empty = write_run(tmp_path / "empty", doc)
    out = tmp_path / "case.json"
    run_case(kanban_task, [empty, FIXTURE_RUNS / GREEN_RUNS[0]], out)
    assert payload(out)["version_key"] == KEY


def test_bootstrap_admission_reaches_the_verdict(kanban_task, tmp_path, monkeypatch):
    """Named in the environment, the case collapses; unnamed, it does not."""
    out = tmp_path / "case.json"
    reds = [FIXTURE_RUNS / n for n in RED_RUNS]

    run_case(kanban_task, reds, out)
    assert payload(out)["blocking"] is False

    monkeypatch.setenv("BOOTSTRAP_ADMITTED", "gpu-stress-test-diagnosis,agent-kanban-smoke")
    run_case(kanban_task, reds, out)
    doc = payload(out)
    assert doc["blocking"] is True
    assert doc["rung_name"] == "COLLAPSE"


def test_the_correctness_floor_comes_from_the_environment(kanban_task, tmp_path, monkeypatch):
    monkeypatch.setenv("DETERMINISTIC_CORRECTNESS_FLOOR", "0.5")
    out = tmp_path / "case.json"
    run_case(kanban_task, [FIXTURE_RUNS / RED_RUNS[0]], out)
    assert payload(out)["passes"] == 1


def test_the_deployer_flag_overrides_the_task_file(write_task, tmp_path, capsys):
    """The shell echoes a deployer too; a local variant run may differ."""
    task = write_task(
        "planted-pdb",
        {"id": "planted-pdb", "name": "x", "infrastructure": {"deployer": "tofu"}},
    )
    out = tmp_path / "case.json"
    main(
        ["case", "--task", str(task), "--baseline-dir", str(BASELINES),
         "--deployer", "noop", "--result", MISSING, "--json-out", str(out)]
    )
    # A noop task has no infrastructure to blame, so the same missing record
    # is now a block rather than resource preparation.
    assert payload(out)["blocking"] is True
    assert "provisions nothing" in capsys.readouterr().out


# --------------------------------------------------------------------------
# `bench-gate suite`
# --------------------------------------------------------------------------


def case_file(tmp_path: Path, name: str, **fields) -> Path:
    doc = {
        "case": name, "name": name, "domain": "obtainability",
        "rung": 7, "rung_name": "GREEN", "blocking": False, "reason": "passed",
        "admitted": True, "expected_fail": False,
        "passes": 3, "scored": 3, "pass_rate": 1.0, "reps": [],
    }
    doc.update(fields)
    path = tmp_path / f"case-{name}.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_a_green_suite_exits_zero(tmp_path, capsys):
    rc = main(["suite", "--case-result", str(case_file(tmp_path, "a"))])
    assert rc == 0
    assert "**GREEN**" in capsys.readouterr().out


def test_a_red_suite_exits_one(tmp_path, capsys):
    path = case_file(
        tmp_path, "a", blocking=True, rung=4, rung_name="COLLAPSE", reason="failed 3/3"
    )
    assert main(["suite", "--case-result", str(path)]) == 1
    printed = capsys.readouterr().out
    assert "**RED**" in printed and "### Why it is red" in printed


def test_a_missing_case_result_reds_the_suite(tmp_path, capsys):
    """The loop died partway. Unaccounted work is louder than a blank row."""
    assert main(["suite", "--case-result", str(tmp_path / "never-written.json")]) == 1
    assert "missing case result" in capsys.readouterr().err


def test_an_unreadable_case_result_reds_the_suite(tmp_path, capsys):
    path = tmp_path / "case-a.json"
    path.write_text("{ truncated", encoding="utf-8")
    assert main(["suite", "--case-result", str(path)]) == 1
    assert "unreadable case result" in capsys.readouterr().err


def test_the_markdown_escapes_a_pipe_in_a_reason(tmp_path):
    """A verifier reason can hold a kubectl selector or a phrase list.

    An unescaped pipe silently splits the table cell and shifts every column
    after it, which reads as a different case having failed.
    """
    path = case_file(tmp_path, "a", blocking=True, reason="required: a|b|c")
    md = tmp_path / "verdict.md"
    main(["suite", "--case-result", str(path), "--markdown-out", str(md)])
    row = [ln for ln in md.read_text(encoding="utf-8").splitlines() if ln.startswith("| `a`")][0]
    assert r"a\|b\|c" in row
    assert row.count("|") - row.count(r"\|") == 6


def test_the_suite_json_records_the_aggregate(tmp_path):
    out = tmp_path / "suite.json"
    main(
        ["suite", "--case-result", str(case_file(tmp_path, "a", passes=1, scored=4)),
         "--baseline-rate", "0.9", "--json-out", str(out)]
    )
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["green"] is False
    assert doc["pass_rate"] == 0.25 and doc["baseline_rate"] == 0.9


def test_the_aggregate_margin_comes_from_the_environment(tmp_path, monkeypatch):
    """0.25 against a 0.9 baseline: red at the default margin, green at 0.9."""
    path = case_file(tmp_path, "a", passes=1, scored=4)
    assert main(["suite", "--case-result", str(path), "--baseline-rate", "0.9"]) == 1
    monkeypatch.setenv("EVAL_AGGREGATE_MARGIN", "0.9")
    assert main(["suite", "--case-result", str(path), "--baseline-rate", "0.9"]) == 0


def test_the_verdict_is_advisory_with_no_baseline(tmp_path, capsys):
    """The state this ships in, and it must say so rather than imply a pass."""
    assert main(["suite", "--case-result", str(case_file(tmp_path, "a", passes=0, scored=3))]) == 0
    assert "advisory" in capsys.readouterr().out


def test_case_and_suite_compose_over_the_real_fixtures(kanban_task, tmp_path, monkeypatch):
    """End to end, exactly as the shell will call it.

    Three captured red repetitions of an admitted case red the job; the two
    captured green ones do not.
    """
    monkeypatch.setenv("BOOTSTRAP_ADMITTED", "agent-kanban-smoke")
    monkeypatch.setenv("JUDGE_MODEL", JUDGE)

    red = tmp_path / "case-red.json"
    run_case(kanban_task, [FIXTURE_RUNS / n for n in RED_RUNS], red)
    assert main(["suite", "--case-result", str(red)]) == 1

    green = tmp_path / "case-green.json"
    run_case(kanban_task, [FIXTURE_RUNS / n for n in GREEN_RUNS], green)
    assert main(["suite", "--case-result", str(green)]) == 0
