"""Tests for report_status.py, the read side of the fleet-audit report store."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import audit_report  # noqa: E402
import report_status  # noqa: E402

AUDIT = "compliance-audit"
REPO = "acme/fleet"
NOW = datetime(2026, 8, 1, 9, 30, tzinfo=timezone.utc)


class ReportStatusTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "reports"
        self.scratch = Path(tmp.name) / "scratch"
        self.scratch.mkdir()

    def write_latest(self, audit=AUDIT, repo=REPO, **overrides):
        envelope = {
            "audit_id": audit,
            "repo": repo,
            "finished_at": NOW.isoformat(),
            "status": "UPDATED",
            "issue_number": 42,
            "new_ids": ["a"],
            "resolved_ids": [],
            "current_ids": ["a", "b"],
            "ledger_body": "the rendered body",
            "document": {
                "findings": [{"severity": "critical"}, {"severity": "major"}],
                "scope": {"clusters": [{}, {}, {}], "skipped": [{}]},
            },
        }
        envelope.update(overrides)
        directory = self.root / audit / repo / "runs"
        directory.mkdir(parents=True, exist_ok=True)
        text = json.dumps(envelope)
        (directory / "20260801T093000.000000Z.json").write_text(text)
        (self.root / audit / repo / "latest.json").write_text(text)

    def write_note(self, audit=AUDIT, age_s=60.0, text=None):
        path = self.scratch / f"inflight_{audit}.json"
        started = NOW.timestamp() - age_s
        path.write_text(text if text is not None else json.dumps({"audit": audit, "started_at": started}))
        return path

    def project(self):
        return report_status.project(str(self.root), NOW, scratch=str(self.scratch))


class TestProjection(ReportStatusTestCase):
    def test_a_completed_stream_projects_counts_and_drops_the_heavy_keys(self):
        self.write_latest(chat="kept")
        stream = self.project()["streams"][AUDIT]
        self.assertEqual(stream["liveness"], "completed")
        latest = stream["repos"][REPO]["latest"]
        self.assertEqual((latest["new"], latest["resolved"], latest["current"]), (1, 0, 2))
        self.assertEqual((latest["findings"], latest["critical"]), (2, 1))
        self.assertEqual((latest["clusters"], latest["skipped"]), (3, 1))
        self.assertEqual(latest["repo"], "acme/fleet")
        self.assertEqual(latest["chat"], "kept")
        for key in ("document", "ledger_body", "new_ids", "current_ids"):
            self.assertNotIn(key, latest)
        self.assertIsNone(latest["prs_opened"])
        self.assertEqual(stream["repos"][REPO]["runs"], ["20260801T093000.000000Z.json"])

    def test_a_malformed_envelope_counts_unknown_not_zero(self):
        self.write_latest(new_ids="x", document=[])
        latest = self.project()["streams"][AUDIT]["repos"][REPO]["latest"]
        self.assertIsNone(latest["new"])
        self.assertIsNone(latest["findings"])
        self.assertIsNone(latest["critical"])

    def test_a_corrupt_stream_costs_only_itself(self):
        self.write_latest()
        (self.root / "drift-audit" / REPO).mkdir(parents=True)
        (self.root / "drift-audit" / REPO / "latest.json").write_text("[]")
        streams = self.project()["streams"]
        self.assertEqual(streams["drift-audit"]["liveness"], "error")
        self.assertIn(f"{REPO}: latest.json: not a JSON object", streams["drift-audit"]["error"])
        self.assertEqual(streams[AUDIT]["liveness"], "completed")

    def test_no_store_is_said_rather_than_read_as_an_empty_fleet(self):
        document = self.project()
        self.assertFalse(document["root_exists"])
        self.assertEqual(document["streams"], {})

    def test_the_temp_file_of_a_write_in_progress_is_not_a_run(self):
        self.write_latest()
        (self.root / AUDIT / REPO / "runs" / "tmpabc.tmp").write_text("{")
        self.assertEqual(len(self.project()["streams"][AUDIT]["repos"][REPO]["runs"]), 1)

    def test_each_repository_is_its_own_entry(self):
        self.write_latest()
        self.write_latest(repo="acme/other", status="CLEAN", new_ids=[])
        repos = self.project()["streams"][AUDIT]["repos"]
        self.assertEqual(sorted(repos), [REPO, "acme/other"])
        self.assertEqual(repos["acme/other"]["latest"]["status"], "CLEAN")
        self.assertEqual(repos[REPO]["latest"]["status"], "UPDATED")

    def test_a_directory_that_cannot_be_a_repository_is_not_one(self):
        self.write_latest()
        (self.root / AUDIT / "stray").mkdir()
        (self.root / AUDIT / "acme" / "bad name").mkdir()
        self.assertEqual(list(self.project()["streams"][AUDIT]["repos"]), [REPO])

    def test_a_repository_argument_cannot_leave_the_store(self):
        for repo in ("../x", "acme/..", "a/b/c", ""):
            with self.subTest(repo=repo), self.assertRaises(ValueError):
                report_status.store_path(str(self.root), AUDIT, repo)


class TestLiveness(ReportStatusTestCase):
    def test_the_ttl_is_the_leases(self):
        self.assertEqual(report_status.INFLIGHT_TTL_S, audit_report.INFLIGHT_TTL_SECONDS)

    def test_the_note_path_is_the_one_start_writes(self):
        audit_report_scratch = audit_report.SCRATCH_DIR
        self.assertEqual(
            os.path.join(
                audit_report_scratch,
                f"{report_status.INFLIGHT_PREFIX}{AUDIT}{report_status.INFLIGHT_SUFFIX}",
            ),
            audit_report.inflight_path_for(AUDIT),
        )

    def test_the_five_states(self):
        ttl = report_status.INFLIGHT_TTL_S
        cases = [
            ("never", None, False),
            ("completed", None, True),
            ("running", 60.0, True),
            ("running", ttl - 1, False),
            ("died", ttl, True),
            ("died", ttl * 3, False),
        ]
        for expected, age, finished in cases:
            with self.subTest(expected=expected, age=age, finished=finished):
                self.setUp()
                if finished:
                    self.write_latest()
                if age is not None:
                    self.write_note(age_s=age)
                if not finished and age is None:
                    (self.root / AUDIT).mkdir(parents=True)
                stream = self.project()["streams"][AUDIT]
                self.assertEqual(stream["liveness"], expected)

    def test_a_first_run_in_flight_is_running_before_the_store_exists(self):
        self.write_note(audit="cost-audit", age_s=30.0)
        stream = self.project()["streams"]["cost-audit"]
        self.assertEqual(stream["liveness"], "running")
        self.assertEqual(stream["started"]["age_s"], 30.0)
        self.assertEqual(stream["repos"], {})

    def test_an_unparseable_note_counts_from_its_mtime(self):
        # A `start` that created the note and has not written it yet: a claim.
        path = self.write_note(text="")
        os.utime(path, (NOW.timestamp() - 10, NOW.timestamp() - 10))
        self.assertEqual(self.project()["streams"][AUDIT]["liveness"], "running")

    def test_the_lock_file_is_not_a_stream(self):
        (self.scratch / f"inflight_{AUDIT}.json.lock").write_text("")
        (self.scratch / "inflight_.json").write_text("{}")
        self.assertEqual(self.project()["streams"], {})


class TestCli(ReportStatusTestCase):
    def test_main_prints_one_json_document(self):
        self.write_latest()
        out = io.StringIO()
        with redirect_stdout(out):
            rc = report_status.main(["--root", str(self.root), "--scratch", str(self.scratch)])
        self.assertEqual(rc, 0)
        self.assertIn(AUDIT, json.loads(out.getvalue())["streams"])

    def test_it_runs_from_stdin_with_no_sibling_modules(self):
        """The view streams this file into a pod on stdin, where there is no
        `__file__` and no sibling to import."""
        self.write_latest()
        source = Path(report_status.__file__).read_text()
        result = subprocess.run(
            [sys.executable, "-", "--root", str(self.root), "--scratch", str(self.scratch)],
            input=source,
            capture_output=True,
            text=True,
            cwd=tempfile.gettempdir(),
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["streams"][AUDIT]["liveness"], "completed")


if __name__ == "__main__":
    unittest.main()
