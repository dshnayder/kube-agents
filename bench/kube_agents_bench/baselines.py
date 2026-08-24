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

"""The checked-in baseline store, the version key, and computed admission.

WHAT A BASELINE IS FOR. Two of the four suite rules need to know how a case
behaves on ``main``: collapse (rung 4) may only red a case that has PROVED it
passes reliably, and the aggregate rule compares this pull request's pass rate
against main's. Neither question can be answered from the pull request's own
run, so the answers are screened once and checked in under
``bench/baselines/``.

THE STORE IS APPEND-ONLY JSONL, one ``<case-id>.jsonl`` per case, one screening
campaign per line. Nothing is ever rewritten: a re-screen appends a line and
the older lines stay, so the file is the case's history and not just its
current state. That matters for three reasons. Re-screening after a model bump
becomes a one-line diff a reviewer can actually read, instead of a rewritten
blob. The old numbers stay available to answer "did this case get less
reliable, or was it always like this" — which is the question that decides
whether a case is worth keeping. And an append conflicts with a concurrent
append far less often than two rewrites of the same object conflict, which is
what makes a checked-in store survive more than a handful of cases.

Only runs on ``main`` append. A pull request's own run is graded against the
store and never writes to it, so a case cannot move the baseline it is about
to be judged against.

ADMISSION IS COMPUTED, NEVER DECLARED. A case is admitted because the store
holds screening evidence for it at the CURRENT version key, not because a task
file says so. Three consequences, all of them the point: a pull request author
cannot self-admit their own case in the same diff that makes it pass; bumping
any version de-admits everything until it is re-screened; and a key with no
record is reported STALE rather than silently compared against a baseline
measured on different software.

THE VERSION KEY, AND WHY IT IS MOSTLY NOT OURS. Three of its five components
are produced by devops-bench and read off the run: ``setupId`` from
``manifest.json`` folds together the agent model, the harness and the
augmentation, and ``scoringVersion`` from ``rows.json`` names the roll-up
formula. Those cannot go stale, because devops-bench changes them when the
thing they name changes. Only ``fleet`` and ``verifiers`` are hand-declared
integers in ``VERSIONS.json``.

Why hand-bumped integers and not content hashes: a hash over ``verifiers.py``
changes on a comment typo, which de-baselines the whole suite — and under a
checked-in store, re-baselining costs a pull request rather than an on-demand
backfill. It is the same contract ``bench/pyproject.toml`` already asks of
contributors for the devops-bench SHA. The trade-off, stated plainly: a
behaviour change with no bump silently compares against a stale baseline. A
lint for that is later work, not this module.

THE JUDGE MODEL IS PINNED INDEPENDENTLY of the agent model, which is why it is
a separate component rather than being folded into ``setupId``. A drifting
judge moves every baseline at once, and a judge that tracks whatever the agent
is running cannot be told apart from an agent that got better.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "AdmissionBar",
    "BaselineRecord",
    "BaselineStore",
    "VersionKey",
    "Versions",
    "load_versions",
]

#: Screening evidence must be at least this fraction of passing runs. 19/20.
DEFAULT_ADMISSION_RATE = 0.95

#: ...over at least this many runs. A case that passed 1 of 1 has proved
#: nothing, and admitting it would let a single lucky run arm the collapse
#: rule against every future pull request.
DEFAULT_ADMISSION_MIN_RUNS = 20


@dataclass(frozen=True)
class AdmissionBar:
    """How much evidence admits a case."""

    rate: float = DEFAULT_ADMISSION_RATE
    min_runs: int = DEFAULT_ADMISSION_MIN_RUNS

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> AdmissionBar:
        src = env if env is not None else os.environ
        return cls(
            rate=float(src.get("EVAL_ADMISSION_RATE", DEFAULT_ADMISSION_RATE)),
            min_runs=int(src.get("EVAL_ADMISSION_MIN_RUNS", DEFAULT_ADMISSION_MIN_RUNS)),
        )


@dataclass(frozen=True)
class Versions:
    """The two hand-declared halves of the key."""

    fleet: int
    verifiers: int


def load_versions(path: str | Path) -> Versions:
    """Read ``bench/baselines/VERSIONS.json``.

    A missing or malformed file is an error rather than a default. Defaulting
    would mean scoring against version 1 of something that might be version 3,
    which is the stale-baseline failure this whole module is built to make
    visible.
    """
    p = Path(path)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FileNotFoundError(f"{p}: cannot read the version pins: {exc}") from exc
    except ValueError as exc:
        raise ValueError(f"{p}: not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise ValueError(f"{p}: expected a JSON object")
    try:
        return Versions(fleet=int(doc["fleet"]), verifiers=int(doc["verifiers"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"{p}: needs integer 'fleet' and 'verifiers' keys: {exc}"
        ) from exc


@dataclass(frozen=True)
class VersionKey:
    """The five components a baseline record is filed under.

    Equality is exact on all five. There is no notion of a compatible-enough
    key: the point of the key is that a baseline measured on other software is
    not evidence about this one.
    """

    setup_id: str
    scoring_version: str
    judge_model: str
    fleet: int
    verifiers: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "setup_id": self.setup_id,
            "scoring_version": self.scoring_version,
            "judge_model": self.judge_model,
            "fleet": self.fleet,
            "verifiers": self.verifiers,
        }

    @classmethod
    def from_dict(cls, doc: dict[str, Any]) -> VersionKey:
        return cls(
            setup_id=str(doc.get("setup_id") or ""),
            scoring_version=str(doc.get("scoring_version") or ""),
            judge_model=str(doc.get("judge_model") or ""),
            fleet=int(doc.get("fleet") or 0),
            verifiers=int(doc.get("verifiers") or 0),
        )

    @classmethod
    def from_run(
        cls,
        *,
        setup_id: str | None,
        scoring_version: str | None,
        judge_model: str | None,
        versions: Versions,
    ) -> VersionKey | None:
        """Build the key for a run, or None when the run does not carry one.

        None is returned rather than a key with empty components: a run whose
        ``manifest.json`` is missing cannot be matched against a baseline, and
        a key of empty strings would match another equally broken run's key.
        The caller reports that as stale, which is the honest answer.
        """
        if not setup_id or not scoring_version or not judge_model:
            return None
        return cls(
            setup_id=setup_id,
            scoring_version=scoring_version,
            judge_model=judge_model,
            fleet=versions.fleet,
            verifiers=versions.verifiers,
        )


@dataclass(frozen=True)
class BaselineRecord:
    """One screening result: how a case behaved on main at one version key."""

    key: VersionKey
    runs: int
    passes: int
    recorded_at: str | None = None
    commit: str | None = None
    judged: dict[str, Any] | None = None

    @property
    def rate(self) -> float | None:
        return (self.passes / self.runs) if self.runs else None

    def admits(self, bar: AdmissionBar) -> bool:
        return (
            self.runs >= bar.min_runs
            and self.rate is not None
            and self.rate >= bar.rate
        )


class BaselineStore:
    """``bench/baselines/<case-id>.jsonl``, one file per case.

    One screening campaign per line, in the order they were run. Lines are
    only ever appended, so a file read bottom-up is the case's history from
    newest to oldest.
    """

    def __init__(self, records: dict[str, list[BaselineRecord]]):
        self._records = records

    @classmethod
    def load(cls, directory: str | Path) -> BaselineStore:
        """Read every ``<case>.jsonl`` in ``directory``.

        A missing directory is an empty store, not an error: that is the state
        this ships in, and it is the state a fresh checkout is in before
        anything has been screened.
        """
        root = Path(directory)
        records: dict[str, list[BaselineRecord]] = {}
        if not root.is_dir():
            return cls(records)

        # A leftover `<case>.json` is refused rather than ignored. Skipping it
        # would read as "this case has never been screened", which silently
        # de-admits the case instead of telling anyone the file is in the old
        # format.
        for stray in sorted(root.glob("*.json")):
            if stray.name != "VERSIONS.json":
                raise ValueError(
                    f"{stray}: the store is JSONL now; rename it to "
                    f"{stray.stem}.jsonl, one record per line"
                )

        for path in sorted(root.glob("*.jsonl")):
            parsed: list[BaselineRecord] = []
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise ValueError(f"{path}: cannot read: {exc}") from exc
            for line_no, line in enumerate(text.splitlines(), start=1):
                # Blank lines are tolerated: an append that raced a trailing
                # newline should not take the presubmit down.
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except ValueError as exc:
                    raise ValueError(f"{path}:{line_no}: not valid JSON: {exc}") from exc
                if not isinstance(entry, dict):
                    raise ValueError(f"{path}:{line_no}: expected a JSON object")
                case_id = str(entry.get("case") or path.stem)
                if case_id != path.stem:
                    raise ValueError(
                        f"{path}:{line_no}: declares case {case_id!r} but is filed "
                        f"as {path.stem!r}; the filename is the join key"
                    )
                parsed.append(
                    BaselineRecord(
                        key=VersionKey.from_dict(entry.get("key") or {}),
                        runs=int(entry.get("runs") or 0),
                        passes=int(entry.get("passes") or 0),
                        recorded_at=entry.get("recorded_at"),
                        commit=entry.get("commit"),
                        judged=entry.get("judged"),
                    )
                )
            records[path.stem] = parsed
        return cls(records)

    def record_for(self, case_id: str, key: VersionKey | None) -> BaselineRecord | None:
        """The NEWEST screening record for this case at this exact key.

        Last line wins. Re-screening at a key that already has evidence is an
        append, so the most recent campaign is the one that describes the
        software as it stands; the earlier lines are history, not candidates.
        """
        if key is None:
            return None
        for record in reversed(self._records.get(case_id, [])):
            if record.key == key:
                return record
        return None

    def history_for(self, case_id: str) -> list[BaselineRecord]:
        """Every record for a case, oldest first, across all version keys."""
        return list(self._records.get(case_id, []))

    def is_admitted(
        self,
        case_id: str,
        key: VersionKey | None,
        *,
        bar: AdmissionBar,
        bootstrap: frozenset[str] = frozenset(),
    ) -> tuple[bool, str]:
        """Whether the case may reach rung 4, and the one-line why.

        ``bootstrap`` is the transition bridge. The store ships empty, so
        without it every case would stop blocking on the day this lands and
        the presubmit would grade nothing for as long as screening takes.
        Named cases keep their old blocking behaviour meanwhile. It is
        deliberately an environment list in the shell rather than a field in
        the store: a bridge that is inconvenient to extend is a bridge people
        take down.
        """
        if case_id in bootstrap:
            return True, "admitted by BOOTSTRAP_ADMITTED (transition bridge)"
        if key is None:
            return False, "the run carries no version key, so no baseline matches it"
        record = self.record_for(case_id, key)
        if record is None:
            known = len(self._records.get(case_id, []))
            if known:
                return False, (
                    f"stale: {known} baseline record(s) exist for this case but "
                    f"none at the current key ({key.setup_id}, judge "
                    f"{key.judge_model}, fleet {key.fleet}, verifiers "
                    f"{key.verifiers}) -- re-screen before this case can collapse"
                )
            return False, "no screening evidence for this case yet"
        if record.admits(bar):
            return True, (
                f"admitted on {record.passes}/{record.runs} screening runs "
                f"(bar {bar.rate:.0%} over {bar.min_runs})"
            )
        return False, (
            f"screened at {record.passes}/{record.runs}, below the bar of "
            f"{bar.rate:.0%} over {bar.min_runs} runs"
        )
