"""Tests for the incident_context pre_gateway_dispatch hook.

    python3 -m unittest discover -s agents/platform/plugins/incident_context -p 'test_*.py'

The hook rewrites the text of every inbound chat message, which makes its
*guards* the interesting part: it must stay out of the way of slash commands and
of platforms it knows nothing about, and it must fail open when the Session KV
server is down rather than swallowing the user's message.

Loaded by file path rather than by name: the module under test is a plugin
package's `__init__.py`, and the gateway imports it as `incident_context`.
"""

import importlib.util
import pathlib
import unittest
from unittest.mock import patch

_HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("incident_context", _HERE / "__init__.py")
ic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ic)


class _Source:
    def __init__(self, platform="google_chat", chat_id="spaces/AAA", thread_id=None):
        self.platform = platform
        self.chat_id = chat_id
        self.thread_id = thread_id


class _Event:
    def __init__(self, text="what is this report about?", **kwargs):
        self.text = text
        self.source = _Source(**kwargs)


class IndexFallbackTest(unittest.TestCase):
    """What happens when no report is keyed to the incoming message."""

    def setUp(self):
        self.by_thread = patch.object(ic, "_lookup", return_value=None)
        self.by_thread.start()
        self.addCleanup(self.by_thread.stop)

    def index(self, reports, **kwargs):
        with patch.object(ic, "_lookup_recent", return_value=reports):
            return ic.on_inbound(event=_Event(**kwargs))

    def test_a_chat_reply_with_no_thread_gets_the_index(self):
        """The main compose box: Google Chat sends no thread_id at all."""
        result = self.index([{"job_id": "deploy-smoke", "profile": "platform"}])
        self.assertEqual(result["action"], "rewrite")
        self.assertIn("deploy-smoke", result["text"])
        self.assertIn("what is this report about?", result["text"])

    def test_a_slack_top_level_message_gets_the_index(self):
        """Slack sends the message's own ts, which matches no stored report."""
        result = self.index(
            [{"job_id": "compliance-audit", "profile": "platform"}],
            platform="slack",
            chat_id="C123",
            thread_id="1755440416.001",
        )
        self.assertIn("compliance-audit", result["text"])

    def test_the_index_tells_the_agent_to_ask_rather_than_guess(self):
        """The whole point: it binds to the wrong antecedent instead of asking."""
        result = self.index([{"job_id": "a"}, {"job_id": "b"}])
        self.assertIn("do NOT have their contents", result["text"])
        self.assertIn("ask which one", result["text"])
        self.assertIn("do not guess", result["text"])

    def test_nothing_recent_leaves_the_message_alone(self):
        self.assertIsNone(self.index([]))

    def test_a_thread_hit_still_wins(self):
        with patch.object(ic, "_lookup", return_value="the full report"), \
             patch.object(ic, "_lookup_recent") as index:
            result = ic.on_inbound(event=_Event(thread_id="spaces/AAA/threads/T1"))
        self.assertIn("the full report", result["text"])
        index.assert_not_called()


class IndexRenderingTest(unittest.TestCase):
    def test_every_field_is_used_when_present(self):
        text = ic._index_text(
            [
                {
                    "job_id": "deploy-smoke-20260817",
                    "title": "Deploy verification",
                    "profile": "platform",
                    "created_at": "2026-08-17 14:40:16",
                }
            ],
            "hi",
        )
        self.assertIn(
            '- deploy-smoke-20260817 "Deploy verification" (platform agent) - 2026-08-17 14:40 UTC',
            text,
        )

    def test_an_iso_timestamp_renders_the_same_as_a_sqlite_one(self):
        sqlite_style = ic._index_text([{"job_id": "j", "created_at": "2026-08-17 14:40:16"}], "hi")
        iso_style = ic._index_text([{"job_id": "j", "created_at": "2026-08-17T14:40:16+00:00"}], "hi")
        self.assertEqual(sqlite_style, iso_style)

    def test_an_unlabelled_report_still_gets_a_line(self):
        """A `send_notification` incident has no relay session to name it."""
        text = ic._index_text([{"thread_id": "T1"}], "hi")
        self.assertIn("- scheduled report", text)

    def test_a_title_that_repeats_the_job_id_is_not_printed_twice(self):
        text = ic._index_text([{"job_id": "compliance-audit", "title": "compliance-audit"}], "hi")
        self.assertEqual(text.count("compliance-audit"), 1)


class GuardTest(unittest.TestCase):
    """The hook sees every inbound message, so what it declines to touch matters."""

    def call(self, **kwargs):
        text = kwargs.pop("text", "hello")
        with patch.object(ic, "_lookup", return_value="a report"), \
             patch.object(ic, "_lookup_recent", return_value=[{"job_id": "j"}]):
            return ic.on_inbound(event=_Event(text=text, **kwargs))

    def test_an_unknown_platform_is_left_alone(self):
        self.assertIsNone(self.call(platform="cli"))

    def test_a_message_with_no_chat_id_is_left_alone(self):
        self.assertIsNone(self.call(chat_id=None))

    def test_a_slash_command_is_left_alone(self):
        """Prepending moves `/hermes sethome` off character zero and it stops parsing."""
        self.assertIsNone(self.call(text="  /hermes sethome", thread_id="spaces/AAA/threads/T1"))
        self.assertIsNone(self.call(text="/hermes sethome"))


class FailOpenTest(unittest.TestCase):
    """A Session KV server that is down must never eat a user's message."""

    def test_a_dead_server_returns_no_index(self):
        with patch.object(ic.urllib.request, "urlopen", side_effect=OSError("connection refused")):
            self.assertEqual(ic._lookup_recent("spaces/AAA"), [])
            self.assertIsNone(ic._lookup("spaces/AAA", "T1"))

    def test_a_dead_server_leaves_the_message_untouched(self):
        with patch.object(ic.urllib.request, "urlopen", side_effect=OSError("connection refused")):
            self.assertIsNone(ic.on_inbound(event=_Event()))


if __name__ == "__main__":
    unittest.main()
