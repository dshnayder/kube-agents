import json
import logging
import os
import urllib.request
from urllib.parse import urlencode

SESSION_KV_URL = "http://127.0.0.1:8699"

logger = logging.getLogger(__name__)

def register(ctx):
    ctx.register_hook("pre_gateway_dispatch", on_inbound)

def on_inbound(*, event, **_):
    src = event.source
    platform = getattr(src.platform, "value", str(src.platform))
    logger.debug("platform=%s, chat_id=%s, thread_id=%s", platform, getattr(src, 'chat_id', None), getattr(src, 'thread_id', None))
    chat_id = getattr(src, "chat_id", None)
    if platform not in ("google_chat", "slack") or not chat_id:
        return None
    # A slash command is addressed to the gateway, not to the incident. Both
    # this hook and `legacy_slash_commands` are `pre_gateway_dispatch`, and
    # whichever rewrites first decides what the other one sees: prepending the
    # triage report moves `/hermes sethome` off the front of the line, so the
    # unwrap never matches and the gateway reads the whole thing as prose. The
    # user gets a paragraph of last week's incident instead of their command,
    # inside the one thread where they are most likely to be running one.
    if (getattr(event, "text", "") or "").lstrip().startswith("/"):
        return None
    thread_id = getattr(src, "thread_id", None)
    report = _lookup(chat_id, thread_id) if thread_id else None
    if not report:
        report = _recover_bot_thread(src, platform, chat_id, thread_id, _raw_thread(event))
    if report:
        # Deliberately not "k8s incident report". The `incidents` table has two
        # writers -- the event watcher, which does store incidents, and the cron
        # report relay, which stores a scheduled report from a job where nothing
        # broke. Naming the wrong one costs a real answer: told a smoke-test
        # report was an incident, the agent opened its reply by correcting the
        # framing ("It's not an incident report - nothing broke") before
        # answering what was asked. The table name is history; this string is
        # read by a model.
        new_text = (
            "[Prior report posted in this thread - use it to interpret the reply below]\n"
            f"{report}\n\n"
            f"[User reply in thread]: {event.text}"
        )
        return {"action": "rewrite", "text": new_text}
    # Nothing is keyed to this message, and on the two paths that matter nothing
    # ever will be: a Google Chat reply typed into the main compose box arrives
    # with no thread_id at all, and a top-level Slack channel message arrives
    # carrying its own ts as thread_id, which matches no stored report. Both
    # leave the agent looking at a bare sentence while the reports sit in the
    # channel above it. It does not degrade to "I lack context" -- it binds to
    # the nearest antecedent in its own history and answers confidently about
    # the wrong one. Naming what exists turns that into a question.
    recent = _lookup_recent(chat_id)
    if not recent:
        return None  # nothing posted here lately -> leave the message untouched
    return {"action": "rewrite", "text": _index_text(recent, event.text)}

def _raw_thread(event):
    """The thread Google Chat says this message is in, before the adapter's edit.

    `raw_message` is the Chat API message resource the adapter parsed, so
    `thread.name` here is the thread the user actually typed into even when the
    normalized source says None. Any other platform's raw message is a different
    shape and simply misses; the caller gates on google_chat regardless.
    """
    raw = getattr(event, "raw_message", None)
    if not isinstance(raw, dict):
        return None
    thread = raw.get("thread")
    if not isinstance(thread, dict):
        return None
    return thread.get("name") or None

def _recover_bot_thread(src, platform, chat_id, thread_id, raw_thread):
    """Re-attach the thread a relayed report created, and return that report.

    Google Chat opens a thread around *every* top-level message, so an inbound
    payload cannot say whether the user posted at top level or replied inside a
    real thread. The adapter settles it by counting: a thread it has never seen
    an inbound message in is treated as "main flow", and the bot answers at top
    level rather than in the thread (`plugins/platforms/google_chat/adapter.py`,
    the `_ThreadCountStore` heuristic). The counter is fed from two places, and a
    relayed report reaches neither -- the report is posted by `hermes send` from
    the Session KV server, a different process from the gateway, so it never
    passes through the adapter's outbound path. A report thread therefore stands
    at zero however long it sits there, and the first reply typed into it is read
    as a new top-level message: the answer lands in the space, detached from the
    question, and starts a second session besides. The reply after that works,
    because by then the counter has the user's own first message in it. That is
    the shape of the bug -- only ever the first follow-up, only in a DM.

    A stored report keyed to `raw_thread` is the missing evidence. The relay
    writes that row when it posts, so a hit means the bot opened this thread and
    the user has deliberately replied inside it -- which is exactly the condition
    the counter was trying to detect. Nothing else here overrides the adapter:
    with no report for the thread this returns None and the heuristic stands.

    Assigning `src.thread_id` is what moves the answer. `SessionSource` is a
    plain dataclass and the gateway keeps the same instance across the
    `dataclasses.replace(event, text=...)` it does for a rewrite, so downstream
    reads see the assignment: the session key gains the thread (`gateway/session.py`)
    and the outbound send carries `metadata={"thread_id": ...}`, which is the
    first thing google_chat's `_resolve_thread_id` looks at. The reply that then
    goes out through the adapter registers the thread on the way, so every later
    message in it routes without any help from here.

    Groups need none of this -- the adapter always keeps their thread_id, so
    `raw_thread` matches what the source already carries and this is a no-op.
    """
    if platform != "google_chat" or not raw_thread or raw_thread == thread_id:
        return None
    report = _lookup(chat_id, raw_thread)
    if not report:
        return None
    src.thread_id = raw_thread
    logger.info(
        "Re-attached bot-created thread %s in %s; the reply would have gone to the space",
        raw_thread, chat_id,
    )
    return report

def _index_text(reports, text):
    """Render the index. Labels only -- never a line of report text.

    The server returns no report body on purpose (see `list_recent_reports`),
    and this block is prepended to *every* unthreaded message in the space, so
    the rule has to hold here too: only fields the platform agent wrote itself.
    """
    lines = []
    for report in reports:
        label = report.get("job_id") or "scheduled report"
        title = report.get("title")
        if title and title != label:
            label = f'{label} "{title}"'
        profile = report.get("profile")
        if profile:
            label = f"{label} ({profile} agent)"
        # SQLite writes "2026-08-17 14:40:16"; an ISO string from the relay row
        # would be "2026-08-17T14:40:16". Both cut to the minute the same way.
        when = (report.get("created_at") or "").replace("T", " ")[:16]
        if when:
            label = f"{label} - {when} UTC"
        lines.append(f"- {label}")
    return (
        "[No report is attached to this message. Scheduled reports posted in this "
        "space recently, most recent first. You do NOT have their contents. If the "
        "user is asking about one of these, ask which one they mean - do not answer "
        "from memory, and do not guess.]\n"
        + "\n".join(lines)
        + f"\n\n[User message]: {text}"
    )

def _lookup(chat_id, thread_id):
    payload = _get("/v1/incidents/by-thread?" + urlencode({"chat_id": chat_id, "thread_id": thread_id}))
    return (payload or {}).get("report")

def _lookup_recent(chat_id):
    payload = _get("/v1/incidents/recent?" + urlencode({"chat_id": chat_id}))
    return (payload or {}).get("reports") or []

def _get(path):
    # The Session KV server now authenticates every data route. An unset key
    # yields a 401 that the except below swallows, which is the same fail-open
    # this lookup already had for a server that is down.
    headers = {}
    token = (os.environ.get("SESSION_KV_API_KEY") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"{SESSION_KV_URL}{path}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=2) as r:
            if r.status == 200:
                return json.load(r)
    except Exception:
        pass  # fail-open: never break normal message flow
    return None
