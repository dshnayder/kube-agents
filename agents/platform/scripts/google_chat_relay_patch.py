"""Credential-free Google Chat transport for Hermes' bundled adapter."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import urllib.request
from typing import Any

from credential_proxy_client import authorization_headers


LOGGER = logging.getLogger("google-chat-relay-patch")

# Below: what makes a deliverable inlineable when it cannot be attached.
#
# Google Chat's ``media.upload`` rejects app authentication outright -- "This
# method doesn't support app authentication with a service account" -- so an
# install that reaches Chat through the credential proxy can never upload a
# native attachment, whatever it is granted. Upstream's answer is a notice
# naming the host path, which on this deployment is a path the person in the
# thread cannot reach; #999 is that notice arriving instead of a report someone
# asked for. The content is already on the agent's own disk and needs no
# credential to post, so the deliverable goes into the thread as text.

#: Extensions whose bytes are worth putting in a chat message. A deliverable
#: outside this set -- a PDF, a PNG, an archive -- has no text form, and the
#: notice upstream posts (patched to English and to this deployment's reality
#: by deploy/docker/patches/apply_google_chat_attachment_notice.py) is still the
#: right answer for it.
INLINE_SUFFIXES = frozenset(
    {".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".log"}
)

#: Bytes above which a file is left as a notice rather than pasted. 32 KiB is
#: ten messages at the chunk budget below; past that the thread stops being a
#: place anyone reads the report and becomes a place it is buried.
INLINE_MAX_BYTES = 32 * 1024

#: Characters per posted chunk. The adapter caps a Chat message at 4000 and
#: chunks anything longer itself, so this only has to leave room for the header
#: line and a code fence -- but it has to leave *enough*: a chunk that arrives
#: over 4000 once decorated is re-split by ``send``, which would cut a fence in
#: half and render the tail of the report as prose.
INLINE_CHUNK_CHARS = 3500

#: Extensions posted inside a code fence rather than as prose. Chat renders the
#: message body as markdown, which eats the underscores and asterisks in a JSON
#: blob or a log line; a markdown report, by contrast, is *meant* to be
#: rendered and reads worse fenced.
INLINE_FENCED_SUFFIXES = frozenset({".json", ".yaml", ".yml", ".csv", ".log"})


def _human_size(size: int) -> str:
    """``9.6 KB``-style size for the header line."""
    kib = 1024
    if size < kib:
        return f"{size} B"
    return f"{size / kib:.1f} KB"


def _inline_text(path: str) -> str | None:
    """The file's text if it is small enough and textual, else ``None``.

    ``None`` is the "leave it to the notice" answer and covers every way this
    can decline: the wrong extension, too many bytes, bytes that are not UTF-8
    after all, and a file that is not readable from this process. A caller that
    got ``None`` has learned only that inlining is not available -- never that
    the file is absent, which is the notice's business to report.
    """
    suffix = os.path.splitext(path)[1].lower()
    if suffix not in INLINE_SUFFIXES:
        return None
    try:
        with open(path, "rb") as handle:
            # One byte past the cap, so a file that grew between a stat and
            # this read is refused rather than silently truncated into chat.
            raw = handle.read(INLINE_MAX_BYTES + 1)
    except OSError:
        return None
    if len(raw) > INLINE_MAX_BYTES:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _inline_chunks(text: str, *, fenced: bool) -> list[str]:
    """Split ``text`` into message-sized pieces, fencing each one separately.

    Fencing per chunk rather than once around the whole report is the reason
    this does its own splitting instead of handing the text to ``send`` and
    letting the adapter chunk it: a fence opened in the first message and
    closed in the last leaves every message between them unfenced.

    Splits on a line boundary when there is one to split on, because a report
    cut mid-line reads as corrupted rather than as continued.
    """
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= INLINE_CHUNK_CHARS:
            head, remaining = remaining, ""
        else:
            cut = remaining.rfind("\n", 0, INLINE_CHUNK_CHARS)
            # No newline in the whole window: a minified blob or one very long
            # line. Cut at the budget -- an ugly break beats no delivery.
            if cut <= 0:
                cut = INLINE_CHUNK_CHARS
            head, remaining = remaining[:cut], remaining[cut:].lstrip("\n")
        chunks.append(f"```\n{head}\n```" if fenced else head)
    return chunks


def install() -> None:
    relay_url = os.getenv("GOOGLE_CHAT_RELAY_URL", "").rstrip("/")
    if not relay_url:
        return

    def request(path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        # The relay shares a listener with the credential broker, so it shares
        # the broker's authentication. Empty in the sidecar deployment.
        headers = {"Content-Type": "application/json", **authorization_headers()}
        req = urllib.request.Request(
            relay_url + path,
            data=body,
            headers=headers,
            method="GET" if body is None else "POST",
        )
        with urllib.request.urlopen(req, timeout=35) as response:
            return json.load(response)

    class RelayMessage:
        """Pub/Sub-shaped message that settles an opaque proxy receipt."""

        def __init__(self, event: dict[str, Any]) -> None:
            self.data = base64.b64decode(event["data"], validate=True)
            self.attributes = event.get("attributes") or {}
            self.message_id = str(event.get("messageId", ""))
            self._receipt = str(event["receipt"])
            self._settled = False

        def _settle(self, acknowledge: bool) -> None:
            if self._settled:
                return
            path = "/v1/chat/events/ack" if acknowledge else "/v1/chat/events/nack"
            request(path, {"receipt": self._receipt})
            self._settled = True

        def ack(self) -> None:
            self._settle(True)

        def nack(self) -> None:
            self._settle(False)

    class RemoteRequest:
        def __init__(
            self, resource: list[str], method: str, arguments: dict[str, Any]
        ) -> None:
            self.resource = resource
            self.method = method
            self.arguments = arguments

        def execute(self, **_kwargs: Any) -> Any:
            response = request(
                "/v1/chat/api",
                {
                    "resource": self.resource,
                    "method": self.method,
                    "arguments": self.arguments,
                },
            )
            return response.get("response")

    class RemoteResource:
        """googleapiclient discovery-resource-shaped remote facade."""

        def __init__(self, resource: list[str] | None = None) -> None:
            self.resource = resource or []

        def __getattr__(self, name: str) -> Any:
            if name.startswith("_"):
                raise AttributeError(name)

            def invoke(**arguments: Any) -> Any:
                if arguments:
                    return RemoteRequest(self.resource, name, arguments)
                return RemoteResource([*self.resource, name])

            return invoke

    async def relay_loop(self: Any) -> None:
        while not self._shutting_down:
            message: RelayMessage | None = None
            try:
                response = await asyncio.to_thread(request, "/v1/chat/events")
                event = response.get("event")
                if not event:
                    continue
                message = RelayMessage(event)
                await asyncio.to_thread(self._on_pubsub_message, message)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.warning("Google Chat relay receive failed", exc_info=True)
                if message is not None:
                    try:
                        await asyncio.to_thread(message.nack)
                    except Exception:
                        pass
                await asyncio.sleep(2)

    def patch_adapter_class(adapter_class: type[Any]) -> None:
        if getattr(adapter_class, "_credential_proxy_relay_patched", False):
            return
        async def connect(self: Any, *, is_reconnect: bool = False) -> bool:
            self._loop = asyncio.get_running_loop()
            self._shutting_down = False
            self._chat_api = RemoteResource()
            try:
                await asyncio.to_thread(self._thread_count_store.load)
            except Exception:
                LOGGER.warning("Google Chat thread state load failed", exc_info=True)
            self._bot_user_id = self._load_cached_bot_id()
            self._relay_task = asyncio.create_task(relay_loop(self))
            self._mark_connected()
            LOGGER.info("Google Chat connected through credential proxy relay")
            return True

        async def disconnect(self: Any) -> None:
            self._shutting_down = True
            task = getattr(self, "_relay_task", None)
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            self._chat_api = None
            self._mark_disconnected()

        def new_authed_http(self: Any) -> Any:
            return None

        async def setup_files(
            self: Any,
            chat_id: str,
            thread_id: str | None,
            raw_text: str,
            sender_email: str | None = None,
        ) -> bool:
            await self.send(
                chat_id,
                "File attachment setup is unavailable through the credential proxy.",
                metadata={"thread_id": thread_id} if thread_id else None,
            )
            return True

        # Absent on a base image that renamed or dropped the hook. Skipping is
        # the whole cost of that -- ``_send_file`` would be calling some other
        # name, so the override could never fire anyway -- whereas letting the
        # AttributeError out of here aborts ``create_adapter`` and takes
        # ``connect`` and the relay loop down with it, losing Chat entirely
        # over a cosmetic feature.
        original_fallback = getattr(adapter_class, "_post_attachment_fallback", None)

        async def post_attachment_fallback(
            self: Any,
            chat_id: str,
            path: str,
            filename: str,
            caption: str | None,
            thread_id: str | None,
        ) -> Any:
            """Paste a deliverable that cannot be attached, or defer to upstream.

            Reached from both of ``_send_file``'s give-up paths -- no user
            OAuth token, and a token the API refused -- which on this
            deployment is every attempt, since the relay holds no user
            credentials and ``/setup-files`` is stubbed out above.

            Returns ``success=True`` when the content lands. The value is what
            the notifier logs, and by then the person in the thread is holding
            the report; calling that a failed delivery would be a worse lie
            than calling a pasted report an attachment.

            Falls back to ``original_fallback`` -- the English, relay-aware
            notice the image's build-time patch leaves here -- for anything
            with no text form, anything too large to read in a thread, and any
            failure to read the bytes at all.
            """
            text = _inline_text(path)
            if text is None or not text.strip():
                return await original_fallback(
                    self,
                    chat_id=chat_id,
                    path=path,
                    filename=filename,
                    caption=caption,
                    thread_id=thread_id,
                )

            suffix = os.path.splitext(path)[1].lower()
            chunks = _inline_chunks(
                text, fenced=suffix in INLINE_FENCED_SUFFIXES
            )
            metadata = {"thread_id": thread_id} if thread_id else None
            size = _human_size(len(text.encode("utf-8")))

            result = None
            for index, chunk in enumerate(chunks):
                header = []
                if index == 0 and caption:
                    header.append(caption)
                if index == 0:
                    header.append(f"📄 **{filename}** ({size})")
                else:
                    header.append(
                        f"📄 **{filename}** "
                        f"({index + 1} of {len(chunks)})"
                    )
                result = await self.send(
                    chat_id, "\n\n".join([*header, chunk]), metadata=metadata
                )
                # Stop at the first refusal rather than posting the tail of a
                # report whose head never arrived.
                if result is not None and not getattr(result, "success", False):
                    LOGGER.warning(
                        "Google Chat inline delivery of %s failed at part "
                        "%d/%d: %s",
                        filename,
                        index + 1,
                        len(chunks),
                        getattr(result, "error", ""),
                    )
                    return result
            return result

        adapter_class.connect = connect
        adapter_class.disconnect = disconnect
        adapter_class._new_authed_http = new_authed_http
        adapter_class._handle_setup_files_command = setup_files
        if original_fallback is not None:
            adapter_class._post_attachment_fallback = post_attachment_fallback
        else:
            LOGGER.warning(
                "Google Chat adapter has no _post_attachment_fallback; "
                "deliverables will not be inlined"
            )
        adapter_class._credential_proxy_relay_patched = True

    from gateway.platform_registry import PlatformRegistry

    original_registry_create = PlatformRegistry.create_adapter
    if not getattr(PlatformRegistry, "_credential_proxy_relay_patched", False):

        # Forwarded blind past ``name``: this wrapper adds a side effect and
        # delegates, so upstream owns the signature. Restating one is how the
        # Slack relay's registry shim took every platform down when the base
        # image grew a new keyword-only argument (see slack_relay_patch).
        def create_adapter(self: Any, name: str, *args: Any, **kwargs: Any) -> Any:
            adapter = original_registry_create(self, name, *args, **kwargs)
            if name == "google_chat" and adapter is not None:
                patch_adapter_class(type(adapter))
            return adapter

        PlatformRegistry.create_adapter = create_adapter
        PlatformRegistry._credential_proxy_relay_patched = True
