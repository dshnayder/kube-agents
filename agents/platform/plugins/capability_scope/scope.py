"""Prototype of the capability-scoping layer in docs/designs/context-scoped-capabilities.md.

Experiment code, not a shipped feature. It runs as a Hermes plugin on the Platform Agent profile
and does nothing at all unless KA_SCOPE_MODE is set. With it set, one image serves every arm of
the A/B in bench/experiments/capability-scope-ab/:

* KA_SCOPE_MODE=off        shadow mode: rank skills and tools against each turn's user message,
                           record what the model was shown and what it used (the design's scoping
                           record) as JSON lines, and change nothing the model sees.
* KA_SCOPE_MODE=skills     also inject the top-K skills' full descriptions into the user message
                           through the pre_llm_call hook. The system-prompt index is expected to be
                           names-only (KA_SKILLS_INDEX_MODE=names, applied by
                           deploy/docker/patches/apply_capability_scope.py).
* KA_SCOPE_MODE=skills+tools  also filter the tool array sent to the model to a pinned set plus the
                           top-N ranked tools, with the hidden names carried as a shelf on the search
                           tool's description. The conversation loop calls filter_tools() through
                           the same patch.

The ranker is BM25 over catalogue metadata (skill name and description; tool name, description
and parameter names). No model call, no side effects, per the design's constraint.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger("hermes.plugin.capability_scope")

MODE_ENV = "KA_SCOPE_MODE"
MODE_OFF = "off"
MODE_SKILLS = "skills"
MODE_SKILLS_TOOLS = "skills+tools"
MODES = (MODE_OFF, MODE_SKILLS, MODE_SKILLS_TOOLS)
K_SKILLS_ENV = "KA_SCOPE_K_SKILLS"
N_TOOLS_ENV = "KA_SCOPE_N_TOOLS"
RECORD_PATH_ENV = "KA_SCOPE_RECORD"
PINNED_TOOLS_ENV = "KA_SCOPE_PINNED_TOOLS"
PINNED_SKILLS_ENV = "KA_SCOPE_PINNED_SKILLS"
EXTRA_SKILLS_DIRS_ENV = "KA_EXTRA_SKILLS_DIRS"
SKILLS_DIR_ENV = "KA_SKILLS_DIR"
HERMES_HOME_ENV = "HERMES_HOME"
DEFAULT_HERMES_HOME = "/opt/data"
DEFAULT_K_SKILLS = 6
DEFAULT_N_TOOLS = 6
DEFAULT_RECORD_NAME = "capability_scope.jsonl"
DEFAULT_PINNED_TOOLS = (
    "terminal", "process", "read_file", "write_file", "patch", "search_files",
    "skills_list", "skill_view", "skill_manage", "execute_code",
    "tool_search", "tool_describe", "tool_call", "clarify", "todo",
)
# The shelf rides on the first of these that survives filtering; tool_search is pinned by default.
SHELF_CARRIER_TOOLS = ("tool_search", "skills_list", "terminal")
SKILL_FILE = "SKILL.md"
SKILLS_SUBDIR = "skills"
STICKY_TURNS = 5
SESSION_TTL_SECONDS = 3600
CATALOGUE_TTL_SECONDS = 300
BM25_K1 = 1.5
BM25_B = 0.75
TOKEN_RE = re.compile(r"[a-z0-9]+")
STOPWORDS = frozenset(
    "the a an and or of to in on for with is are be this that it as at by from use when you "
    "your our my we i how what which do does can should would will not no into over under".split()
)
MIN_TOKEN_LEN = 2
# The stemmer is pinned to what the recorded run used (results/ in the experiment directory); it
# does not fold `upgrades` and `upgrade`, and test_scope.py pins that so a change to it is a
# deliberate re-run rather than a silent drift of the record.
STEM_SUFFIXES = ("ations", "ation", "ings", "ing", "ers", "er", "ies", "es", "s")
MIN_STEM_LEN = 4
NAME_BOOST = 3
SCORE_DECIMALS = 3
SIGNAL_HASH_LEN = 16
MAX_SIGNAL_CHARS = 4000
SKILLS_HEADER = "[SKILLS RELEVANT TO THIS REQUEST]"
SKILLS_PREAMBLE = (
    "The system prompt lists every skill by name. These are the ones most relevant to the "
    "current request, with their full descriptions:"
)
SKILLS_FOOTER = (
    "If one of these fits, load it with skill_view(name) before acting. If none fits, "
    "call skills_list to see every skill with its description."
)
TOOLS_SHELF_TEXT = (
    " Tools that exist but are not loaded for this request: {names}. Name the one you need in "
    "your reply and it loads on the next turn."
)
DESCRIPTION_KEY = "description"
NAME_KEY = "name"
FRONTMATTER_DELIM = "---"


def _stem(token: str) -> str:
    """A crude single-suffix stripper (v1): folds autoscaling/autoscaler and pods/pod, and not
    upgrades/upgrade or PVCs/PVC. Known weakness, kept as the run had it."""
    for suffix in STEM_SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= MIN_STEM_LEN:
            return token[: -len(suffix)]
    return token


def _tokens(text: str) -> List[str]:
    return [
        _stem(t) for t in TOKEN_RE.findall((text or "").lower())
        if len(t) >= MIN_TOKEN_LEN and t not in STOPWORDS
    ]


class BM25:
    """Minimal BM25 over a fixed document list. Rebuilt whenever the catalogue changes."""

    def __init__(self, docs: Sequence[Tuple[str, str]]):
        self.ids = [d[0] for d in docs]
        self.doc_tokens = [_tokens(d[1]) for d in docs]
        self.doc_len = [len(t) for t in self.doc_tokens]
        self.avgdl = (sum(self.doc_len) / len(self.doc_len)) if self.doc_len else 0.0
        self.df: Dict[str, int] = {}
        for toks in self.doc_tokens:
            for t in set(toks):
                self.df[t] = self.df.get(t, 0) + 1
        self.n = len(self.ids)
        self.tf: List[Dict[str, int]] = []
        for toks in self.doc_tokens:
            counts: Dict[str, int] = {}
            for t in toks:
                counts[t] = counts.get(t, 0) + 1
            self.tf.append(counts)

    def score(self, query: str) -> List[Tuple[str, float]]:
        q = _tokens(query)
        out: List[Tuple[str, float]] = []
        for i, doc_id in enumerate(self.ids):
            s = 0.0
            dl = self.doc_len[i] or 1
            for t in q:
                f = self.tf[i].get(t)
                if not f:
                    continue
                idf = math.log(1 + (self.n - self.df[t] + 0.5) / (self.df[t] + 0.5))
                s += idf * (f * (BM25_K1 + 1)) / (f + BM25_K1 * (1 - BM25_B + BM25_B * dl / self.avgdl))
            out.append((doc_id, s))
        out.sort(key=lambda x: (-x[1], x[0]))
        return out


def _parse_frontmatter(text: str) -> Dict[str, Any]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != FRONTMATTER_DELIM:
        return {}
    body: List[str] = []
    for line in lines[1:]:
        if line.strip() == FRONTMATTER_DELIM:
            break
        body.append(line)
    raw = "\n".join(body)
    try:
        import yaml  # Hermes ships PyYAML

        data = yaml.safe_load(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        out: Dict[str, Any] = {}
        for line in body:
            if ":" in line and not line.startswith(" "):
                k, v = line.split(":", 1)
                out[k.strip()] = v.strip().strip("'\"")
        return out


def _hermes_home() -> Path:
    """The profile's home. `hermes --profile X` resolves it internally; the env var may still
    name the parent, so ask Hermes first and fall back to the environment."""
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get(HERMES_HOME_ENV, DEFAULT_HERMES_HOME))


def _skills_dirs() -> List[Path]:
    explicit = os.environ.get(SKILLS_DIR_ENV)
    dirs = [Path(explicit)] if explicit else [_hermes_home() / SKILLS_SUBDIR]
    extra = os.environ.get(EXTRA_SKILLS_DIRS_ENV, "")
    for d in extra.split(os.pathsep):
        if d.strip():
            dirs.append(Path(d.strip()).expanduser())
    return dirs


def load_skill_catalogue() -> List[Dict[str, str]]:
    """Every SKILL.md under the profile's skills dir and any extra dirs, as name/description.

    This walks the directories directly; Hermes's own environment and platform gates on skills
    are not applied, so the catalogue can be a superset of the rendered index.
    """
    seen: Dict[str, Dict[str, str]] = {}
    for base in _skills_dirs():
        if not base.is_dir():
            continue
        for path in sorted(base.rglob(SKILL_FILE)):
            try:
                fm = _parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("capability_scope: cannot read %s: %s", path, exc)
                continue
            name = str(fm.get(NAME_KEY) or path.parent.name).strip()
            desc = str(fm.get(DESCRIPTION_KEY) or "").strip()
            if name and name not in seen:
                seen[name] = {"name": name, "description": " ".join(desc.split()), "path": str(path)}
    return list(seen.values())


class _State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.skills: List[Dict[str, str]] = []
        self.skills_index: Optional[BM25] = None
        self.skills_loaded_at = 0.0
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self.tools_index_key: Optional[Tuple[str, ...]] = None
        self.tools_index: Optional[BM25] = None


_state = _State()


def mode() -> Optional[str]:
    """The arm, or None when the plugin is inert. Unset means inert; an unknown value is inert too."""
    raw = (os.environ.get(MODE_ENV) or "").strip().lower()
    return raw if raw in MODES else None


def enabled() -> bool:
    return mode() is not None


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, "") or default))
    except ValueError:
        return default


def _record_path() -> Path:
    explicit = os.environ.get(RECORD_PATH_ENV)
    if explicit:
        return Path(explicit)
    return _hermes_home() / DEFAULT_RECORD_NAME


def record(event: str, **fields: Any) -> None:
    if not enabled():
        return
    payload = {"event": event, "ts": time.time(), "mode": mode(), **fields}
    line = json.dumps(payload, default=str)
    try:
        path = _record_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with _state.lock:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception as exc:  # pragma: no cover - never break a turn over the record
        logger.debug("capability_scope: record write failed: %s", exc)
    logger.info("capability_scope %s", line)


def _ensure_skills_index() -> None:
    now = time.time()
    if _state.skills_index is not None and now - _state.skills_loaded_at < CATALOGUE_TTL_SECONDS:
        return
    skills = load_skill_catalogue()
    # The name is the author's own summary; repeat it so its tokens outweigh the prose.
    docs = [(s["name"], f"{(s['name'].replace('-', ' ') + ' ') * NAME_BOOST}{s['description']}") for s in skills]
    _state.skills = skills
    _state.skills_index = BM25(docs)
    _state.skills_loaded_at = now


def _session(session_id: str) -> Dict[str, Any]:
    """Per-session working-set state; callers hold the lock. Evicts sessions idle past the TTL,
    which is the bound on memory: Hermes fires on_session_end after every message, so that hook
    cannot be used to clear state without losing the sticky set between turns."""
    now = time.time()
    for sid in [sid for sid, st in _state.sessions.items() if now - st["last_seen"] > SESSION_TTL_SECONDS]:
        del _state.sessions[sid]
    s = _state.sessions.get(session_id)
    if s is None:
        s = {"turn": 0, "signal": "", "skills": {}, "tools": {}, "hidden_tools": [], "turn_id": "",
             "recorded_tools_turn": -1, "turn_started": 0.0}
        _state.sessions[session_id] = s
    s["last_seen"] = now
    return s


def _sticky_update(active: Dict[str, int], ranked: Iterable[str], turn: int, budget: int) -> List[str]:
    """Sticky working set. This turn's top entries always enter; entries from earlier turns are
    carried while they are younger than STICKY_TURNS, most recent first, up to as many again as
    the fresh budget, so the working set is at most twice `budget` and a stale set can never
    crowd out what this turn ranked. `active` maps a name to the turn it last ranked in."""
    fresh = list(dict.fromkeys(ranked))[:budget]
    for name in fresh:
        active[name] = turn
    for name in [n for n, t in list(active.items()) if turn - t >= STICKY_TURNS]:
        del active[name]
    carried = sorted((n for n in active if n not in fresh), key=lambda n: -active[n])[: max(0, 2 * budget - len(fresh))]
    for name in [n for n in active if n not in fresh and n not in carried]:
        del active[name]
    return fresh + carried


def _pinned(env: str, default: Sequence[str]) -> List[str]:
    raw = os.environ.get(env)
    if raw is None:
        return list(default)
    return [x.strip() for x in raw.split(",") if x.strip()]


def rank_skills(signal: str, k: int) -> List[Tuple[str, float]]:
    _ensure_skills_index()
    assert _state.skills_index is not None
    return _state.skills_index.score(signal)[:k]


def handle_pre_llm_call(**kw: Any) -> Optional[Dict[str, str]]:
    if not enabled():
        return None
    session_id = str(kw.get("session_id") or "")
    user_message = kw.get("user_message")
    signal = (user_message if isinstance(user_message, str) else json.dumps(user_message, default=str))[:MAX_SIGNAL_CHARS]
    k = _int_env(K_SKILLS_ENV, DEFAULT_K_SKILLS)
    with _state.lock:
        s = _session(session_id)
        s["turn"] += 1
        s["signal"] = signal
        s["turn_id"] = str(kw.get("turn_id") or "")
        s["turn_started"] = time.time()
        turn = s["turn"]
        # A tool the model was told about on the shelf and named in this message joins the set.
        named = [t for t in s["hidden_tools"] if t in signal]
        for t in named:
            s["tools"][t] = turn
    ranked = rank_skills(signal, max(k, 1))
    pinned = _pinned(PINNED_SKILLS_ENV, ())
    with _state.lock:
        shown = _sticky_update(s["skills"], pinned + [n for n, _ in ranked], turn, k + len(pinned))
    by_name = {sk["name"]: sk for sk in _state.skills}
    record(
        "turn",
        session_id=session_id,
        turn=turn,
        turn_id=s["turn_id"],
        signal_sha=hashlib.sha256(signal.encode("utf-8")).hexdigest()[:SIGNAL_HASH_LEN],
        catalogue_skills=len(_state.skills),
        skills_ranked=[[n, round(sc, SCORE_DECIMALS)] for n, sc in ranked],
        skills_shown=shown,
        tools_named_from_shelf=named,
        k=k,
    )
    if mode() not in (MODE_SKILLS, MODE_SKILLS_TOOLS):
        return None
    lines = [SKILLS_HEADER, SKILLS_PREAMBLE]
    for name in shown:
        sk = by_name.get(name)
        if sk is None:
            continue
        lines.append(f"- {name}: {sk['description']}" if sk["description"] else f"- {name}")
    lines.append(SKILLS_FOOTER)
    return {"context": "\n".join(lines)}


def _tool_name(tool: Dict[str, Any]) -> str:
    fn = tool.get("function") if isinstance(tool, dict) else None
    if isinstance(fn, dict) and fn.get("name"):
        return str(fn["name"])
    return str(tool.get("name") or "")


def _tool_doc(tool: Dict[str, Any]) -> str:
    fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
    name = str(fn.get("name") or "")
    desc = str(fn.get("description") or "")
    params = fn.get("parameters") or fn.get("input_schema") or {}
    props = params.get("properties") if isinstance(params, dict) else None
    ptext = ""
    if isinstance(props, dict):
        ptext = " ".join(f"{k} {str(v.get('description') or '') if isinstance(v, dict) else ''}" for k, v in props.items())
    return f"{(name.replace('_', ' ') + ' ') * NAME_BOOST}{desc} {ptext}"


def _with_shelf(tool: Dict[str, Any], hidden: List[str]) -> Dict[str, Any]:
    """A copy of `tool` whose description carries the names of the tools not loaded this turn."""
    shelf = TOOLS_SHELF_TEXT.format(names=", ".join(hidden))
    copy = json.loads(json.dumps(tool))
    target = copy.get("function") if isinstance(copy.get("function"), dict) else copy
    target["description"] = str(target.get("description") or "") + shelf
    return copy


def filter_tools(agent: Any, tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Called by the patched conversation loop before each model request.

    Ranks and records in every mode so the shadow record covers tools; enforces only in
    skills+tools. Returns `tools` unchanged whenever it cannot or should not scope.
    """
    if not enabled() or not tools:
        return tools
    session_id = str(getattr(agent, "session_id", "") or "")
    names = [_tool_name(t) for t in tools]
    key = tuple(names)
    with _state.lock:
        s = _session(session_id)
        signal = s["signal"]
        turn = s["turn"]
        if _state.tools_index_key != key:
            _state.tools_index = BM25([(n, _tool_doc(t)) for n, t in zip(names, tools)])
            _state.tools_index_key = key
        index = _state.tools_index
    if not signal or index is None:
        return tools
    pinned = set(_pinned(PINNED_TOOLS_ENV, DEFAULT_PINNED_TOOLS))
    n = _int_env(N_TOOLS_ENV, DEFAULT_N_TOOLS)
    ranked = [name for name, _ in index.score(signal) if name not in pinned][:n]
    with _state.lock:
        extra = _sticky_update(s["tools"], ranked, turn, n)
        already = s["recorded_tools_turn"] == turn
        s["recorded_tools_turn"] = turn
        keep = pinned | set(extra)
        hidden = [name for name in names if name not in keep]
        s["hidden_tools"] = hidden
    enforce = mode() == MODE_SKILLS_TOOLS
    if not already:
        record(
            "tools",
            session_id=session_id,
            turn=turn,
            tools_total=len(tools),
            tools_shown=[name for name in names if name in keep],
            tools_hidden=hidden,
            enforced=enforce,
            n=n,
        )
    if not enforce or not hidden:
        return tools
    shown = [t for t, name in zip(tools, names) if name in keep]
    shown_names = [_tool_name(t) for t in shown]
    for carrier in SHELF_CARRIER_TOOLS:  # preference order, not array order
        if carrier in shown_names:
            i = shown_names.index(carrier)
            shown[i] = _with_shelf(shown[i], hidden)
            break
    return shown


def handle_pre_tool_call(tool_name: str = "", args: Optional[Dict[str, Any]] = None, **kw: Any) -> None:
    if not enabled():
        return None
    session_id = str(kw.get("session_id") or "")
    with _state.lock:
        s = _state.sessions.get(session_id)
        turn = s["turn"] if s else None
        started = s.get("turn_started") if s else None
        shown_skills = list(s["skills"]) if s else []
        shown_tools = set(s["tools"]) if s else set()
    skill = None
    if tool_name == "skill_view" and isinstance(args, dict):
        skill = args.get("name")
    pinned = set(_pinned(PINNED_TOOLS_ENV, DEFAULT_PINNED_TOOLS))
    record(
        "tool_call",
        session_id=session_id,
        turn=turn,
        tool_name=tool_name,
        skill=skill,
        since_turn_start=(round(time.time() - started, SCORE_DECIMALS) if started else None),
        skill_in_working_set=(skill in shown_skills) if skill else None,
        tool_in_working_set=(tool_name in shown_tools or tool_name in pinned),
    )
    return None


def handle_post_api_request(**kw: Any) -> None:
    if not enabled():
        return None
    usage = kw.get("usage")
    record(
        "api",
        session_id=str(kw.get("session_id") or ""),
        api_call_count=kw.get("api_call_count"),
        api_duration=kw.get("api_duration"),
        model=kw.get("model"),
        finish_reason=kw.get("finish_reason"),
        usage=usage if isinstance(usage, dict) else None,
    )
    return None
