#!/usr/bin/env python3
"""Wire tools/cron_deliver_chat.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. Two anchored edits
in two files, each with an import trailer, adding ``chat`` as a cron delivery
mode.

Why the mode exists at all — and why the trigger is ``deliver`` rather than a
line in every job's prompt — is the module docstring of
``deploy/docker/patches/cron_deliver_chat.py``. This file documents only where
the edits land.

Anchor 1, ``cron/scheduler.py::_deliver_result``. The interception has to sit
above ``_resolve_delivery_targets``, because that call is what turns an
unrecognised ``deliver`` value into an empty target list and then into a
``no delivery target resolved`` error. The anchor carries the last line of the
function's docstring and its closing quotes: ``targets =
_resolve_delivery_targets(job)`` at four-space indentation occurs **twice** in
that file — here and in the singular ``_resolve_delivery_target`` immediately
below — so anchoring on the call alone is a guaranteed "found 2". The docstring
line is unique to this one.

Anchor 2, ``tools/cronjob_tools.py::_local_delivery_notice``. Cosmetic in the
sense that nothing is delivered differently, load-bearing in the sense that it
is addressed to a model. That helper answers "did this job get a real delivery
target?" by calling ``_resolve_delivery_targets``, which cannot see the relay —
so a job created with ``deliver='chat'`` comes back with a notice telling the
agent the job will never be heard from and to recreate it with ``deliver='all'``
instead. An agent that follows that advice undoes the mode on the job it was
just asked to create. The anchor is the helper's existing early return for an
explicit ``local``, which is the same shape of "the caller asked for this, it is
not a surprise" exemption.

Ordering. Must run AFTER ``apply_cron_tick_lock_scope.py`` and
``apply_cron_skip_ledger.py``, which also edit ``cron/scheduler.py``: the
anchors were derived from the fully-patched tree. Neither touches
``_deliver_result`` or ``_local_delivery_notice``, so the edits are disjoint —
the ordering rule is about deriving against the tree the build produces, not
about a known collision.

Usage::

    python3 apply_cron_deliver_chat.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

SCHEDULER = "cron/scheduler.py"
CRONJOB_TOOLS = "tools/cronjob_tools.py"

# --- Anchor 1: the delivery fork ---------------------------------------------

DELIVER_ANCHOR = (
    "    Returns None on success, or an error string on failure.\n"
    '    """\n'
    "    targets = _resolve_delivery_targets(job)\n"
)

# `job` is rebound rather than a flag being set, so the fallback path needs no
# second edit: everything below this point already reads the rewritten value.
DELIVER_PATCHED = (
    "    Returns None on success, or an error string on failure.\n"
    '    """\n'
    "    # kube-agents patch: `deliver: \"chat\"` hands the finished report to\n"
    "    # the Chat Agent, which posts it and owns the conversation that\n"
    "    # follows. None means it was accepted and this function is done; a\n"
    "    # dict means deliver normally, with `deliver` rewritten to the\n"
    "    # fallback if the relay could not be reached. See\n"
    "    # tools/cron_deliver_chat.py.\n"
    "    job = _ka_intercept_chat_delivery(job, content)\n"
    "    if job is None:\n"
    "        return None\n"
    "\n"
    "    targets = _resolve_delivery_targets(job)\n"
)

SCHEDULER_TRAILER = (
    "\n\n# kube-agents patch: see tools/cron_deliver_chat.py\n"
    "from tools.cron_deliver_chat import (  # noqa: E402\n"
    "    intercept_chat_delivery as _ka_intercept_chat_delivery,\n"
    ")\n"
)

#: Text that only exists after a successful run. The anchor is consumed by its
#: own replacement, so a second pass fails on "found 0" — but that message
#: blames upstream drift for what is really a duplicated build step, and it
#: would fire only after the trailer had been appended twice.
SCHEDULER_SENTINELS = (
    "job = _ka_intercept_chat_delivery(job, content)",
    "from tools.cron_deliver_chat import",
)

# --- Anchor 2: the misleading local-only notice ------------------------------
#
# The em dash is upstream's, in a comment. Kept verbatim: an anchor is a literal.

NOTICE_ANCHOR = (
    "    # An explicit local request is exactly what the user asked for — no notice.\n"
    '    if (user_deliver or "").strip().lower() == "local":\n'
    "        return None\n"
)

NOTICE_PATCHED = (
    "    # An explicit local request is exactly what the user asked for — no notice.\n"
    '    if (user_deliver or "").strip().lower() == "local":\n'
    "        return None\n"
    "    # kube-agents patch: nor is `deliver: \"chat\"`, which delivers through\n"
    "    # the Chat Agent — a target _resolve_delivery_targets cannot see, so\n"
    "    # without this the notice would tell the agent to undo the mode. See\n"
    "    # tools/cron_deliver_chat.py.\n"
    "    if _ka_wants_chat_delivery(user_deliver):\n"
    "        return None\n"
)

CRONJOB_TOOLS_TRAILER = (
    "\n\n# kube-agents patch: see tools/cron_deliver_chat.py\n"
    "from tools.cron_deliver_chat import (  # noqa: E402\n"
    "    wants_chat_delivery as _ka_wants_chat_delivery,\n"
    ")\n"
)

CRONJOB_TOOLS_SENTINELS = (
    "_ka_wants_chat_delivery(user_deliver)",
    "from tools.cron_deliver_chat import",
)


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    scheduler = patchlib.Patch(root, SCHEDULER, prefix="cron_deliver_chat")
    scheduler.refuse_if_patched(*SCHEDULER_SENTINELS)
    scheduler.substitute(DELIVER_ANCHOR, DELIVER_PATCHED, label="delivery fork")
    scheduler.append(SCHEDULER_TRAILER)
    scheduler.commit("1 anchor")

    tools = patchlib.Patch(root, CRONJOB_TOOLS, prefix="cron_deliver_chat")
    tools.refuse_if_patched(*CRONJOB_TOOLS_SENTINELS)
    tools.substitute(NOTICE_ANCHOR, NOTICE_PATCHED, label="local-only notice")
    tools.append(CRONJOB_TOOLS_TRAILER)
    tools.commit("1 anchor")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
