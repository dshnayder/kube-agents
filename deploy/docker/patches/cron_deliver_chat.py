"""``deliver: "chat"`` — hand a cron job's report to the Chat Agent.

Installed at ``/opt/hermes/tools/cron_deliver_chat.py`` and wired into
``cron/scheduler.py::_deliver_result`` by
``deploy/docker/patches/apply_cron_deliver_chat.py``.

Why a delivery mode and not a prompt instruction
------------------------------------------------

The relay itself — the Chat Agent composing the message and the report being
stored against the thread it lands in — is
``docs/designs/cron-report-relay.md``. This module is only about *what triggers*
it.

The first cut triggered it from the job's prompt: call ``report_to_chat``, then
return ``[SILENT]``. That works for a roster shipped in the image, where the
prompt is reviewed alongside the job, and fails for everything else. A job the
user asks for at runtime ("watch that rollout every ten minutes and tell me
when it settles") is created through ``cronjob(action='create')`` with whatever
prompt the moment produced, so it carries no such contract — and its ``deliver``
then resolves to a Google Chat home channel this image cannot hand a child
profile, i.e. to nowhere. The result is a job that runs forever and is never
heard from, which is the exact failure the relay exists to end.

``deliver`` is the field that already means "where does the output go", it is a
free-form string every creation path can set (``create_job`` stores it verbatim;
the model tool's schema documents values rather than enumerating them), and the
scheduler consults it at fire time. Putting the relay behind a token there makes
delivery a property of the job instead of an instruction the model has to
remember, and one the model can set for a job it is asked to create.

``report_to_chat`` stays, for the case this cannot serve: an agent that wants to
report *during* a run, or to send something other than its final response.

The token is exclusive
----------------------

``deliver: "chat,slack"`` relays and does not also post to Slack. Fanning out
would mean the Chat Agent's rendering of the report and the specialist's raw
one arriving side by side, and only one of them is answerable. A job that wants
a specific channel as well as the relay is asking for two deliveries of one
finding; it should be two jobs, or the Chat Agent's own message should say
where else to look.

Failure falls back rather than swallowing
-----------------------------------------

If the relay cannot be reached — no ``SESSION_KV_API_KEY``, the Session KV
server down, a 500 — :func:`intercept_chat_delivery` rewrites ``deliver`` to
``all`` and lets the scheduler's ordinary path run. Nothing has been posted at
that point, so there is no double-delivery to avoid, and a watchdog that found
a real problem should not go quiet because the front door was busy. Where
``all`` also resolves to nothing the scheduler records its usual
``no delivery target resolved`` in ``last_delivery_error``, which is the
existing signal for "this job's output went nowhere".
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: The ``deliver`` token that routes a report through the Chat Agent.
CHAT_TOKEN = "chat"

#: What ``deliver`` becomes when the relay is unreachable. ``all`` rather than
#: the job's original value because the original value *is* ``chat`` — there is
#: nothing else recorded to fall back to.
FALLBACK_DELIVER = "all"

#: The Session KV server's relay route. Loopback: it runs in this Pod.
DEFAULT_RELAY_URL = "http://127.0.0.1:8699/v1/cron-reports"
RELAY_URL_ENV = "CRON_REPORT_RELAY_URL"

#: Every route on the Session KV server except ``/healthz`` needs this.
API_KEY_ENV = "SESSION_KV_API_KEY"

#: The route hands the relay to a background task and answers immediately, so
#: this bounds a connect/accept stall, not a Chat Agent turn.
RELAY_TIMEOUT_SECONDS = 10


def wants_chat_delivery(deliver: Any) -> bool:
    """True when ``deliver`` carries the ``chat`` token.

    Accepts the list form as well as the string one: ``create_job`` stores
    whatever it is given, and MCP clients have historically passed
    ``["chat"]``. ``cron/scheduler.py::_normalize_deliver_value`` exists for the
    same reason; this cannot call it, because the module that owns it imports
    this one.
    """
    if isinstance(deliver, (list, tuple)):
        parts = [str(part) for part in deliver]
    elif deliver is None:
        return False
    else:
        parts = str(deliver).split(",")
    return any(part.strip().lower() == CHAT_TOKEN for part in parts)


def profile_name() -> str:
    """The profile a cron child is running as, from its ``HERMES_HOME``.

    Named profiles live at ``<root>/profiles/<name>``, which is the shape
    ``profile_cron_tick.py`` hands the child. Anything else is the root home —
    the Chat Agent's own store — and reports as ``default``. Only used to name
    the relay session, so a wrong answer costs a thread, not a delivery.
    """
    home = Path(os.getenv("HERMES_HOME", "") or "/opt/data")
    return home.name if home.parent.name == "profiles" else "default"


def _relay_url() -> str:
    return (os.getenv(RELAY_URL_ENV, "") or "").strip() or DEFAULT_RELAY_URL


def relay(job: Dict[str, Any], content: str) -> Optional[str]:
    """POST one finished report. ``None`` on success, else why it failed.

    The failure strings are what the caller logs and what ends up in
    ``last_delivery_error``, so they name the condition and never the key.
    """
    api_key = (os.getenv(API_KEY_ENV, "") or "").strip()
    if not api_key:
        return (
            f"{API_KEY_ENV} is unset, so the Chat Agent relay cannot be "
            f"authenticated"
        )

    payload = json.dumps(
        {
            "job_id": str(job.get("id") or ""),
            "profile": profile_name(),
            "title": str(job.get("name") or ""),
            "report": content,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        _relay_url(),
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=RELAY_TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", None) or response.getcode()
            if status >= 300:
                return f"Chat Agent relay answered HTTP {status}"
    except urllib.error.HTTPError as exc:
        return f"Chat Agent relay answered HTTP {exc.code}"
    except Exception as exc:  # URLError, socket timeout, malformed URL
        return f"Chat Agent relay unreachable: {type(exc).__name__}: {exc}"
    return None


def _fallback(job: Any) -> Any:
    """``job`` with ``deliver`` rewritten to :data:`FALLBACK_DELIVER`.

    A copy, not an edit: the caller's dict is the one the scheduler goes on to
    persist through ``mark_job_run``, and a relay outage must not silently
    rewrite the roster.
    """
    if not isinstance(job, dict):
        return job
    replaced = dict(job)
    replaced["deliver"] = FALLBACK_DELIVER
    return replaced


def intercept_chat_delivery(
    job: Dict[str, Any], content: str
) -> Optional[Dict[str, Any]]:
    """The one call ``_deliver_result`` makes. Decides who delivers.

    Returns ``None`` when the report has been handed to the Chat Agent and the
    scheduler must not deliver it as well; otherwise the job dict to go on
    with — ``job`` itself when the job never asked for chat delivery, or a copy
    with ``deliver`` rewritten to :data:`FALLBACK_DELIVER` when the relay
    failed.

    A three-way answer through the return value rather than a flag because it
    keeps the patch to one anchor: the caller's next line already binds ``job``.
    Never raises — a delivery mode is not worth an exception escaping into the
    scheduler's per-job error path, where it would be recorded as the *run*
    having failed.
    """
    try:
        if not wants_chat_delivery(job.get("deliver")):
            return job
        error = relay(job, content)
        if error is None:
            logger.info(
                "Job '%s': report handed to the Chat Agent (deliver=%s)",
                job.get("id", "?"),
                CHAT_TOKEN,
            )
            return None
        logger.warning(
            "Job '%s': %s — falling back to deliver=%s",
            job.get("id", "?"),
            error,
            FALLBACK_DELIVER,
        )
        return _fallback(job)
    except Exception:
        logger.exception(
            "chat-delivery interception failed; falling back to deliver=%s",
            FALLBACK_DELIVER,
        )
        return _fallback(job)
