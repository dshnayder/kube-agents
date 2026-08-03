"""Per-user and shared memory for the Chat Agent, in one Hindsight bank.

Everyone's memory lives in a single bank. What separates one person's memories
from another's is a **scope tag** carried by every fact: ``user:<id>`` for a
private memory, ``scope:shared`` for one the whole organisation can read. Recall
asks for the current user's tag plus the shared tag; nothing else can come back.

Hindsight has all the machinery for this — tags on retain, tag filters on recall,
tag-scoped consolidation. What it does not have is any way to get the *current
user's id* into that configuration. ``{user}`` substitution exists but is wired
to exactly one setting: ``_resolve_bank_id_template`` is called for ``bank_id``
and nothing else (upstream ``plugins/memory/hindsight/__init__.py``), while
``retain_tags`` and ``recall_tags`` are read as literal strings. Configure
``retain_tags: "user:{user_id}"`` and every user is tagged with the characters
``user:{user_id}``. That is the gap this wrapper closes, and it is most of what
it does: resolve the identity, then hand the stock provider the right tags.

Three upstream behaviours make the difference between isolation and a leak, and
each is pinned here rather than left to configuration:

* **``any_strict``, never ``any``.** Hindsight's tag matcher treats ``any``/``all``
  as *"matching tags **or** no tags at all"* — only the ``_strict`` variants
  exclude untagged rows (``engine/search/tags.py``). The plugin's default is
  ``any``. In a single bank that default hands every untagged memory to every
  user, so ``_recall_tags_match`` is forced to ``any_strict`` below.

* **Reflect ignores tags.** ``hindsight_reflect`` and reflect-mode prefetch both
  call ``areflect(bank_id, query, budget)`` with no tag arguments, so a reflect
  would reason across every user in the bank. The REST API and the client both
  accept ``tags``/``tags_match`` — it is only the plugin that omits them — so
  ``memory_reflect`` is implemented here against the client directly, and
  prefetch is pinned to ``recall`` mode.

* **Observation scopes are pinned explicitly.** Recall returns *observations*,
  not raw facts, so isolation is only real if the observation layer is scoped
  too. Hindsight's default (``combined``) scopes an observation by the full tag
  set of its sources — and the stock provider attaches a ``session:<id>`` tag to
  every auto-retained turn. That would make each session its own scope, so
  nothing a user said last week would ever consolidate with what they say today.
  Setting ``observation_scopes`` to an explicit ``[[scope_tag]]`` fixes both
  halves at once: one durable scope per user, immune to whatever per-call
  provenance tags ride along. Hindsight's own consolidator documents this as the
  intended use of explicit scopes.

A fourth upstream behaviour is corrected rather than pinned. The stock read
tools answer an empty result with the string ``"No relevant memories found."``,
which a model reads as *no such record exists* — a claim it will then make
confidently about a store that holds the fact. ``memory_recall`` and
``memory_reflect`` are therefore implemented here to name their outcome
(``found`` / ``no_match`` / ``unreachable``) and to report the search they ran,
so absence is attributable to a query and a scope. See ``NO_MATCH_GUIDANCE``.

The two banks this replaced also carried a mission each — what the bank is for,
and what is worth extracting into it. One bank has one ``retain_mission``, but
``retain_mission`` is a per-bank *configurable* field, and configurable fields
can be overridden per item by a named **retain strategy**. So the personal and
shared extraction guidance survives as strategies (``personal``/``shared``)
rather than as separate banks, with ``retain_default_strategy`` making the
personal one apply to automatic capture.

Everything else is still the stock provider, loaded through
``load_memory_provider("hindsight")``. Not forking its ~92 KB implementation is
deliberate: a Hermes base-image bump brings Hindsight fixes along with it and
there is no merge to redo.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from plugins.memory import load_memory_provider
from tools.registry import tool_error

logger = logging.getLogger(__name__)

PROVIDER_NAME = "kube_agents_memory"
DEFAULT_BANK_ID = "kube-agents-memory"

# The tag every organisation-wide fact carries. Anything without it is only ever
# returned to the one user whose tag it bears.
SHARED_TAG = "scope:shared"
USER_TAG_PREFIX = "user:"

# Excludes untagged rows. See the module docstring — `any` would not.
TAGS_MATCH = "any_strict"

_SCOPES = ("personal", "shared", "both")
_VALID_BUDGETS = ("low", "mid", "high")

# Retain strategies. `retain_mission` steers what the extractor keeps, and it is
# in Hindsight's per-bank configurable-field set, so a named strategy can carry
# its own — which is how one bank still gets two different editorial policies.
PERSONAL_STRATEGY = "personal"
SHARED_STRATEGY = "shared"

# The strategy the TTL curator writes its checkpoints under.
#
# `memory_ttl_curator.py` would keep the bank bounded by distilling the
# observation layer back into facts and then retiring the aged originals. It is
# shelved and on no schedule, but the strategy is still provisioned here so the
# bank is ready for it rather than needing a migration later. A checkpoint is
# only sound if it carries the observation's text *unchanged* — re-summarising a
# summary every cycle is a game of telephone, and the bank would drift away from
# what was actually said. `verbatim` extraction is what guarantees that:
# `_collapse_to_verbatim` in Hindsight's extractor overwrites the fact text with
# the raw chunk, so one checkpoint in is one fact out, byte for byte. The LLM
# still runs, but only to attach entities and dates — never to rewrite.
CHECKPOINT_STRATEGY = "checkpoint"

PERSONAL_RETAIN_MISSION = (
    "Extract only durable facts about this person. Keep their location, "
    "timezone, role and responsibilities; the clusters, projects and "
    "environments they call their own; and the working preferences they state. "
    "Phrase each fact to stand alone and name the person rather than saying "
    "'the user'. "
    "Drop the state of individual tasks and tickets, decisions scoped to one "
    "piece of work, and anything the assistant itself did — that is a record of "
    "a conversation, not a fact about a person, and it stops being true once "
    "the work closes. Drop anything that holds for the whole team; that is "
    "shared knowledge, not personal."
)

# Shared knowledge is loaded from documents, not conversation. Hindsight's
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

RETAIN_STRATEGIES = {
    PERSONAL_STRATEGY: {"retain_mission": PERSONAL_RETAIN_MISSION},
    SHARED_STRATEGY: {"retain_mission": SHARED_RETAIN_MISSION},
    CHECKPOINT_STRATEGY: {"retain_extraction_mode": "verbatim"},
}

# What the bank is for, and how to answer from it.
#
# One field, not two. Hindsight's `set_mission` and `set_reflect_mission` are
# both deprecated aliases for `create_bank(bank_id, mission=...)`, so a bank has
# a single `mission` and it is what reflect reasons against; the text has to say
# what the bank holds *and* how to answer from it.
BANK_MISSION = (
    "The working memory of a Kubernetes platform team's assistant. It holds two "
    "kinds of knowledge, separated by tag. Shared knowledge is true for "
    "everybody: standard operating procedures, platform conventions and "
    "defaults, on-call and timezone facts, cluster and environment inventory, "
    "and the history of releases and infrastructure changes. Personal knowledge "
    "is about one individual: where they work and in which timezone, which "
    "clusters, projects and environments are theirs, how they prefer work to be "
    "done, and what they are responsible for. "
    "When answering, cite the specific procedure, version or dated change that "
    "supports the answer, and say plainly when the record does not cover the "
    "question rather than generalising from adjacent facts."
)

# Bank config this process has already settled, so the common case costs nothing.
# Deliberately per-process rather than persisted: it is only a cache, and
# re-applying after a restart is idempotent.
_bank_provisioned: set = set()

SHARED_SESSION_NOTICE = (
    "Personal memory is unavailable in this conversation. It is a shared thread "
    "that more than one person can post in, and the harness cannot attribute a "
    "message to its sender here, so nothing may be read from or written to a "
    "person's private memory. Shared memory still works; personal memory works "
    "in a direct message."
)
NO_IDENTITY_NOTICE = (
    "Personal memory is unavailable because this session carries no user "
    "identity. Only shared memory is reachable here."
)

RECALL_PREAMBLE = (
    "# Memory\n"
    "Durable facts recalled from previous sessions — both what you know about "
    "the person you are talking to and what the organisation has recorded for "
    "everyone. Use them to answer directly and to resolve possessives before "
    "delegating. Do not look these up with a tool — they are already here. "
    "These are the entries that matched this turn, not an index of everything "
    "recorded; something absent here may still be in memory."
)

# What a read reports about itself.
#
# The stock tool answers an empty recall with the bare string "No relevant
# memories found." A model reads that as *no such record exists* and will then
# say so with full confidence: in the scale test, a specialist reported a real
# ADR as "zero records — its content isn't recorded anywhere retrievable" while
# the ADR's text sat in the store it was nominally reading. The failure is in the
# return value, not the model. Three outcomes are not interchangeable, and the
# tool has to name which one happened rather than leaving it to be inferred:
#
#   found       — the store answered, and matched
#   no_match    — the store answered, and matched nothing *for this query*
#   unreachable — the store did not answer; nothing was searched at all
#
# Every return therefore also carries a `searched` envelope — bank, scope tags,
# query, layer — so an empty result is attributable to a search that was run,
# rather than reading as a property of the world.
NO_MATCH_GUIDANCE = (
    "The store was reachable and answered: this query matched nothing. That is "
    "not the same as the record not existing. Recall is a semantic search that "
    "returns top matches over the consolidated layer, so a record phrased "
    "differently, held under a scope not searched here, or retained but not yet "
    "consolidated will not surface. Do not report that something does not exist "
    "on the strength of this result — report that the search did not surface it, "
    "and try an exact identifier, different wording, or a wider scope."
)
UNREACHABLE_GUIDANCE = (
    "Memory could not be reached, so nothing was searched. This is a failure of "
    "the store, not an absence of records. Say that memory was unavailable; do "
    "not answer as though it were empty."
)

# The rule the return values above exist to make followable. Stated in the system
# prompt as well as in each result, because the injected block has no return
# value to carry it: when nothing matches, no memory block appears at all, and
# silence is the one outcome the tool cannot annotate.
MEMORY_ABSENCE_RULE = (
    "Memory is a search, not an index. Neither the injected entries nor a "
    "`memory_recall` result is a list of everything recorded, and a read can "
    "fail to reach the store entirely. Never state that a record does not exist "
    "because memory did not return it — say that you could not find it, and name "
    "which it was: the search matched nothing, or memory was unavailable."
)

SYSTEM_PROMPT_HEADER = "# Memory"
SYSTEM_PROMPT_BODY = (
    "You have long-term memory. Relevant entries are injected into your context "
    "automatically each turn and are retained automatically — in the normal case "
    "you do not call a tool at all. It holds two kinds of fact:\n"
    "- **Personal** — private to the person you are talking to.\n"
    "- **Shared** — visible to everyone in the organisation.\n"
    "\n"
    "`memory_recall` searches and `memory_reflect` synthesises across both by "
    "default; use them only when the injected memories are not enough. "
    "`memory_retain` writes a personal fact unless you pass `scope: \"shared\"`, "
    "which you should do only for facts that are true for everybody, never for "
    "one person's preferences.\n"
    "\n"
    + MEMORY_ABSENCE_RULE
)
SYSTEM_PROMPT_SHARED_ONLY = (
    "You have long-term memory, but only the **shared** part of it is reachable "
    "here — the facts this organisation has recorded for everyone. Relevant "
    "entries are injected into your context automatically. `memory_recall` and "
    "`memory_reflect` search them; `memory_retain` adds to them.\n"
    "\n"
    + MEMORY_ABSENCE_RULE
)
SYSTEM_PROMPT_READ_ONLY = (
    "You can **read** the organisation's shared long-term memory: standard "
    "procedures, platform conventions and defaults, cluster and environment "
    "inventory, ownership, and the history of releases and infrastructure "
    "changes. Relevant entries are injected into your context automatically; "
    "`memory_recall` searches them and `memory_reflect` synthesises across them "
    "when you need something the injected entries do not cover.\n"
    "\n"
    "You **cannot write** to memory, and there is no tool that would let you. "
    "What you conclude during a task is a finding, not a recorded fact: report "
    "it in your result. Do not cache what you read here into a file, a skill or "
    "a note for later — a private copy goes stale the moment shared memory is "
    "corrected, and nobody can review it. Read it again next time.\n"
    "\n"
    + MEMORY_ABSENCE_RULE
)


def _sanitize_user_id(user_id: str) -> str:
    """Reduce a gateway identity to something safe to use as a tag value.

    Mirrors Hindsight's own ``_sanitize_bank_segment``: alphanumerics, dash and
    underscore survive, everything else collapses to a single dash. Applied for
    the same reason it is applied to bank names — the value is attacker-adjacent
    (it comes from the chat platform) and ends up in a query filter.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", str(user_id or ""))
    return re.sub(r"-{2,}", "-", cleaned).strip("-_")


def _memory_is_read_only() -> bool:
    """Read ``memory.read_only`` from the active profile's config.yaml.

    Profile-scoped: ``load_config()`` resolves through ``HERMES_HOME``, and a
    kanban worker is launched with ``HERMES_HOME`` pointed at its own profile
    directory (``hermes_cli/kanban_db.py`` — ``env["HERMES_HOME"] =
    resolve_profile_env(profile_arg)``). So the platform specialist reads
    ``profiles/platform/config.yaml`` and the Chat Agent reads its own.

    It is a setting rather than something derived from the session because the
    two identity-less cases are not the same. A shared chat space has humans in
    it who can vouch for a shared write; a dispatcher-spawned specialist has
    nobody. Only the second is read-only, and only the profile config knows
    which one it is.

    Defaults to False — a profile that says nothing keeps the write tools.
    """
    try:
        from hermes_cli.config import load_config
        config = load_config() or {}
        memory = config.get("memory")
        if isinstance(memory, dict):
            return bool(memory.get("read_only", False))
    except Exception as e:
        logger.debug("Could not read memory.read_only, assuming writable: %s", e)
    return False


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


class KubeAgentsMemoryProvider(MemoryProvider):
    """One Hindsight bank, split per user by scope tags."""

    def __init__(self) -> None:
        self._hindsight: Optional[MemoryProvider] = None
        self._user_tag: str = ""
        self._personal_disabled_reason: str = ""
        self._session_id: str = ""
        self._read_only: bool = False

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    def is_available(self) -> bool:
        # Hermes asks this *before* initialize() and drops the provider outright
        # if it says no (agent_init.py:1460), so it has to be answerable with no
        # bank built yet. Hindsight's own answer is stateless — it reads
        # $HERMES_HOME/hindsight/config.json — so an uninitialised instance gives
        # the same verdict the real one would.
        if self._hindsight is not None:
            return self._hindsight.is_available()
        probe = load_memory_provider("hindsight")
        return bool(probe is not None and probe.is_available())

    # -- lifecycle -----------------------------------------------------------

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self._session_id = str(session_id or "").strip()
        self._hindsight = None
        self._user_tag = ""
        self._personal_disabled_reason = ""
        self._read_only = _memory_is_read_only()

        user_id = _sanitize_user_id(kwargs.get("user_id") or "")

        # Refuse personal memory when the session can carry more than one human.
        #
        # agent._user_id is frozen once at Agent construction, and
        # build_session_key() (gateway/session.py) deliberately omits the
        # participant id inside a thread unless `thread_sessions_per_user` is on.
        # So in a shared thread the second speaker reuses the first speaker's
        # cached Agent, and a per-user tag would recall person A's memories into
        # person B's prompt and retain B's turns under A's name. Nothing in the
        # provider protocol identifies the speaker — system_prompt_block() takes
        # no arguments and handle_tool_call() is passed no identity — so it fails
        # closed. Shared memory, visible to everyone by design, is unaffected.
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
                "%s: personal memory disabled for session %s (shared %s thread — "
                "sender cannot be attributed)", PROVIDER_NAME, session_id, chat_type,
            )
        elif not user_id:
            self._personal_disabled_reason = NO_IDENTITY_NOTICE
            logger.info("%s: personal memory disabled for session %s (no user identity)",
                        PROVIDER_NAME, session_id)
        else:
            self._user_tag = f"{USER_TAG_PREFIX}{user_id}"

        self._hindsight = self._init_hindsight(session_id, kwargs)
        if self._hindsight is None:
            logger.warning("%s: no memory available for session %s", PROVIDER_NAME, session_id)
            return

        self._apply_scoping(self._hindsight)
        self._ensure_bank(self._hindsight)
        logger.info("%s: bank=%s scope=%s budget=%s writes=%s", PROVIDER_NAME,
                    getattr(self._hindsight, "_bank_id", "?"),
                    self._user_tag or "shared-only",
                    getattr(self._hindsight, "_budget", "?"),
                    "denied (read_only)" if self._read_only else "allowed")

    def _init_hindsight(self, session_id: str, kwargs: Dict[str, Any]) -> Optional[MemoryProvider]:
        provider = load_memory_provider("hindsight")
        if provider is None:
            logger.warning("%s: could not load the hindsight provider", PROVIDER_NAME)
            return None
        try:
            provider.initialize(session_id, **kwargs)
        except Exception as e:
            logger.warning("%s: hindsight initialize failed: %s", PROVIDER_NAME, e)
            return None
        provider._recall_prompt_preamble = RECALL_PREAMBLE
        return provider

    def _apply_scoping(self, provider: MemoryProvider) -> None:
        """Pin the tag scoping that makes one bank safe for many users.

        Every value here is read by the stock provider at call time, which is why
        overriding the resolved attributes after ``initialize()`` is enough and
        no config-file contract is needed.
        """
        # One bank per deployment, not per user, and its name is a constant here
        # rather than a setting: the Hindsight config file is image-owned, but a
        # bank_id left in a hand-edited copy on the PVC used to win and silently
        # move every memory into a bank nobody was reading. Say so and ignore it.
        config = getattr(provider, "_config", None) or {}
        configured = str(config.get("bank_id") or "").strip()
        if configured and configured != DEFAULT_BANK_ID:
            logger.warning("%s: ignoring bank_id %r from the Hindsight config; this "
                           "provider is single-bank and pins %r",
                           PROVIDER_NAME, configured, DEFAULT_BANK_ID)
        provider._bank_id_template = ""
        provider._bank_id = DEFAULT_BANK_ID

        # Read side. Shared facts are visible to everyone; personal ones only to
        # their owner. `any_strict` is what excludes untagged rows from both.
        recall_tags = [self._user_tag] if self._user_tag else []
        recall_tags.append(SHARED_TAG)
        provider._recall_tags = recall_tags
        provider._recall_tags_match = TAGS_MATCH

        # Reflect-mode prefetch calls areflect() with no tag arguments, so it
        # would read across every user. Recall mode applies the filter above.
        if getattr(provider, "_prefetch_method", "recall") != "recall":
            logger.warning("%s: forcing recall-mode prefetch (reflect prefetch ignores "
                           "tag filters and would cross users)", PROVIDER_NAME)
        provider._prefetch_method = "recall"

        # Write side. Automatic capture is always personal — shared facts are
        # written deliberately, through the tool. With no identity there is
        # nobody to attribute a turn to, so nothing is captured automatically.
        if self._read_only:
            provider._retain_tags = []
            provider._tags = None
            provider._observation_scopes = None
            provider._auto_retain = False
        elif self._user_tag:
            provider._retain_tags = [self._user_tag]
            provider._tags = [self._user_tag]
            # One durable scope per user. Without this the `session:<id>` tag the
            # provider adds to each turn would put every session in a scope of
            # its own, and nothing would ever consolidate across them.
            provider._observation_scopes = [[self._user_tag]]
        else:
            provider._retain_tags = []
            provider._tags = None
            provider._observation_scopes = None
            provider._auto_retain = False

        self._apply_budget(provider)

    @staticmethod
    def _apply_budget(provider: MemoryProvider) -> None:
        """Honour ``recall_budget`` from the Hindsight config, if it is valid.

        Hindsight resolves it into ``_budget`` and reads that attribute on every
        recall and reflect. Unset leaves its own resolution (``mid``) in place.
        """
        config = getattr(provider, "_config", None) or {}
        value = str(config.get("recall_budget") or "").strip().lower()
        if value in _VALID_BUDGETS:
            provider._budget = value

    @staticmethod
    def _ensure_bank(provider: MemoryProvider) -> None:
        """Provision the bank's mission and retain strategies, creating it if needed.

        None of this can be seeded ahead of time: a Hindsight bank does not exist
        until something is written to it. Doing it here means the first session
        provisions it, a deleted bank comes back correctly, and there is no
        manual step for an operator to forget.

        ``retain_strategies`` is the load-bearing part. ``personal`` and
        ``shared`` carry the two extraction missions that used to be two banks,
        ``retain_default_strategy`` points automatic capture at the personal one,
        and ``checkpoint`` is what the TTL curator writes under. A missing
        strategy is not an error to Hindsight — ``apply_strategy`` logs a warning
        and silently uses the bank default — so the curator checks for its own
        before it will run.

        The comparison is the sentinel for "already done": the bank-level
        ``mission`` is not part of the ``get_bank_config`` payload (that returns
        ``{bank_id, config}``, and mission is bank metadata), so it cannot be
        compared cheaply. Mission and strategies are always written together,
        which makes the strategies a sound proxy for both.

        Costs one read per process and two writes only when something changed.
        Failures are logged and swallowed — an unguided bank is worse than a
        guided one, but it still works, and memory must never be the reason a
        session fails to start.
        """
        bank_id = str(getattr(provider, "_bank_id", "") or "").strip()
        if not bank_id or bank_id in _bank_provisioned:
            return
        # Recorded before the attempt, not after: if the API is down, every
        # subsequent session in this process would otherwise retry a call that is
        # already known to be failing, on the session-creation path.
        _bank_provisioned.add(bank_id)
        try:
            client = provider._get_client()
            config = (client.get_bank_config(bank_id) or {}).get("config") or {}
            if (config.get("retain_strategies") == RETAIN_STRATEGIES
                    and config.get("retain_default_strategy") == PERSONAL_STRATEGY):
                return
            # create_bank doubles as the update path — it is what Hindsight's own
            # deprecated set_mission() calls — and leaves existing facts intact.
            # It must come first: it is the call that creates the bank, and
            # update_bank_config only edits one that exists.
            client.create_bank(bank_id=bank_id, mission=BANK_MISSION)
            client.update_bank_config(
                bank_id,
                retain_strategies=RETAIN_STRATEGIES,
                retain_default_strategy=PERSONAL_STRATEGY,
            )
            logger.info("%s: provisioned mission and retain strategies on bank %s",
                        PROVIDER_NAME, bank_id)
        except Exception as e:
            logger.warning("%s: could not provision bank %s: %s", PROVIDER_NAME, bank_id, e)

    def shutdown(self) -> None:
        self._call("shutdown")

    # -- context -------------------------------------------------------------

    def system_prompt_block(self) -> str:
        if self._hindsight is None:
            return ""
        if self._read_only:
            return f"{SYSTEM_PROMPT_HEADER}\n{SYSTEM_PROMPT_READ_ONLY}"
        if not self._user_tag:
            reason = self._personal_disabled_reason
            block = f"{SYSTEM_PROMPT_HEADER}\n{SYSTEM_PROMPT_SHARED_ONLY}"
            return block + (f"\n\n{reason}" if reason else "")
        return f"{SYSTEM_PROMPT_HEADER}\n{SYSTEM_PROMPT_BODY}"

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        self._call("queue_prefetch", query, session_id=session_id)

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        return self._call("prefetch", query, session_id=session_id) or ""

    def on_turn_start(self, turn_number: int, message: str, **kwargs: Any) -> None:
        self._call("on_turn_start", turn_number, message, **kwargs)

    def on_session_switch(self, new_session_id: str, **kwargs: Any) -> None:
        self._session_id = str(new_session_id or "").strip()
        self._call("on_session_switch", new_session_id, **kwargs)

    # -- retention -----------------------------------------------------------
    #
    # Automatic capture is personal, and only when the speaker is known. Shared
    # knowledge is read by everyone, so it never absorbs a conversation wholesale
    # — it takes explicit writes through memory_retain(scope="shared").

    def sync_turn(self, user_content: str, assistant_content: str, **kwargs: Any) -> None:
        if self._user_tag and not self._read_only:
            self._call("sync_turn", user_content, assistant_content, **kwargs)

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        if self._user_tag and not self._read_only:
            self._call("on_session_end", messages)

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
        # A read-only profile is not shown the write tool at all. Advertising it
        # and refusing the call would spend a turn and read as a transient
        # failure worth retrying; the absent schema is unambiguous. The refusal
        # in handle_tool_call stays as the backstop.
        retain: List[Dict[str, Any]] = [] if self._read_only else [
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
            }
        ]
        return retain + [
            {
                "name": "memory_recall",
                "description": (
                    "Search long-term memory. Relevant memories are already recalled "
                    "into your context each turn; use this only for something you "
                    "need now and cannot see there. Returns a `status` of `found`, "
                    "`no_match` (searched, matched nothing) or `unreachable` (the "
                    "store did not answer) — the last two are not evidence that a "
                    "record does not exist."
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

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs: Any) -> str:
        if self._hindsight is None:
            return tool_error("Memory is unavailable.")
        if tool_name not in ("memory_retain", "memory_recall", "memory_reflect"):
            return tool_error(f"Unknown memory tool: {tool_name}")

        is_write = tool_name == "memory_retain"
        if is_write and self._read_only:
            # Backstop for the schema omission above. Reached only if the model
            # invents the call or a cached schema outlives a config change.
            logger.warning("%s: refused a write on a read-only profile", PROVIDER_NAME)
            return tool_error(
                "Memory is read-only for this agent — there is no way to write to it "
                "from here, and retrying will not change that. Report the fact in "
                "your result instead; recording it is the front-door agent's job.",
                status="read_only",
            )
        scope = str(args.get("scope") or ("personal" if is_write else "both")).strip().lower()
        if scope not in _SCOPES or (is_write and scope == "both"):
            return tool_error(
                f"Invalid scope {scope!r} for {tool_name}. "
                f"Use {'personal or shared' if is_write else 'personal, shared, or both'}."
            )
        if scope in ("personal", "both") and not self._user_tag:
            # 'both' degrades to shared rather than failing: the shared half is
            # still answerable, and the system prompt has already explained why
            # the personal half is not.
            if scope == "personal":
                return tool_error(self._personal_disabled_reason
                                  or "Personal memory is unavailable in this session.")
            scope = "shared"

        if is_write:
            return self._retain(args, scope)
        return self._read(tool_name, args, scope)

    def _tags_for(self, scope: str) -> List[str]:
        if scope == "shared":
            return [SHARED_TAG]
        if scope == "personal":
            return [self._user_tag]
        return [self._user_tag, SHARED_TAG] if self._user_tag else [SHARED_TAG]

    def _retain(self, args: Dict[str, Any], scope: str) -> str:
        """Write one fact under the tags and strategy its scope requires.

        Written against the client rather than delegated to ``hindsight_retain``
        because the stock tool merges per-call tags with the instance's own and
        offers no per-call ``observation_scopes`` or ``strategy``. A shared fact
        must not inherit the caller's ``user:`` tag — it would consolidate into
        that person's scope and become invisible to everyone else.
        """
        content = str(args.get("content") or "").strip()
        if not content:
            return tool_error("Missing required parameter: content")
        tags = self._tags_for(scope)
        item: Dict[str, Any] = {
            "content": content,
            "tags": tags,
            "observation_scopes": [tags],
            "strategy": SHARED_STRATEGY if scope == "shared" else PERSONAL_STRATEGY,
        }
        context = str(args.get("context") or "").strip()
        if context:
            item["context"] = context
        bank_id = self._hindsight._bank_id
        try:
            self._hindsight._run_hindsight_operation(
                lambda client: client.aretain_batch(bank_id=bank_id, items=[item], retain_async=False)
            )
        except Exception as e:
            logger.warning("%s: retain failed (scope=%s): %s", PROVIDER_NAME, scope, e)
            return tool_error(f"Failed to store the memory: {e}")
        return json.dumps({"result": f"Stored in {scope} memory."})

    def _read(self, tool_name: str, args: Dict[str, Any], scope: str) -> str:
        query = str(args.get("query") or "").strip()
        if not query:
            return tool_error("Missing required parameter: query")
        tags = self._tags_for(scope)
        if tool_name == "memory_recall":
            return self._recall(query, scope, tags)
        return self._reflect(query, scope, tags)

    def _searched(self, tool_name: str, query: str, scope: str, tags: List[str]) -> Dict[str, Any]:
        """Describe the search that was run, whatever its outcome.

        Returned alongside every read so that "nothing came back" is a statement
        about a query and a scope, not about the world. See NO_MATCH_GUIDANCE.
        """
        envelope: Dict[str, Any] = {
            "tool": tool_name,
            "query": query,
            "scope": scope,
            "bank": self._hindsight._bank_id,
            "tags": list(tags),
        }
        if tool_name == "memory_recall":
            types = getattr(self._hindsight, "_recall_types", None)
            if types:
                envelope["layer"] = list(types)
        return envelope

    def _recall(self, query: str, scope: str, tags: List[str]) -> str:
        """Search, and report what was searched.

        Written against the client rather than delegated to ``hindsight_recall``
        for two reasons. The stock tool collapses an empty result set to the
        string "No relevant memories found." and a transport failure to a generic
        tool error, which is exactly the conflation this replaces; and it filters
        on the instance's own ``_recall_tags``, so a narrower scope could only be
        served by mutating that attribute around the call and restoring it after.
        One direct call serves every scope and keeps the outcome distinguishable.
        """
        searched = self._searched("memory_recall", query, scope, tags)
        recall_kwargs: Dict[str, Any] = {
            "bank_id": self._hindsight._bank_id,
            "query": query,
            "budget": getattr(self._hindsight, "_budget", "mid"),
            "max_tokens": getattr(self._hindsight, "_recall_max_tokens", 4096),
            "tags": tags,
            "tags_match": TAGS_MATCH,
        }
        types = getattr(self._hindsight, "_recall_types", None)
        if types:
            recall_kwargs["types"] = list(types)
        try:
            response = self._hindsight._run_hindsight_operation(
                lambda client: client.arecall(**recall_kwargs)
            )
        except Exception as e:
            logger.warning("%s: recall failed (scope=%s): %s", PROVIDER_NAME, scope, e)
            return tool_error(
                f"Memory is unreachable: {e}. {UNREACHABLE_GUIDANCE}",
                status="unreachable",
                searched=searched,
            )
        results = list(getattr(response, "results", None) or [])
        if not results:
            return json.dumps(
                {"status": "no_match", "searched": searched, "matches": 0,
                 "result": NO_MATCH_GUIDANCE}
            )
        lines = [f"{i}. {getattr(r, 'text', '') or ''}" for i, r in enumerate(results, 1)]
        return json.dumps(
            {"status": "found", "searched": searched, "matches": len(results),
             "result": "\n".join(lines)}
        )

    def _reflect(self, query: str, scope: str, tags: List[str]) -> str:
        """Synthesize across memories, with the tag filter the stock tool omits.

        ``hindsight_reflect`` calls ``areflect(bank_id, query, budget)`` and
        stops there, so in a shared bank it would reason over everyone. The API
        and the generated client both accept ``tags``/``tags_match``; only the
        plugin leaves them out. Mental models are excluded because they are
        bank-level and not tag-scoped — this deployment creates none, so the
        exclusion costs nothing and removes the one remaining unscoped path.
        """
        searched = self._searched("memory_reflect", query, scope, tags)
        bank_id = self._hindsight._bank_id
        budget = getattr(self._hindsight, "_budget", "mid")
        try:
            response = self._hindsight._run_hindsight_operation(
                lambda client: client.areflect(
                    bank_id=bank_id, query=query, budget=budget,
                    tags=tags, tags_match=TAGS_MATCH, exclude_mental_models=True,
                )
            )
        except Exception as e:
            logger.warning("%s: reflect failed (scope=%s): %s", PROVIDER_NAME, scope, e)
            return tool_error(
                f"Memory is unreachable: {e}. {UNREACHABLE_GUIDANCE}",
                status="unreachable",
                searched=searched,
            )
        text = str(getattr(response, "text", "") or "").strip()
        if not text:
            return json.dumps(
                {"status": "no_match", "searched": searched, "result": NO_MATCH_GUIDANCE}
            )
        return json.dumps({"status": "found", "searched": searched, "result": text})

    # -- helper --------------------------------------------------------------

    def _call(self, method: str, *a: Any, **kw: Any):
        if self._hindsight is None:
            return None
        try:
            return getattr(self._hindsight, method)(*a, **kw)
        except Exception as e:
            logger.debug("%s: %s failed: %s", PROVIDER_NAME, method, e)
            return "" if method == "prefetch" else None


def register(ctx: Any) -> None:
    ctx.register_memory_provider(KubeAgentsMemoryProvider())
