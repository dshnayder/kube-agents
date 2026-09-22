#!/usr/bin/env python3
"""Build-time patch for the capability-scoping experiment (bench/experiments/capability-scope-ab/).

Three small, environment-gated changes to the Hermes runtime and one call site the plugin owns.
With none of the variables set and the plugin disabled the runtime behaves exactly as shipped,
which is what makes one image serve every arm of the A/B.

  KA_SKILLS_INDEX_MODE=names   render every skill category in the system-prompt index as a
                               names-only line (the design's "shelf").
  KA_SKILL_DESC_LIMIT=<int>    override the 60-character description truncation in the index.
  KA_EXTRA_SKILLS_DIRS=<a:b>   extra skill directories, appended to skills.external_dirs, so a
                               larger catalogue can be mounted without touching the synced set.
  (plugin)                     the conversation loop hands the tool array to the capability_scope
                               plugin's filter_tools() before each request when the plugin is
                               loaded; the plugin returns it unchanged unless
                               KA_SCOPE_MODE=skills+tools.

Usage: apply_capability_scope.py /opt/hermes
"""

import pathlib
import sys

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes")

PROMPT_BUILDER = ROOT / "agent" / "prompt_builder.py"
SKILL_UTILS = ROOT / "agent" / "skill_utils.py"
CONVERSATION_LOOP = ROOT / "agent" / "conversation_loop.py"

INDEX_MODE_ENV = "KA_SKILLS_INDEX_MODE"
DESC_LIMIT_ENV = "KA_SKILL_DESC_LIMIT"
EXTRA_DIRS_ENV = "KA_EXTRA_SKILLS_DIRS"


def replace_once(path: pathlib.Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if new in text:
        return
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected exactly one match for patch anchor, found {count}:\n{old}")
    path.write_text(text.replace(old, new), encoding="utf-8")


# 1. Names-only index and cache key. ------------------------------------------------------------
replace_once(
    PROMPT_BUILDER,
    """    demoted = frozenset(
        cat for cat in skills_by_category
        if cat.split("/", 1)[0] in (compact_categories or frozenset())
    )
""",
    """    demoted = frozenset(
        cat for cat in skills_by_category
        if cat.split("/", 1)[0] in (compact_categories or frozenset())
    )
    # capability_scope experiment: names-only shelf for every category.
    import os as _ka_os
    if (_ka_os.environ.get("%s") or "").strip().lower() == "names":
        demoted = frozenset(skills_by_category)
"""
    % INDEX_MODE_ENV,
)
replace_once(
    PROMPT_BUILDER,
    """        hidden_note = (
            "\\n(Categories marked [names only] are outside the current coding "
            "context, so their descriptions are omitted — the skills work "
            "normally and load with skill_view(name) as usual.)"
        )
""",
    """        hidden_note = (
            "\\n(Categories marked [names only] are outside the current coding "
            "context, so their descriptions are omitted — the skills work "
            "normally and load with skill_view(name) as usual.)"
        )
        if (_ka_os.environ.get("%s") or "").strip().lower() == "names":
            hidden_note = (
                "\\n(Skills are listed by name only. The descriptions of the skills relevant "
                "to the current request arrive with each message under "
                "[SKILLS RELEVANT TO THIS REQUEST]; every skill loads with skill_view(name), "
                "and skills_list shows all descriptions.)"
            )
"""
    % INDEX_MODE_ENV,
)
replace_once(
    PROMPT_BUILDER,
    """        _platform_hint,
        tuple(sorted(disabled)),
        tuple(sorted(compact_categories or ())),
    )
""",
    """        _platform_hint,
        tuple(sorted(disabled)),
        tuple(sorted(compact_categories or ())),
        __import__("os").environ.get("%s", ""),
        __import__("os").environ.get("%s", ""),
    )
"""
    % (INDEX_MODE_ENV, DESC_LIMIT_ENV),
)
# The disk snapshot is keyed on the skills directory manifest alone and stores descriptions
# already cut to the limit, so both sides are gated: an arm with the variables set neither reads
# a snapshot another arm wrote nor writes one for the next arm to read.
replace_once(
    PROMPT_BUILDER,
    """    snapshot_path = _skills_prompt_snapshot_path()
    if not snapshot_path.exists():
        return None
""",
    """    snapshot_path = _skills_prompt_snapshot_path()
    import os as _ka_os
    if _ka_os.environ.get("%s") or _ka_os.environ.get("%s"):
        return None
    if not snapshot_path.exists():
        return None
"""
    % (INDEX_MODE_ENV, DESC_LIMIT_ENV),
)
replace_once(
    PROMPT_BUILDER,
    """    \"\"\"Persist skill metadata to disk for fast cold-start reuse.\"\"\"
    payload = {
        "version": _SKILLS_SNAPSHOT_VERSION,
""",
    """    \"\"\"Persist skill metadata to disk for fast cold-start reuse.\"\"\"
    import os as _ka_os
    if _ka_os.environ.get("%s") or _ka_os.environ.get("%s"):
        return
    payload = {
        "version": _SKILLS_SNAPSHOT_VERSION,
"""
    % (INDEX_MODE_ENV, DESC_LIMIT_ENV),
)

# 2. Description limit. -------------------------------------------------------------------------
replace_once(
    SKILL_UTILS,
    """    desc = _normalize_skill_description(frontmatter)
    if not desc:
        return ""
    if len(desc) > SKILL_PROMPT_DESC_LIMIT:
        return desc[:SKILL_PROMPT_DESC_LIMIT - 3] + "..."
    return desc
""",
    """    desc = _normalize_skill_description(frontmatter)
    if not desc:
        return ""
    import os as _ka_os
    try:
        _ka_limit = int(_ka_os.environ.get("%s") or SKILL_PROMPT_DESC_LIMIT)
    except ValueError:
        _ka_limit = SKILL_PROMPT_DESC_LIMIT
    if len(desc) > _ka_limit:
        return desc[:_ka_limit - 3] + "..."
    return desc
"""
    % DESC_LIMIT_ENV,
)

# 3. Extra skill directories. -------------------------------------------------------------------
# The resolver returns early when there is no config.yaml, when it does not parse, and when it has
# no `skills:` block; the env directories have to survive all three, so a helper supplies them on
# every early return and the normal path merges them into the configured list.
replace_once(
    SKILL_UTILS,
    """def get_external_skills_dirs() -> List[Path]:
""",
    """def _ka_extra_skill_dirs() -> List[Path]:
    \"\"\"capability_scope experiment: directories named by %s that exist.\"\"\"
    import os as _ka_os
    out: List[Path] = []
    for d in (_ka_os.environ.get("%s") or "").split(_ka_os.pathsep):
        p = Path(_ka_os.path.expanduser(d.strip())) if d.strip() else None
        if p is not None and p.is_dir() and p.resolve() not in [q.resolve() for q in out]:
            out.append(p.resolve())
    return out


def get_external_skills_dirs() -> List[Path]:
"""
    % (EXTRA_DIRS_ENV, EXTRA_DIRS_ENV),
)
replace_once(
    SKILL_UTILS,
    """    config_path = get_config_path()
    if not config_path.exists():
        return []
""",
    """    config_path = get_config_path()
    if not config_path.exists():
        return _ka_extra_skill_dirs()
""",
)
replace_once(
    SKILL_UTILS,
    """    parsed = _load_raw_config()
    if not parsed:
        return []

    skills_cfg = parsed.get("skills")
    if not isinstance(skills_cfg, dict):
        return []
""",
    """    parsed = _load_raw_config()
    if not parsed:
        return _ka_extra_skill_dirs()

    skills_cfg = parsed.get("skills")
    if not isinstance(skills_cfg, dict):
        skills_cfg = {}
""",
)
replace_once(
    SKILL_UTILS,
    """    raw_dirs = skills_cfg.get("external_dirs")
    if not raw_dirs:
""",
    """    raw_dirs = skills_cfg.get("external_dirs")
    _ka_extra = [str(p) for p in _ka_extra_skill_dirs()]
    if _ka_extra:
        if isinstance(raw_dirs, str):
            raw_dirs = [raw_dirs]
        raw_dirs = list(raw_dirs or []) + _ka_extra
    if not raw_dirs:
""",
)

# 4. Tool array filter. -------------------------------------------------------------------------
replace_once(
    CONVERSATION_LOOP,
    """        tools_for_api = agent.tools
        if agent._use_prompt_caching and agent.provider != "moa":
""",
    """        tools_for_api = agent.tools
        # capability_scope experiment: per-turn tool working set (identity unless enabled).
        try:
            import sys as _ka_sys
            _ka_scope = _ka_sys.modules.get("ka_capability_scope")
            if _ka_scope is not None:
                tools_for_api = _ka_scope.filter_tools(agent, list(agent.tools))
        except Exception as _ka_exc:
            logger.warning("capability_scope filter skipped: %s", _ka_exc)
        if agent._use_prompt_caching and agent.provider != "moa":
""",
)

for path in (PROMPT_BUILDER, SKILL_UTILS, CONVERSATION_LOOP):
    compile(path.read_text(encoding="utf-8"), str(path), "exec")
print("capability_scope patch applied")
