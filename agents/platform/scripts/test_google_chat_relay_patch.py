"""The relay patch's inline delivery of files it cannot attach (#999).

The transport half of this patch is covered by
``tests/integration/test_seam_chat_ingress.py``, which drives the real closures
against the real credential proxy. This file covers the half that has no
network in it: what the adapter does with a deliverable on an install where
``media.upload`` is unreachable.
"""

import asyncio
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


def _install_gateway_stub() -> dict:
    """Stub ``gateway.platform_registry``, the one hermes module ``install()``
    imports. Returns the saved modules so a test can restore them."""
    registry_module = types.ModuleType("gateway.platform_registry")

    class PlatformRegistry:
        def create_adapter(self, name, *args, **kwargs):
            return None

    registry_module.PlatformRegistry = PlatformRegistry
    gateway_pkg = types.ModuleType("gateway")
    gateway_pkg.platform_registry = registry_module
    saved = {
        name: sys.modules.get(name)
        for name in ("gateway", "gateway.platform_registry")
    }
    sys.modules["gateway"] = gateway_pkg
    sys.modules["gateway.platform_registry"] = registry_module
    return saved


class FakeSendResult:
    """Stands in for ``gateway.platforms.base.SendResult``."""

    def __init__(self, success=True, error=None):
        self.success = success
        self.error = error
        self.message_id = "spaces/AAA/messages/m1" if success else None


def make_adapter_class(send_results=None):
    """A minimal adapter carrying only what the fallback override touches.

    Fresh per test: ``patch_adapter_class`` latches on the class it patched, so
    a shared class would keep the first test's closures.
    """

    class MinimalAdapter:
        def __init__(self):
            self.sent = []
            self.fallback_calls = []
            self._send_results = list(send_results or [])

        async def send(self, chat_id, content, metadata=None):
            self.sent.append((chat_id, content, metadata))
            if self._send_results:
                return self._send_results.pop(0)
            return FakeSendResult()

        async def _post_attachment_fallback(
            self, chat_id, path, filename, caption, thread_id
        ):
            """The build-time-patched notice this override defers to."""
            self.fallback_calls.append(
                {
                    "chat_id": chat_id,
                    "path": path,
                    "filename": filename,
                    "caption": caption,
                    "thread_id": thread_id,
                }
            )
            return FakeSendResult(success=False, error="not attached")

    return MinimalAdapter


class InlineHelpersTest(unittest.TestCase):
    """``_inline_text`` / ``_inline_chunks``, with no adapter in the way."""

    def setUp(self):
        self.saved = _install_gateway_stub()
        self.addCleanup(self._restore)
        import google_chat_relay_patch

        self.patch = google_chat_relay_patch
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _restore(self):
        for name, module in self.saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def _write(self, name, data):
        path = Path(self.tmp.name) / name
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
        return str(path)

    def test_reads_a_text_deliverable(self):
        path = self._write("report.md", "# Title\n\nBody.\n")
        self.assertEqual(self.patch._inline_text(path), "# Title\n\nBody.\n")

    def test_declines_a_binary_extension(self):
        # The bytes are valid UTF-8; the extension alone must decide, because a
        # .pdf that happens to decode is still not a thing to paste in a thread.
        path = self._write("report.pdf", "not really a pdf")
        self.assertIsNone(self.patch._inline_text(path))

    def test_declines_over_the_cap(self):
        oversize = "x" * (self.patch.INLINE_MAX_BYTES + 1)
        self.assertIsNone(self.patch._inline_text(self._write("big.md", oversize)))

    def test_accepts_exactly_the_cap(self):
        atlimit = "x" * self.patch.INLINE_MAX_BYTES
        self.assertEqual(
            self.patch._inline_text(self._write("atlimit.md", atlimit)), atlimit
        )

    def test_declines_bytes_that_are_not_utf8(self):
        path = self._write("report.md", b"\xff\xfe\x00binary")
        self.assertIsNone(self.patch._inline_text(path))

    def test_declines_a_missing_file(self):
        missing = os.path.join(self.tmp.name, "gone.md")
        self.assertIsNone(self.patch._inline_text(missing))

    def test_chunks_stay_under_the_send_budget(self):
        text = "\n".join(f"line {n}" for n in range(4000))
        chunks = self.patch._inline_chunks(text, fenced=True)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            # 4000 is the adapter's own cap. A chunk at or over it would be
            # re-split by send() and the fence would be cut in half.
            self.assertLess(len(chunk), 4000)

    def test_every_fenced_chunk_carries_its_own_fence(self):
        text = "\n".join(f"line {n}" for n in range(4000))
        chunks = self.patch._inline_chunks(text, fenced=True)
        for chunk in chunks:
            self.assertTrue(chunk.startswith("```\n"))
            self.assertTrue(chunk.endswith("\n```"))

    def test_unfenced_chunks_reassemble_to_the_source(self):
        text = "\n".join(f"line {n}" for n in range(4000))
        chunks = self.patch._inline_chunks(text, fenced=False)
        self.assertEqual("\n".join(chunks), text)

    def test_splits_a_line_only_when_it_has_to(self):
        # No newline anywhere: an ugly cut is correct, losing the tail is not.
        text = "x" * (self.patch.INLINE_CHUNK_CHARS * 2 + 5)
        chunks = self.patch._inline_chunks(text, fenced=False)
        self.assertEqual("".join(chunks), text)


class InlineFallbackTest(unittest.TestCase):
    """The override installed on the adapter class by ``patch_adapter_class``."""

    def setUp(self):
        self.saved = _install_gateway_stub()
        self.addCleanup(self._restore)
        os.environ["GOOGLE_CHAT_RELAY_URL"] = "http://127.0.0.1:1"
        self.addCleanup(os.environ.pop, "GOOGLE_CHAT_RELAY_URL", None)
        sys.modules.pop("google_chat_relay_patch", None)
        import google_chat_relay_patch

        self.patch = google_chat_relay_patch
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _restore(self):
        for name, module in self.saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def _patched_adapter(self, send_results=None):
        """An adapter instance whose class has been through the relay patch.

        Goes through ``install()`` and the registry wrapper rather than calling
        ``patch_adapter_class`` directly, so the test exercises the path
        production takes.
        """
        adapter_class = make_adapter_class(send_results)
        registry = sys.modules["gateway.platform_registry"].PlatformRegistry
        registry.create_adapter = (
            lambda self, name, *a, **k: adapter_class() if name == "google_chat" else None
        )
        self.patch.install()
        adapter = registry().create_adapter("google_chat")
        self.assertTrue(
            getattr(adapter_class, "_credential_proxy_relay_patched", False)
        )
        return adapter

    def _write(self, name, data):
        path = Path(self.tmp.name) / name
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
        return str(path)

    def _fallback(self, adapter, path, filename, caption=None, thread_id=None):
        return asyncio.run(
            type(adapter)._post_attachment_fallback(
                adapter,
                chat_id="spaces/AAA",
                path=path,
                filename=filename,
                caption=caption,
                thread_id=thread_id,
            )
        )

    def test_pastes_a_markdown_report_instead_of_the_notice(self):
        adapter = self._patched_adapter()
        path = self._write("assessment.md", "# Design Assessment\n\nThe topology.\n")

        result = self._fallback(adapter, path, "assessment.md")

        self.assertTrue(result.success, "an inlined report is a delivered report")
        self.assertEqual(adapter.fallback_calls, [], "the notice must not also post")
        self.assertEqual(len(adapter.sent), 1)
        _chat_id, content, _metadata = adapter.sent[0]
        self.assertIn("assessment.md", content)
        self.assertIn("# Design Assessment", content)
        self.assertIn("The topology.", content)

    def test_names_the_file_and_its_size(self):
        adapter = self._patched_adapter()
        path = self._write("report.md", "x" * 2048)

        self._fallback(adapter, path, "report.md")

        content = adapter.sent[0][1]
        self.assertIn("**report.md**", content)
        self.assertIn("2.0 KB", content)

    def test_threads_the_paste_under_the_summary(self):
        adapter = self._patched_adapter()
        path = self._write("report.md", "body\n")

        self._fallback(adapter, path, "report.md", thread_id="spaces/AAA/threads/T")

        self.assertEqual(
            adapter.sent[0][2], {"thread_id": "spaces/AAA/threads/T"}
        )

    def test_leads_with_the_caption_when_there_is_one(self):
        adapter = self._patched_adapter()
        path = self._write("report.md", "body\n")

        self._fallback(adapter, path, "report.md", caption="Here is the report")

        self.assertTrue(adapter.sent[0][1].startswith("Here is the report"))

    def test_defers_to_the_notice_for_a_binary(self):
        adapter = self._patched_adapter()
        path = self._write("chart.png", b"\x89PNG\r\n\x1a\n")

        result = self._fallback(adapter, path, "chart.png")

        self.assertFalse(result.success)
        self.assertEqual(adapter.sent, [], "nothing is pasted for a binary")
        self.assertEqual(len(adapter.fallback_calls), 1)
        self.assertEqual(adapter.fallback_calls[0]["filename"], "chart.png")

    def test_defers_to_the_notice_over_the_cap(self):
        adapter = self._patched_adapter()
        path = self._write("huge.md", "x" * (self.patch.INLINE_MAX_BYTES + 1))

        result = self._fallback(adapter, path, "huge.md")

        self.assertFalse(result.success)
        self.assertEqual(len(adapter.fallback_calls), 1)

    def test_defers_to_the_notice_for_an_empty_file(self):
        # Nothing to read is not a delivery, and a message holding only a
        # filename header is worse than the notice that says where the file is.
        adapter = self._patched_adapter()
        path = self._write("empty.md", "   \n")

        result = self._fallback(adapter, path, "empty.md")

        self.assertFalse(result.success)
        self.assertEqual(len(adapter.fallback_calls), 1)

    def test_fences_structured_content(self):
        adapter = self._patched_adapter()
        path = self._write("findings.json", '{"a": 1}\n')

        self._fallback(adapter, path, "findings.json")

        self.assertIn('```\n{"a": 1}', adapter.sent[0][1])

    def test_marks_the_parts_of_a_multi_message_report(self):
        # ~8.9 KB: several chunks, but comfortably under the cap, so this
        # exercises chunking rather than the refusal path above it.
        adapter = self._patched_adapter()
        path = self._write("long.md", "\n".join(f"line {n}" for n in range(1000)))

        result = self._fallback(adapter, path, "long.md")

        self.assertTrue(result.success)
        self.assertGreater(len(adapter.sent), 1)
        self.assertIn("2 of ", adapter.sent[1][1])

    def test_an_adapter_without_the_hook_still_gets_the_transport(self):
        # A base image that renames _post_attachment_fallback must cost the
        # inlining and nothing else. Losing connect() here would lose Chat.
        adapter_class = make_adapter_class()
        del adapter_class._post_attachment_fallback
        registry = sys.modules["gateway.platform_registry"].PlatformRegistry
        registry.create_adapter = lambda self, name, *a, **k: adapter_class()

        with self.assertLogs("google-chat-relay-patch", level="WARNING"):
            self.patch.install()
            registry().create_adapter("google_chat")

        self.assertTrue(adapter_class._credential_proxy_relay_patched)
        self.assertTrue(callable(adapter_class.connect))
        self.assertFalse(hasattr(adapter_class, "_post_attachment_fallback"))

    def test_stops_at_the_first_refusal(self):
        # Posting the tail of a report whose head was refused leaves the reader
        # with a fragment they cannot tell is a fragment.
        adapter = self._patched_adapter(
            send_results=[FakeSendResult(success=False, error="rate limited")]
        )
        path = self._write("long.md", "\n".join(f"line {n}" for n in range(1000)))

        with self.assertLogs("google-chat-relay-patch", level="WARNING") as logs:
            result = self._fallback(adapter, path, "long.md")

        self.assertFalse(result.success)
        self.assertEqual(len(adapter.sent), 1)
        self.assertIn("long.md", logs.output[0])
        self.assertIn("rate limited", logs.output[0])


if __name__ == "__main__":
    unittest.main()
