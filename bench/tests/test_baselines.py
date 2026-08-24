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

"""Tests for the baseline store, the version key, and computed admission.

The load-bearing properties, in rough order of what they cost if wrong:

1. **Admission is computed from screening evidence, never declared.** It is
   the only thing standing between the collapse rule and a pull request author
   arming it against everyone else in the same diff that adds the case.
2. **A key with no record is STALE, not admitted and not silently compared.**
   A baseline measured on a different agent, judge, or verifier is not
   evidence about this run, and the expensive failure is not noticing.
3. **Three of the five components are read off the run**, so they cannot go
   stale: `setupId` and `scoringVersion` are devops-bench's own, and it
   changes them when the thing they name changes. The captured fixtures are
   what proves those fields exist and where.
4. **Records accumulate.** A model bump appends a key rather than rewriting
   every file, which is what keeps a checked-in store's churn tolerable.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kube_agents_bench.baselines import (
    DEFAULT_ADMISSION_MIN_RUNS,
    DEFAULT_ADMISSION_RATE,
    AdmissionBar,
    BaselineRecord,
    BaselineStore,
    VersionKey,
    Versions,
    load_versions,
)
from kube_agents_bench.scoring import load_run

from conftest import FIXTURE_RUNS

BASELINES = Path(__file__).resolve().parents[1] / "baselines"

VERSIONS = Versions(fleet=1, verifiers=1)

KEY = VersionKey(
    setup_id="gemini-3-1-pro-preview-kubeagents-mcp",
    scoring_version="v1",
    judge_model="gemini-3.1-pro-preview",
    fleet=1,
    verifiers=1,
)


def write_store(root: Path, case: str, records: list[dict]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{case}.json").write_text(
        json.dumps({"case": case, "records": records}, indent=2), encoding="utf-8"
    )
    return root


def record(key: VersionKey = KEY, *, runs: int = 20, passes: int = 19) -> dict:
    return {
        "key": key.to_dict(),
        "recorded_at": "2026-08-25T00:00:00Z",
        "commit": "d3be984d",
        "runs": runs,
        "passes": passes,
    }


# --------------------------------------------------------------------------
# VERSIONS.json
# --------------------------------------------------------------------------


def test_the_shipped_versions_file_parses():
    """The store ships one hand-maintained file; it must be readable."""
    versions = load_versions(BASELINES / "VERSIONS.json")
    assert versions.fleet >= 1 and versions.verifiers >= 1


def test_the_store_ships_empty():
    """Deliberate, and worth failing on if someone lands a record by accident.

    Nothing is admitted until it has been screened against `main`, so on the
    day this lands the collapse rule cannot fire and the aggregate is
    advisory. `BOOTSTRAP_ADMITTED` is the bridge, not a checked-in record.
    """
    assert sorted(p.name for p in BASELINES.glob("*.json")) == ["VERSIONS.json"]


@pytest.mark.parametrize(
    "text", ['{"fleet": 1}', '{"fleet": "one", "verifiers": 1}', "[]", "not json"]
)
def test_a_malformed_versions_file_raises_rather_than_defaulting(tmp_path, text):
    """Defaulting to 1 would score against a baseline measured at version 3.

    That is the stale-baseline failure this module exists to make visible, so
    it may not be introduced by the module's own error handling.
    """
    path = tmp_path / "VERSIONS.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        load_versions(path)


def test_a_missing_versions_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_versions(tmp_path / "VERSIONS.json")


# --------------------------------------------------------------------------
# The version key
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["kanban_red_1", "kanban_green_1"])
def test_the_key_builds_from_a_real_captured_run(name):
    """Three components come free from devops-bench; this proves where."""
    rec = load_run(FIXTURE_RUNS / name)
    key = VersionKey.from_run(
        setup_id=rec.setup_id,
        scoring_version=rec.scoring_version,
        judge_model="gemini-3.1-pro-preview",
        versions=VERSIONS,
    )
    assert key == KEY


def test_the_key_is_none_when_the_run_does_not_carry_one():
    """A key of empty strings would match another equally broken run's key.

    Returning None makes the caller report stale, which is the honest answer;
    an empty-string key would quietly admit garbage against garbage.
    """
    for missing in ("setup_id", "scoring_version", "judge_model"):
        kwargs = {
            "setup_id": "s",
            "scoring_version": "v1",
            "judge_model": "j",
            "versions": VERSIONS,
        }
        kwargs[missing] = None
        assert VersionKey.from_run(**kwargs) is None


def test_the_key_round_trips_through_json():
    assert VersionKey.from_dict(json.loads(json.dumps(KEY.to_dict()))) == KEY


@pytest.mark.parametrize(
    "field, value",
    [
        ("setup_id", "gemini-4-kubeagents-mcp"),
        ("scoring_version", "v2"),
        ("judge_model", "gemini-4-judge"),
        ("fleet", 2),
        ("verifiers", 2),
    ],
)
def test_every_component_alone_changes_the_key(field, value):
    """There is no compatible-enough key.

    In particular the judge is its own component: a judge that tracks whatever
    the agent is running cannot be told apart from an agent that got better,
    and a drifting judge moves every baseline at once.
    """
    import dataclasses

    assert dataclasses.replace(KEY, **{field: value}) != KEY


# --------------------------------------------------------------------------
# The store
# --------------------------------------------------------------------------


def test_a_missing_directory_is_an_empty_store(tmp_path):
    """The state of a fresh checkout before anything has been screened."""
    store = BaselineStore.load(tmp_path / "nope")
    assert store.record_for("anything", KEY) is None


def test_the_shipped_store_loads():
    """VERSIONS.json is not a case file and must be skipped, not parsed."""
    store = BaselineStore.load(BASELINES)
    assert store.record_for("VERSIONS", KEY) is None


def test_a_record_is_found_at_its_exact_key(tmp_path):
    store = BaselineStore.load(write_store(tmp_path, "planted-pdb", [record()]))
    found = store.record_for("planted-pdb", KEY)
    assert found is not None and found.runs == 20 and found.rate == 0.95


def test_records_accumulate_and_only_the_current_key_matches(tmp_path):
    """A model bump appends; the old record stays true about its own software."""
    import dataclasses

    old = dataclasses.replace(KEY, setup_id="gemini-3-0-pro-kubeagents-mcp")
    store = BaselineStore.load(
        write_store(tmp_path, "planted-pdb", [record(old, passes=20), record()])
    )
    assert store.record_for("planted-pdb", old).passes == 20
    assert store.record_for("planted-pdb", KEY).passes == 19


def test_a_filename_that_disagrees_with_its_case_is_fatal(tmp_path):
    """The filename is the join key, and so is the task directory name.

    If they can disagree, a case scores against another case's evidence.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "planted-pdb.json").write_text(
        json.dumps({"case": "something-else", "records": []}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="the filename is the join key"):
        BaselineStore.load(tmp_path)


@pytest.mark.parametrize(
    "doc", ["not json", "[]", '{"case": "c"}', '{"case": "c", "records": [1]}']
)
def test_a_malformed_case_file_is_fatal(tmp_path, doc):
    (tmp_path / "c.json").write_text(doc, encoding="utf-8")
    with pytest.raises(ValueError):
        BaselineStore.load(tmp_path)


# --------------------------------------------------------------------------
# Admission
# --------------------------------------------------------------------------


def test_the_default_bar_is_nineteen_of_twenty():
    bar = AdmissionBar()
    assert (bar.rate, bar.min_runs) == (DEFAULT_ADMISSION_RATE, DEFAULT_ADMISSION_MIN_RUNS)
    assert BaselineRecord(key=KEY, runs=20, passes=19).admits(bar) is True
    assert BaselineRecord(key=KEY, runs=20, passes=18).admits(bar) is False


def test_a_lucky_single_run_does_not_admit():
    """1/1 is a 100% rate and proves nothing.

    Without the run floor, one lucky screening run would arm the collapse
    rule against every future pull request.
    """
    assert BaselineRecord(key=KEY, runs=1, passes=1).admits(AdmissionBar()) is False


def test_the_bar_is_configurable_from_the_environment():
    """Every threshold here is a starting point to be tuned against main."""
    bar = AdmissionBar.from_env({"EVAL_ADMISSION_RATE": "0.8", "EVAL_ADMISSION_MIN_RUNS": "5"})
    assert bar == AdmissionBar(rate=0.8, min_runs=5)
    assert BaselineRecord(key=KEY, runs=5, passes=4).admits(bar) is True


def test_screening_evidence_admits(tmp_path):
    store = BaselineStore.load(write_store(tmp_path, "planted-pdb", [record()]))
    admitted, why = store.is_admitted("planted-pdb", KEY, bar=AdmissionBar())
    assert admitted is True
    assert "19/20" in why


def test_a_case_with_no_evidence_is_not_admitted(tmp_path):
    store = BaselineStore.load(tmp_path)
    admitted, why = store.is_admitted("brand-new", KEY, bar=AdmissionBar())
    assert admitted is False
    assert "no screening evidence" in why


def test_a_key_with_no_record_reports_stale_rather_than_comparing(tmp_path):
    """A version bump de-admits everything until it is re-screened.

    The message has to say *stale*, not *unscreened*: the difference is
    whether someone needs to re-run the screener or write a case.
    """
    import dataclasses

    old = dataclasses.replace(KEY, judge_model="gemini-3.0-judge")
    store = BaselineStore.load(write_store(tmp_path, "planted-pdb", [record(old)]))
    admitted, why = store.is_admitted("planted-pdb", KEY, bar=AdmissionBar())
    assert admitted is False
    assert why.startswith("stale:")
    assert "gemini-3.1-pro-preview" in why


def test_a_run_with_no_key_is_not_admitted(tmp_path):
    store = BaselineStore.load(write_store(tmp_path, "planted-pdb", [record()]))
    admitted, why = store.is_admitted("planted-pdb", None, bar=AdmissionBar())
    assert admitted is False
    assert "no version key" in why


def test_evidence_below_the_bar_says_so(tmp_path):
    store = BaselineStore.load(
        write_store(tmp_path, "planted-pdb", [record(runs=20, passes=12)])
    )
    admitted, why = store.is_admitted("planted-pdb", KEY, bar=AdmissionBar())
    assert admitted is False
    assert "12/20" in why and "below the bar" in why


def test_bootstrap_admits_a_named_case_with_no_store_at_all(tmp_path):
    """The transition bridge.

    Without it every case stops blocking on the day this lands, for as long
    as screening takes.
    """
    store = BaselineStore.load(tmp_path)
    admitted, why = store.is_admitted(
        "gpu-stress-test-diagnosis",
        None,
        bar=AdmissionBar(),
        bootstrap=frozenset({"gpu-stress-test-diagnosis"}),
    )
    assert admitted is True
    assert "BOOTSTRAP_ADMITTED" in why


def test_bootstrap_does_not_leak_to_unnamed_cases(tmp_path):
    store = BaselineStore.load(tmp_path)
    admitted, _ = store.is_admitted(
        "agent-kanban-smoke",
        None,
        bar=AdmissionBar(),
        bootstrap=frozenset({"gpu-stress-test-diagnosis"}),
    )
    assert admitted is False
