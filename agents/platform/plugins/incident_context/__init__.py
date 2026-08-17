import json
import os
import urllib.request
from urllib.parse import urlencode

SESSION_KV_URL = "http://127.0.0.1:8699"

def register(ctx):
    ctx.register_hook("pre_gateway_dispatch", on_inbound)

def on_inbound(*, event, **_):
    src = event.source
    platform = getattr(src.platform, "value", str(src.platform))
    import logging
    logger = logging.getLogger(__name__)
    logger.info("platform=%s, chat_id=%s, thread_id=%s", platform, getattr(src, 'chat_id', None), getattr(src, 'thread_id', None))
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
