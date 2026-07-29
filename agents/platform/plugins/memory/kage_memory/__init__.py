"""Two-bank memory for the Chat Agent: one private bank per user, one common bank.

Hermes loads exactly one external memory provider (``MemoryManager.add_provider``
rejects a second non-builtin), and a Hindsight provider instance binds to exactly
one bank — ``_bank_id`` is resolved once in ``initialize()`` and every escape
hatch is closed: ``handle_tool_call`` pops a per-item ``bank_id`` before retain,
``RECALL_SCHEMA`` exposes only ``query``, and ``recall_tags`` gets no placeholder
resolution. So "a bank per user AND a bank for everyone" cannot be expressed in
Hindsight's own configuration.

This provider is the wrapper that makes it possible. It holds *two* stock
Hindsight instances and fans the provider protocol across both:

* **personal** — ``bank_id_template`` from config, so the bank name carries the
  gateway ``user_id``. Auto-recalled and auto-retained.
* **shared** — pinned to a single bank name. Auto-recalled so team facts reach
  every conversation, but never auto-retained: it takes explicit writes only,
  because everything in it is visible to every user.

The two are not symmetric in size. The shared bank is the org's corpus — SOPs,
conventions, release history — and is expected to grow large; the personal bank
holds a handful of facts about one person. ``shared_recall_budget`` and
``personal_recall_budget`` let each side set its own recall depth accordingly.

Each bank also needs a *mission*: what it is for, and what is worth extracting
into it. Hindsight's ``bank_mission``/``bank_retain_mission`` config keys are
read into attributes the plugin never uses, so a mission can only be set on the
bank itself, through the API — and this provider is the one that sets it, for
both banks, in ``_ensure_mission``. A bank that has never been written to does
not exist yet, so there is no install step and nothing to remember: the first
session to touch a bank provisions it. Delete a bank, or rebuild Hindsight's
database, and the next session puts the mission back.

Both are loaded through ``load_memory_provider("hindsight")``, which re-runs the
plugin's ``register()`` per call and therefore returns independent instances.
Reusing the upstream provider wholesale — rather than forking its ~92 KB
implementation — is deliberate: a Hermes base-image bump brings Hindsight fixes
along with it and there is no merge to redo.
"""

import json
import logging
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from plugins.memory import load_memory_provider
from tools.registry import tool_error

logger = logging.getLogger(__name__)

PROVIDER_NAME = "kage_memory"
DEFAULT_SHARED_BANK_ID = "kage-shared"

PERSONAL_PREAMBLE = (
    "# Your Memory of This User (private to them)\n"
    "Durable facts about the person you are talking to, recalled from previous "
    "sessions. Use them to answer directly and to resolve possessives before "
    "delegating. Do not look these up with a tool — they are already here."
)
SHARED_PREAMBLE = (
    "# Shared Team Memory (visible to everyone)\n"
    "Facts the organisation has recorded as true for everybody. These are not "
    "specific to the current user."
)

SHARED_SESSION_NOTICE = (
    "Personal memory is unavailable in this conversation. It is a shared thread "
    "that more than one person can post in, and the harness cannot attribute a "
    "message to its sender here, so nothing may be read from or written to a "
    "personal bank. Shared memory still works; personal memory works in a direct "
    "message."
)
NO_IDENTITY_NOTICE = (
    "Personal memory is unavailable because this session carries no user "
    "identity. Only shared memory is reachable here."
)

_SCOPES = ("personal", "shared", "both")
_VALID_BUDGETS = ("low", "mid", "high")

# What a personal bank is for, and what is worth keeping in it.
#
# Without these a personal bank comes up with an empty mission and Hindsight
# extracts whatever seemed notable in the transcript — which in practice means
# the state of individual kanban cards and the assistant's own bookkeeping.
# Those read as facts but expire when the work closes, and they keep being
# injected into the prompt long after they stop being true.
PERSONAL_MISSION = (
    "What one person's assistant needs to remember about them: where they work "
    "and in which timezone, which clusters, projects and environments are "
    "theirs, how they prefer work to be done, and what they are responsible "
    "for. Everything here is about this individual. Facts that hold for the "
    "whole team belong in the shared bank instead."
)

PERSONAL_RETAIN_MISSION = (
    "Extract only durable facts about this person. Keep their location, "
    "timezone, role and responsibilities; the clusters, projects and "
    "environments they call their own; and the working preferences they state. "
    "Phrase each fact to stand alone and name the person rather than saying "
    "'the user'. "
    "Drop the state of individual tasks and tickets, decisions scoped to one "
    "piece of work, and anything the assistant itself did — that is a record of "
    "a conversation, not a fact about a person, and it stops being true once "
    "the work closes. Drop anything that holds for the whole team; that belongs "
    "in the shared bank."
)

# What the shared bank is for, and how to answer from it.
#
# One field, not two. Hindsight's `set_mission` and `set_reflect_mission` are
# both deprecated aliases for `create_bank(bank_id, mission=...)`, so a bank has
# a single `mission` and it is what reflect reasons against; the text has to say
# what the bank holds *and* how to answer from it. `retain_mission` below is
# genuinely separate — it shapes extraction, not retrieval.
SHARED_MISSION = (
    "The shared operational knowledge of this organisation's Kubernetes "
    "platform team, readable by every user of the Kage agent. It holds "
    "standard operating procedures, platform conventions and defaults, "
    "on-call and timezone facts, cluster and environment inventory, and the "
    "history of releases and infrastructure changes. Facts here are true for "
    "everybody. Nothing about an individual person belongs in this bank: "
    "personal preferences, one user's clusters, and anything phrased about "
    "'me' or 'my' belong in that user's own bank instead. "
    "When answering, cite the specific procedure, version or dated change that "
    "supports the answer, and say plainly when the record does not cover the "
    "question rather than generalising from adjacent facts."
)

# The shared bank is loaded from documents, not conversation. Hindsight's
# default extraction assumes dialogue and keeps asides that read as commitments.
SHARED_RETAIN_MISSION = (
    "Extract durable, self-contained operational facts. Each fact must stand "
    "alone without the surrounding document: name the cluster, environment, "
    "component, version or date it is about rather than saying 'this' or "
    "'the above'. Keep procedures, thresholds, ownership, defaults, and dated "
    "changes. Preserve exact identifiers, versions and dates verbatim. Drop "
    "narrative framing, TODOs, unresolved proposals, and anything true only "
    "while a document was being written."
)

# Banks whose mission this process has already settled, so the common case costs
# nothing. Deliberately per-process rather than persisted: it is only a cache,
# and re-applying after a restart is idempotent.
_missions_applied: set = set()

# Written here rather than fanned out from the sub-providers: theirs name the
# hindsight_* tools, which this provider does not expose, and two of them would
# arrive describing two banks as if each were the only one.
SYSTEM_PROMPT_HEADER = "# Memory"
SYSTEM_PROMPT_BODY = (
    "You have two memory banks. Relevant entries from both are injected into "
    "your context automatically each turn and are retained automatically — in "
    "the normal case you do not call a tool at all.\n"
    "- **Personal** ({personal}) — private to the person you are talking to.\n"
    "- **Shared** ({shared}) — visible to everyone in the organisation.\n"
    "\n"
    "`memory_recall` searches and `memory_reflect` synthesises across both by "
    "default; use them only when the injected memories are not enough. "
    "`memory_retain` writes to personal memory unless you pass "
    "`scope: \"shared\"`, which you should do only for facts that are true for "
    "everybody, never for one person's preferences."
)
SYSTEM_PROMPT_SHARED_ONLY = (
    "You have one memory bank: **shared** ({shared}), visible to everyone in "
    "the organisation. Relevant entries are injected into your context "
    "automatically. `memory_recall` and `memory_reflect` search it; "
    "`memory_retain` writes to it."
)


def _thread_sessions_are_per_user() -> bool:
    """Best-effort read of the gateway's ``thread_sessions_per_user`` setting.

    The gateway accepts the key at the top level of config.yaml or under
    ``gateway:`` (gateway/config.py). Upstream default is False.
    """
    try:
        from hermes_cli.config import load_config
        config = load_config() or {}
        for section in (config, config.get("gateway")):
            if isinstance(section, dict) and "thread_sessions_per_user" in section:
                return bool(section["thread_sessions_per_user"])
    except Exception as e:
        logger.debug("Could not read thread_sessions_per_user, assuming shared: %s", e)
    return False


class KageMemoryProvider(MemoryProvider):
    """A private Hindsight bank per user, plus one common bank for everyone."""

    def __init__(self) -> None:
        self._personal: Optional[MemoryProvider] = None
        self._shared: Optional[MemoryProvider] = None
        self._personal_disabled_reason: str = ""
        self._session_id: str = ""

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    def is_available(self) -> bool:
        # Hermes asks this *before* initialize() and drops the provider outright
        # if it says no (agent_init.py:1460), so it has to be answerable with no
        # banks built yet. Hindsight's own answer is stateless — it reads
        # $HERMES_HOME/hindsight/config.json — so an uninitialised instance
        # gives the same verdict the real banks would.
        banks = [p for p in (self._personal, self._shared) if p is not None]
        if banks:
            return any(p.is_available() for p in banks)
        probe = load_memory_provider("hindsight")
        return bool(probe is not None and probe.is_available())

    # -- lifecycle -----------------------------------------------------------

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self._session_id = str(session_id or "").strip()
        self._personal = None
        self._shared = None
        self._personal_disabled_reason = ""

        user_id = str(kwargs.get("user_id") or "").strip()

        # Refuse the personal bank when the session can carry more than one human.
        #
        # agent._user_id is frozen once at Agent construction, and
        # build_session_key() (gateway/session.py) deliberately omits the
        # participant id inside a thread unless `thread_sessions_per_user` is on.
        # So in a shared thread the second speaker reuses the first speaker's
        # cached Agent, and a per-user bank would recall person A's memories into
        # person B's prompt and retain B's turns under A's name. Nothing in the
        # provider protocol identifies the speaker — system_prompt_block() takes
        # no arguments and handle_tool_call() is passed no identity — so it fails
        # closed. The shared bank, visible to everyone by design, is unaffected.
        chat_type = str(kwargs.get("chat_type") or "").strip().lower()
        session_is_shared = bool(
            chat_type
            and chat_type != "dm"
            and kwargs.get("thread_id")
            and not _thread_sessions_are_per_user()
        )

        if session_is_shared:
            self._personal_disabled_reason = SHARED_SESSION_NOTICE
            logger.info(
                "%s: personal bank disabled for session %s (shared %s thread — "
                "sender cannot be attributed)", PROVIDER_NAME, session_id, chat_type,
            )
        elif not user_id:
            # An unresolved {user} placeholder renders an empty segment rather
            # than falling back to the static bank_id, which would quietly pool
            # every anonymous caller into one real bank. Fail closed instead.
            self._personal_disabled_reason = NO_IDENTITY_NOTICE
            logger.info(
                "%s: personal bank disabled for session %s (no user identity)",
                PROVIDER_NAME, session_id,
            )
        else:
            self._personal = self._init_hindsight(
                session_id, kwargs, preamble=PERSONAL_PREAMBLE, label="personal",
            )
            self._apply_budget(self._personal, "personal_recall_budget")
            self._ensure_mission(
                self._personal, PERSONAL_MISSION, PERSONAL_RETAIN_MISSION, "personal",
            )

        self._shared = self._init_hindsight(
            session_id, kwargs, preamble=SHARED_PREAMBLE, label="shared",
        )
        if self._shared is not None:
            # Pin to one bank for everyone: drop the template so the resolved
            # per-user name is replaced by the static bank id. Read at call time
            # by retain/recall/prefetch, and initialize() creates no bank eagerly
            # in local_external mode, so overriding it here is sufficient.
            shared_bank = self._shared_bank_id(self._shared)
            self._shared._bank_id_template = ""
            self._shared._bank_id = shared_bank
            # Everything in this bank is visible to every user, so it takes
            # explicit writes only — never the end-of-session fact extraction.
            self._shared._auto_retain = False
            self._apply_budget(self._shared, "shared_recall_budget")
            self._ensure_mission(
                self._shared, SHARED_MISSION, SHARED_RETAIN_MISSION, "shared",
            )
            logger.info("%s: shared bank=%s (budget=%s)", PROVIDER_NAME, shared_bank,
                        getattr(self._shared, "_budget", "?"))

        if self._personal is None and self._shared is None:
            logger.warning("%s: no memory banks available for session %s",
                           PROVIDER_NAME, session_id)

    @staticmethod
    def _shared_bank_id(provider: MemoryProvider) -> str:
        """Resolve the common bank's name from the Hindsight config.

        ``shared_bank_id`` wins; otherwise the static ``bank_id``, which is
        Hindsight's own template fallback and is not otherwise reachable once a
        ``bank_id_template`` is set.
        """
        config = getattr(provider, "_config", None) or {}
        for key in ("shared_bank_id", "bank_id"):
            value = str(config.get(key) or "").strip()
            if value:
                return value
        return DEFAULT_SHARED_BANK_ID

    @staticmethod
    def _apply_budget(provider: Optional[MemoryProvider], key: str) -> None:
        """Give each bank its own recall thoroughness.

        Hindsight resolves a single ``recall_budget`` per instance into
        ``_budget`` and reads that attribute at call time on every recall and
        reflect, so overriding it here is enough. The two banks want different
        values: the shared bank holds the org-wide corpus — SOPs, conventions,
        release history — and earns a deeper search, while the personal bank is
        a handful of facts about one person and does not.

        Unset leaves Hindsight's own resolution (``mid`` by default) in place.
        """
        if provider is None:
            return
        config = getattr(provider, "_config", None) or {}
        value = str(config.get(key) or "").strip().lower()
        if value in _VALID_BUDGETS:
            provider._budget = value

    @staticmethod
    def _ensure_mission(
        provider: Optional[MemoryProvider], mission: str, retain_mission: str, label: str,
    ) -> None:
        """Provision a bank's editorial guidance, creating the bank if needed.

        Neither bank can be seeded ahead of time. A Hindsight bank does not exist
        until something is written to it, and personal banks are named after the
        user, so no install step could enumerate them. Doing it here means the
        first session to touch a bank provisions it, a deleted bank comes back
        correctly, and there is no manual step for an operator to forget.

        ``retain_mission`` is the sentinel for "already done": the bank-level
        ``mission`` is not part of the ``get_bank_config`` payload (that returns
        ``{bank_id, config}``, and mission is bank metadata), so it cannot be
        compared cheaply. The two are always written together, which makes one a
        sound proxy for the other.

        Costs one read per bank per process and two writes only when the text has
        actually changed. Failures are logged and swallowed — an unguided bank is
        worse than a guided one, but it still works, and memory must never be the
        reason a session fails to start.
        """
        if provider is None:
            return
        bank_id = str(getattr(provider, "_bank_id", "") or "").strip()
        if not bank_id or bank_id in _missions_applied:
            return
        # Recorded before the attempt, not after: if the API is down, every
        # subsequent session in this process would otherwise retry a call that is
        # already known to be failing, on the session-creation path.
        _missions_applied.add(bank_id)
        try:
            client = provider._get_client()
            config = (client.get_bank_config(bank_id) or {}).get("config") or {}
            if config.get("retain_mission") == retain_mission:
                return
            # create_bank doubles as the update path — it is what Hindsight's own
            # deprecated set_mission() calls — and leaves existing facts intact.
            # It must come first: it is the call that creates the bank, and
            # update_bank_config only edits one that exists.
            client.create_bank(bank_id=bank_id, mission=mission)
            client.update_bank_config(bank_id, retain_mission=retain_mission)
            logger.info("%s: provisioned mission on %s bank %s",
                        PROVIDER_NAME, label, bank_id)
        except Exception as e:
            logger.warning("%s: could not set the mission on %s bank %s: %s",
                           PROVIDER_NAME, label, bank_id, e)

    def _init_hindsight(
        self, session_id: str, kwargs: Dict[str, Any], *, preamble: str, label: str,
    ) -> Optional[MemoryProvider]:
        provider = load_memory_provider("hindsight")
        if provider is None:
            logger.warning("%s: could not load the hindsight provider for the %s bank",
                           PROVIDER_NAME, label)
            return None
        try:
            provider.initialize(session_id, **kwargs)
        except Exception as e:
            logger.warning("%s: hindsight initialize failed for the %s bank: %s",
                           PROVIDER_NAME, label, e)
            return None
        # Label each bank's recalled block so the model can tell whose fact it is
        # reading. Both banks flow into one prompt.
        provider._recall_prompt_preamble = preamble
        return provider

    def shutdown(self) -> None:
        self._each(lambda p: p.shutdown())

    # -- context -------------------------------------------------------------

    def system_prompt_block(self) -> str:
        if self._shared is None:
            return ""
        shared_bank = getattr(self._shared, "_bank_id", DEFAULT_SHARED_BANK_ID)
        if self._personal is None:
            body = SYSTEM_PROMPT_SHARED_ONLY.format(shared=shared_bank)
            reason = self._personal_disabled_reason
            return f"{SYSTEM_PROMPT_HEADER}\n{body}" + (f"\n\n{reason}" if reason else "")
        body = SYSTEM_PROMPT_BODY.format(
            personal=getattr(self._personal, "_bank_id", "per-user"),
            shared=shared_bank,
        )
        return f"{SYSTEM_PROMPT_HEADER}\n{body}"

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        self._each(lambda p: p.queue_prefetch(query, session_id=session_id))

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        blocks = [
            block for block in
            (self._call(p, "prefetch", query, session_id=session_id)
             for p in self._banks())
            if block
        ]
        return "\n\n".join(blocks)

    def on_turn_start(self, turn_number: int, message: str, **kwargs: Any) -> None:
        self._each(lambda p: p.on_turn_start(turn_number, message, **kwargs))

    def on_session_switch(self, new_session_id: str, **kwargs: Any) -> None:
        self._session_id = str(new_session_id or "").strip()
        self._each(lambda p: p.on_session_switch(new_session_id, **kwargs))

    # -- retention -----------------------------------------------------------
    #
    # Automatic capture goes to the personal bank only. The shared bank holds
    # organisation-wide facts and is read by everyone, so it must never absorb
    # one conversation's contents wholesale.

    def sync_turn(self, user_content: str, assistant_content: str, **kwargs: Any) -> None:
        if self._personal is not None:
            self._call(self._personal, "sync_turn", user_content, assistant_content, **kwargs)

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        if self._personal is not None:
            self._call(self._personal, "on_session_end", messages)

    # -- tools ---------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        scope_read = {
            "type": "string",
            "enum": list(_SCOPES),
            "description": (
                "Which memory to search: 'personal' (this user only), 'shared' "
                "(facts everyone sees), or 'both'. Defaults to 'both'."
            ),
        }
        scope_write = {
            "type": "string",
            "enum": ["personal", "shared"],
            "description": (
                "Where to store it. 'personal' is private to this user and is the "
                "default. Use 'shared' ONLY for a fact the user states as true for "
                "the whole team or organisation — every user can read it."
            ),
        }
        return [
            {
                "name": "memory_retain",
                "description": (
                    "Store a durable fact in long-term memory immediately. Routine "
                    "facts are captured automatically at the end of a session; use "
                    "this when the fact is needed sooner or the user asks you to "
                    "remember it."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "The fact to store, phrased to stand on its own."},
                        "scope": scope_write,
                        "context": {"type": "string", "description": "Short label (e.g. 'user preference', 'team standard')."},
                    },
                    "required": ["content"],
                },
            },
            {
                "name": "memory_recall",
                "description": (
                    "Search long-term memory. Relevant memories are already recalled "
                    "into your context each turn; use this only for something you "
                    "need now and cannot see there."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "What to search for."},
                        "scope": scope_read,
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "memory_reflect",
                "description": (
                    "Synthesize a reasoned answer across long-term memories, rather "
                    "than returning individual matches. Use for open questions about "
                    "the user's history."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "The question to reflect on."},
                        "scope": scope_read,
                    },
                    "required": ["query"],
                },
            },
        ]

    _TOOL_MAP = {
        "memory_retain": "hindsight_retain",
        "memory_recall": "hindsight_recall",
        "memory_reflect": "hindsight_reflect",
    }

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs: Any) -> str:
        inner_tool = self._TOOL_MAP.get(tool_name)
        if inner_tool is None:
            return tool_error(f"Unknown memory tool: {tool_name}")

        is_write = tool_name == "memory_retain"
        default_scope = "personal" if is_write else "both"
        scope = str(args.get("scope") or default_scope).strip().lower()
        if scope not in _SCOPES or (is_write and scope == "both"):
            return tool_error(
                f"Invalid scope {scope!r} for {tool_name}. "
                f"Use {'personal or shared' if is_write else 'personal, shared, or both'}."
            )

        inner_args = {k: v for k, v in args.items() if k != "scope"}
        targets = self._targets_for(scope)
        if not targets:
            return tool_error(self._unavailable_reason(scope))

        results = {}
        for label, provider in targets:
            try:
                results[label] = provider.handle_tool_call(inner_tool, dict(inner_args), **kwargs)
            except Exception as e:
                logger.warning("%s: %s failed on the %s bank: %s",
                               PROVIDER_NAME, inner_tool, label, e)
                results[label] = json.dumps({"error": str(e)})

        if len(results) == 1:
            return next(iter(results.values()))
        # A 'both' read: label each bank's result so the model does not attribute
        # a shared fact to the user personally.
        return json.dumps({label: _maybe_json(text) for label, text in results.items()})

    def _targets_for(self, scope: str) -> List[tuple]:
        pairs = []
        if scope in ("personal", "both") and self._personal is not None:
            pairs.append(("personal", self._personal))
        if scope in ("shared", "both") and self._shared is not None:
            pairs.append(("shared", self._shared))
        return pairs

    def _unavailable_reason(self, scope: str) -> str:
        if scope in ("personal", "both") and self._personal is None and self._personal_disabled_reason:
            return self._personal_disabled_reason
        return f"No memory bank is available for scope {scope!r}."

    # -- fan-out helpers -----------------------------------------------------

    def _banks(self):
        return [p for p in (self._personal, self._shared) if p is not None]

    def _each(self, fn) -> None:
        for provider in self._banks():
            try:
                fn(provider)
            except Exception as e:
                logger.debug("%s: fan-out call failed on %s: %s",
                             PROVIDER_NAME, getattr(provider, "name", "?"), e)

    @staticmethod
    def _call(provider: MemoryProvider, method: str, *a: Any, **kw: Any):
        try:
            return getattr(provider, method)(*a, **kw)
        except Exception as e:
            logger.debug("%s: %s failed: %s", PROVIDER_NAME, method, e)
            return "" if method in ("system_prompt_block", "prefetch") else None


def _maybe_json(text: str) -> Any:
    """Inline a sub-provider's JSON result instead of double-encoding it."""
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def register(ctx: Any) -> None:
    ctx.register_memory_provider(KageMemoryProvider())
