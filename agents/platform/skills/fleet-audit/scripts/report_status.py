#!/usr/bin/env python3
"""report_status.py — the read side of the fleet-audit report store.

Projects `reports/<audit-id>/<owner>/<name>/{latest.json, runs/}` and the
in-flight notes `start` leaves in the scratch directory into one small JSON
document: each stream's liveness and, per repository it publishes to, the last
run's outcome without its findings document and the run ring's filenames. Design of record: docs/designs/fleet-audit-report-store.md.

Two consumers, each pinning one property of this file:

- `scripts/fleet_audit_status_view.py` runs it off-pod by streaming this file
  into the pod on stdin (`kubectl exec -i … -- python3 -`), which keeps the
  view working against an image built before this file was. Streamed stdin has
  no `__file__`, so nothing here may reference one, import a sibling module,
  or reach outside the standard library.
- `fleet-audit-reports/scripts/report_query.py` imports the reading helpers
  below so the two do not grow two parsers of the same files. The module
  therefore does no work at import time: every entry point is a function and
  the CLI sits behind `__main__`.

`root_exists` and the per-stream `error` are the keys that make failure
legible: "I could not look" must never render as "nothing is wrong".
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

# Identical to audit_report.py's, and deliberately not imported from it: this
# file is streamed into a pod whose image may predate that module's copy.
REPORTS_DIR = os.environ.get("FLEET_AUDIT_REPORTS_DIR") or "/opt/data/fleet-audit/reports"
SCRATCH_DIR = os.environ.get("FLEET_AUDIT_SCRATCH_DIR") or "/opt/data/scratch"

# audit_report.INFLIGHT_TTL_SECONDS, duplicated for the same reason. An
# in-flight note this old no longer holds the stream — the next `start` takes
# it — so the two numbers must be the same number or this surface and the
# lease disagree about whether a run holds the stream. A test pins them.
INFLIGHT_TTL_S = 2 * 60 * 60

# audit_report.inflight_path_for's spelling: `<scratch>/inflight_<audit>.json`.
INFLIGHT_PREFIX = "inflight_"
INFLIGHT_SUFFIX = ".json"

# audit_report.REPORT_REPO_SEGMENT_RE, duplicated for the same reason: one
# segment of the `owner/name` a store directory is keyed on, never `.`/`..`.
REPO_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+\Z")

# Always present on a projected `latest`, null when the envelope lacks them, so
# a reader never has to tell an absent key from a null one. Everything else the
# envelope carries except the keys below rides along untouched
# (`_project_latest`), so a key added to the envelope later reaches a reader
# without an edit here.
LATEST_KEYS = (
    "audit_id",
    "repo",
    "finished_at",
    "status",
    "issue_number",
    "issue_url",
    "partial",
    "coverage_gaps",
    "prs_opened",
    "prs_closed",
    "silent_ok",
    "id_scheme",
)

# Keys the projection never carries. `document` is the whole findings document
# and runs to megabytes; `ledger_body` is the rendered issue body, which is
# `finish`'s memory and not a status; the three id lists are summarised as
# `new`/`resolved`/`current`, and the reader that wants the ids themselves
# reads the envelope through `load_latest` instead.
_NEVER_PROJECTED = frozenset(
    {"document", "ledger_document", "ledger_body", "new_ids", "resolved_ids", "current_ids"}
)


def reports_root(root: str | None = None) -> str:
    """The store root: argument, then environment, then the module default.

    The environment is re-read here rather than trusted from import time so a
    caller that sets `FLEET_AUDIT_REPORTS_DIR` after importing the module gets
    the root it set; patching `REPORTS_DIR` on the module works too.
    """
    return str(root or os.environ.get("FLEET_AUDIT_REPORTS_DIR") or REPORTS_DIR)


def scratch_root(root: str | None = None) -> str:
    """Where `start` leaves in-flight notes, resolved the way `reports_root` is."""
    return str(root or os.environ.get("FLEET_AUDIT_SCRATCH_DIR") or SCRATCH_DIR)


def stream_ids(root: str) -> list[str]:
    """Every stream directory under the root, sorted; [] when it is missing.

    A stream is a directory, so a stray temp file never reads as a stream. Any
    other OSError propagates — `project` turns "the root is there but
    unreadable" into `root_exists: false` rather than into an empty fleet.
    """
    try:
        with os.scandir(root) as entries:
            return sorted(entry.name for entry in entries if entry.is_dir())
    except FileNotFoundError:
        return []


def _subdirs(path: str) -> list[str]:
    with os.scandir(path) as entries:
        return sorted(entry.name for entry in entries if entry.is_dir())


def repo_ids(root: str, audit_id: str) -> list[str]:
    """Every `owner/name` the stream has a store for, sorted; [] when none.

    A stream is kept once per repository it publishes to, because an SOP
    walking `managed_repos` finishes it once per repository and each run's
    memory is its own ledger's. OSError other than absence propagates, as in
    `stream_ids`.
    """
    try:
        owners = _subdirs(os.path.join(root, audit_id))
    except FileNotFoundError:
        return []
    return [
        f"{owner}/{name}"
        for owner in owners
        if REPO_SEGMENT_RE.match(owner)
        for name in _subdirs(os.path.join(root, audit_id, owner))
        if REPO_SEGMENT_RE.match(name)
    ]


def store_path(root: str, audit_id: str, repo: str) -> str:
    """The directory one stream keeps for one repository. ValueError for a
    `repo` that is not `owner/name`, so an argument can never walk out of it.
    Lower-cased as audit_report.reports_dir_for writes it: GitHub's names are
    not case-sensitive, so neither is the store."""
    segments = str(repo).lower().split("/")
    if len(segments) != 2 or not all(
        REPO_SEGMENT_RE.match(part) and part not in (os.curdir, os.pardir) for part in segments
    ):
        raise ValueError(f"repository {repo!r} is not owner/name")
    return os.path.join(root, audit_id, *segments)


def in_flight_ids(scratch: str) -> list[str]:
    """Every stream with an in-flight note, sorted; [] when there is none.

    A stream's first run has a note before it has a store directory, and a
    running first run must not read as "never ran".
    """
    try:
        names = os.listdir(scratch)
    except OSError:
        return []
    return sorted(
        name[len(INFLIGHT_PREFIX) : -len(INFLIGHT_SUFFIX)]
        for name in names
        if name.startswith(INFLIGHT_PREFIX)
        and name.endswith(INFLIGHT_SUFFIX)
        and len(name) > len(INFLIGHT_PREFIX) + len(INFLIGHT_SUFFIX)
    )


def read_json(path: str) -> object:
    """One JSON file, parsed. Raises OSError or ValueError; callers catch."""
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _read_object(path: str) -> dict | None:
    """A JSON object, None when the file is absent, ValueError when it is not
    an object. A file holding a list parses fine and is still corrupt."""
    try:
        value = read_json(path)
    except FileNotFoundError:
        return None
    if not isinstance(value, dict):
        raise ValueError("not a JSON object")
    return value


def in_flight_since(scratch: str, audit_id: str) -> float | None:
    """When the run holding this stream started, or None when none holds it.

    audit_report._in_flight_since, restated: a note that exists but does not
    parse — a `start` that created it a moment ago — counts from its mtime,
    because an unreadable note is a claim, not an absence.
    """
    path = os.path.join(scratch, f"{INFLIGHT_PREFIX}{audit_id}{INFLIGHT_SUFFIX}")
    try:
        note = read_json(path)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        note = None
    started = note.get("started_at") if isinstance(note, dict) else None
    if isinstance(started, (int, float)) and not isinstance(started, bool):
        return float(started)
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def load_latest(root: str, audit_id: str, repo: str) -> dict | None:
    """The raw, whole `latest.json`, `document` included.

    The projection strips `document`; report_query.py needs it, so this helper
    is the one that does not.
    """
    return _read_object(os.path.join(store_path(root, audit_id, repo), "latest.json"))


def load_last(root: str, audit_id: str, repo: str) -> tuple[dict | None, bool]:
    """The last run the store kept, whole, and whether it came off the ring.

    `finish` deletes `latest.json` once it has read its memory and restores it
    only on a completed write, so a run that failed in between leaves the ring
    and no `latest.json`. The newest ring entry is then the last run the store
    has, and the flag says a later run may have changed the ledger unrecorded.
    """
    latest = load_latest(root, audit_id, repo)
    if latest is not None:
        return latest, False
    runs = list_runs(root, audit_id, repo)
    if not runs:
        return None, False
    return load_run(root, audit_id, repo, runs[-1]), True


def list_runs(root: str, audit_id: str, repo: str) -> list[str]:
    """Filenames in `runs/`, sorted ascending — which is time order, because
    the stamp is UTC. [] when the ring does not exist yet."""
    try:
        names = os.listdir(os.path.join(store_path(root, audit_id, repo), "runs"))
    except FileNotFoundError:
        return []
    # The atomic write replaces from a `.tmp` file in the same directory, so a
    # read that lands mid-write must not report the temp file as a run.
    return sorted(name for name in names if name.endswith(".json"))


def load_run(root: str, audit_id: str, repo: str, name: str) -> dict | None:
    """One ring entry, whole. None when that stamp is not in the ring."""
    return _read_object(os.path.join(store_path(root, audit_id, repo), "runs", name))


def liveness(
    started: float | None,
    latest: dict | None,
    now_epoch: float,
    ttl: float = INFLIGHT_TTL_S,
    error: str | None = None,
) -> str:
    """Which of five states this stream is in.

    `running` and `died` are the lease's own rule, not a second opinion: a note
    younger than the TTL holds the stream (the next `start` is refused), an
    older one does not (the next `start` takes it over). `died` is a run that
    started and never reached `finish`; nothing here can tell whether its
    session is still going, only that it no longer holds the stream.
    """
    if error:
        return "error"
    if started is not None:
        return "running" if now_epoch - started < ttl else "died"
    if latest is not None:
        return "completed"
    return "never"


def project(
    root: str | None = None,
    now: float | datetime | None = None,
    scratch: str | None = None,
) -> dict:
    """The whole store as one document the view can render off-pod.

    One unreadable stream may not cost the others, so each stream's reads are
    wrapped and a failure becomes that stream's `error` plus `liveness:
    "error"` while the sweep continues.
    """
    root = reports_root(root)
    scratch = scratch_root(scratch)
    now_epoch = _now_epoch(now)
    root_exists = os.path.isdir(root)
    try:
        ids = stream_ids(root)
    except OSError:
        # A root that is present but cannot be listed is a store the view could
        # not read, not a fleet with no streams — and `root_exists` is the key
        # its exit code hangs on.
        ids, root_exists = [], False
    ids = sorted(set(ids) | set(in_flight_ids(scratch)))
    return {
        "root": root,
        "root_exists": root_exists,
        "generated_at": datetime.fromtimestamp(now_epoch, timezone.utc).isoformat(),
        "ttl_s": INFLIGHT_TTL_S,
        "streams": {
            audit_id: _project_stream(root, scratch, audit_id, now_epoch) for audit_id in ids
        },
    }


def _project_stream(root: str, scratch: str, audit_id: str, now_epoch: float) -> dict:
    """The stream's lease, and one entry per repository it has a store for.

    Liveness is the stream's, because the lease is: one `start` holds the
    stream across every repository. `error` names the first repository that
    could not be read, and the entry for it carries its own.
    """
    started = in_flight_since(scratch, audit_id)
    error: str | None = None
    repos: dict[str, dict] = {}
    try:
        ids = repo_ids(root, audit_id)
    except OSError as exc:
        ids, error = [], _failure(f"{audit_id}/", exc)
    any_latest = None
    for repo in ids:
        entry = _project_repo(root, audit_id, repo)
        repos[repo] = entry
        error = error or (f"{repo}: {entry['error']}" if entry["error"] else None)
        any_latest = any_latest or entry["latest"]
    return {
        "started": _project_started(started, now_epoch),
        "repos": repos,
        "liveness": liveness(started, any_latest, now_epoch, error=error),
        "error": error,
    }


def _project_repo(root: str, audit_id: str, repo: str) -> dict:
    latest: dict | None = None
    runs: list[str] = []
    error: str | None = None
    latest_missing = False
    try:
        latest, latest_missing = load_last(root, audit_id, repo)
    except (OSError, ValueError) as exc:
        error = _failure("latest.json", exc)
    try:
        runs = list_runs(root, audit_id, repo)
    except OSError as exc:
        error = error or _failure("runs/", exc)
    return {
        "latest": _project_latest(latest),
        # True when `latest` is the newest ring entry because `latest.json` is
        # gone: a run after it failed, and the ledger may be newer than this.
        "latest_missing": latest_missing,
        "runs": runs,
        "error": error,
    }


def _project_started(started: float | None, now_epoch: float) -> dict | None:
    if started is None:
        return None
    return {
        "started_at": datetime.fromtimestamp(started, timezone.utc).isoformat(),
        "age_s": round(now_epoch - started, 1),
    }


def _project_latest(envelope: dict | None) -> dict | None:
    """The last run's envelope minus the heavy keys, plus counts derived from it.

    Every count is guarded into null rather than a number, because a malformed
    envelope must read as "unknown" and not as "zero findings".
    """
    if envelope is None:
        return None
    row: dict = {key: envelope.get(key) for key in LATEST_KEYS}
    row.update(
        {
            key: value
            for key, value in envelope.items()
            if key not in _NEVER_PROJECTED and key not in row
        }
    )
    row["new"] = _count(envelope.get("new_ids"))
    row["resolved"] = _count(envelope.get("resolved_ids"))
    row["current"] = _count(envelope.get("current_ids"))
    document = envelope.get("document")
    document = document if isinstance(document, dict) else {}
    findings = document.get("findings")
    row["findings"] = _count(findings)
    row["critical"] = (
        sum(1 for finding in findings if _is_critical(finding))
        if isinstance(findings, list)
        else None
    )
    scope = document.get("scope")
    scope = scope if isinstance(scope, dict) else {}
    row["clusters"] = _count(scope.get("clusters"))
    row["skipped"] = _count(scope.get("skipped"))
    return row


def _is_critical(finding: object) -> bool:
    return (
        isinstance(finding, dict)
        and str(finding.get("severity", "")).strip().lower() == "critical"
    )


def _count(value: object) -> int | None:
    return len(value) if isinstance(value, list) else None


def _failure(label: str, exc: Exception) -> str:
    """One line, always — the view prints it in a table cell."""
    return f"{label}: {' '.join(str(exc).split()) or type(exc).__name__}"


def _now_epoch(now: float | datetime | None) -> float:
    if now is None:
        return time.time()
    if isinstance(now, datetime):
        return now.timestamp()
    return float(now)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Project the fleet-audit report store as one JSON document."
    )
    parser.add_argument(
        "--root",
        help=f"store root to read (default: $FLEET_AUDIT_REPORTS_DIR or {REPORTS_DIR})",
    )
    parser.add_argument(
        "--scratch",
        help=f"in-flight note directory (default: $FLEET_AUDIT_SCRATCH_DIR or {SCRATCH_DIR})",
    )
    args = parser.parse_args(argv)
    # Exit 0 even with no store: `root_exists: false` is the answer, and the
    # view decides what a missing store costs.
    print(json.dumps(project(args.root, scratch=args.scratch), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
