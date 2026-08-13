"""Unit tests for the ``deliver: "chat"`` relay module.

Covers ``cron_deliver_chat.py`` on its own. What it cannot cover is the edit
the applier makes inside Hermes' ``_deliver_result`` — that is
``verify_cron_deliver_chat.py``, which runs against the patched tree at build
time.
"""

from __future__ import annotations

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

import cron_deliver_chat as mod


class RecordingRelay:
    """A stdlib HTTP server standing in for the Session KV server."""

    def __init__(self, status: int = 202) -> None:
        self.status = status
        self.requests: list[dict] = []
        server_self = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 — stdlib naming
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length).decode("utf-8")
                server_self.requests.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization", ""),
                        "content_type": self.headers.get("Content-Type", ""),
                        "body": json.loads(raw) if raw else {},
                    }
                )
                self.send_response(server_self.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *_args) -> None:
                """Keep the test output clean."""

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "RecordingRelay":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[0], self._server.server_address[1]
        return f"http://{host}:{port}/v1/cron-reports"


class TestWantsChatDelivery(unittest.TestCase):
    def test_the_bare_token(self):
        self.assertTrue(mod.wants_chat_delivery("chat"))

    def test_case_and_whitespace_do_not_matter(self):
        for value in ("CHAT", " chat ", "Chat"):
            with self.subTest(value=value):
                self.assertTrue(mod.wants_chat_delivery(value))

    def test_one_token_among_several(self):
        self.assertTrue(mod.wants_chat_delivery("origin,chat"))

    def test_the_list_form_mcp_clients_send(self):
        self.assertTrue(mod.wants_chat_delivery(["chat"]))

    def test_other_delivery_values_are_untouched(self):
        for value in ("all", "local", "origin", "slack:C123", "", None):
            with self.subTest(value=value):
                self.assertFalse(mod.wants_chat_delivery(value))

    def test_a_platform_that_merely_starts_with_chat(self):
        """``google_chat`` and ``chatwork`` are real platform names."""
        self.assertFalse(mod.wants_chat_delivery("google_chat"))
        self.assertFalse(mod.wants_chat_delivery("chatwork:room1"))


class TestProfileName(unittest.TestCase):
    def test_a_named_profile(self):
        with patch.dict(os.environ, {"HERMES_HOME": "/opt/data/profiles/platform"}):
            self.assertEqual(mod.profile_name(), "platform")

    def test_a_cluster_profile(self):
        with patch.dict(os.environ, {"HERMES_HOME": "/opt/data/profiles/cluster-prod-a"}):
            self.assertEqual(mod.profile_name(), "cluster-prod-a")

    def test_the_root_home_is_not_called_data(self):
        with patch.dict(os.environ, {"HERMES_HOME": "/opt/data"}):
            self.assertEqual(mod.profile_name(), "default")

    def test_an_unset_home(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(mod.profile_name(), "default")


class TestRelay(unittest.TestCase):
    JOB = {"id": "github-issue-resolver", "name": "GitHub issue resolver"}

    def test_a_report_reaches_the_route_with_its_key(self):
        with RecordingRelay() as relay:
            with patch.dict(
                os.environ,
                {
                    "SESSION_KV_API_KEY": "k",
                    "CRON_REPORT_RELAY_URL": relay.url,
                    "HERMES_HOME": "/opt/data/profiles/platform",
                },
            ):
                self.assertIsNone(mod.relay(self.JOB, "two issues triaged"))
            self.assertEqual(len(relay.requests), 1)
            sent = relay.requests[0]
            self.assertEqual(sent["path"], "/v1/cron-reports")
            self.assertEqual(sent["authorization"], "Bearer k")
            self.assertEqual(sent["content_type"], "application/json")
            self.assertEqual(
                sent["body"],
                {
                    "job_id": "github-issue-resolver",
                    "profile": "platform",
                    "title": "GitHub issue resolver",
                    "report": "two issues triaged",
                },
            )

    def test_no_key_is_refused_before_the_request(self):
        with RecordingRelay() as relay:
            with patch.dict(
                os.environ, {"CRON_REPORT_RELAY_URL": relay.url}, clear=True
            ):
                error = mod.relay(self.JOB, "r")
            self.assertIn("SESSION_KV_API_KEY", error or "")
            self.assertEqual(relay.requests, [], "nothing should have been sent")

    def test_a_server_error_is_reported_not_raised(self):
        with RecordingRelay(status=500) as relay:
            with patch.dict(
                os.environ,
                {"SESSION_KV_API_KEY": "k", "CRON_REPORT_RELAY_URL": relay.url},
            ):
                error = mod.relay(self.JOB, "r")
        self.assertIn("500", error or "")

    def test_an_unreachable_relay_is_reported_not_raised(self):
        with patch.dict(
            os.environ,
            {
                "SESSION_KV_API_KEY": "k",
                # Port 1 is reserved and never listening.
                "CRON_REPORT_RELAY_URL": "http://127.0.0.1:1/v1/cron-reports",
            },
        ):
            error = mod.relay(self.JOB, "r")
        self.assertIn("unreachable", (error or "").lower())

    def test_no_failure_string_carries_the_key(self):
        """These strings end up in ``last_delivery_error`` and in the log."""
        secret = "s3cr3t-session-kv-key"
        with RecordingRelay(status=503) as relay:
            with patch.dict(
                os.environ,
                {"SESSION_KV_API_KEY": secret, "CRON_REPORT_RELAY_URL": relay.url},
            ):
                error = mod.relay(self.JOB, "r")
        self.assertNotIn(secret, error or "")

    def test_the_default_route_is_the_loopback_session_kv_server(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(mod._relay_url(), mod.DEFAULT_RELAY_URL)
        self.assertTrue(mod.DEFAULT_RELAY_URL.startswith("http://127.0.0.1:8699/"))


class TestInterceptChatDelivery(unittest.TestCase):
    def test_a_job_that_did_not_ask_is_handed_straight_back(self):
        job = {"id": "j", "deliver": "all"}
        with patch.object(mod, "relay") as relayed:
            self.assertIs(mod.intercept_chat_delivery(job, "r"), job)
        relayed.assert_not_called()

    def test_a_relayed_report_ends_the_scheduler_path(self):
        job = {"id": "j", "deliver": "chat"}
        with patch.object(mod, "relay", return_value=None):
            self.assertIsNone(mod.intercept_chat_delivery(job, "r"))

    def test_a_failed_relay_falls_back_to_all(self):
        job = {"id": "j", "deliver": "chat"}
        with patch.object(mod, "relay", return_value="relay unreachable"):
            result = mod.intercept_chat_delivery(job, "r")
        self.assertEqual(result["deliver"], "all")

    def test_the_fallback_does_not_rewrite_the_caller_s_job(self):
        """``mark_job_run`` persists this dict; a relay outage is not a roster edit."""
        job = {"id": "j", "deliver": "chat"}
        with patch.object(mod, "relay", return_value="relay unreachable"):
            mod.intercept_chat_delivery(job, "r")
        self.assertEqual(job["deliver"], "chat")

    def test_an_exception_falls_back_instead_of_escaping(self):
        """An escape here is recorded as the *run* having failed, which it did not."""
        job = {"id": "j", "deliver": "chat"}
        with patch.object(mod, "relay", side_effect=RuntimeError("boom")):
            result = mod.intercept_chat_delivery(job, "r")
        self.assertEqual(result["deliver"], "all")

    def test_the_report_is_what_gets_relayed(self):
        job = {"id": "j", "deliver": "chat"}
        with patch.object(mod, "relay", return_value=None) as relayed:
            mod.intercept_chat_delivery(job, "the finding")
        relayed.assert_called_once_with(job, "the finding")


if __name__ == "__main__":
    unittest.main()
