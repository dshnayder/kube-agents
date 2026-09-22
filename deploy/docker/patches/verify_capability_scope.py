#!/usr/bin/env python3
"""Behavioural check for apply_capability_scope.py, run in the image with cwd=/opt/hermes.

Grepping for the applier's own markers proves the text landed, not that the seams behave, so
this imports the patched modules from the Hermes checkout and drives three seams both ways in a
throwaway Hermes home: with the KA_* variables unset the functions must behave as shipped, and
with them set they must change in exactly the documented way. The fourth seam, the tool filter in
the conversation loop, is a call site inside the request loop and is checked by source position
only. Exit 1 with the failing seam named.
"""

import importlib
import os
import pathlib
import sys
import tempfile

INDEX_MODE_ENV = "KA_SKILLS_INDEX_MODE"
DESC_LIMIT_ENV = "KA_SKILL_DESC_LIMIT"
EXTRA_DIRS_ENV = "KA_EXTRA_SKILLS_DIRS"
HOME_ENV = "HERMES_HOME"
SHIPPED_LIMIT = 60
LONG_DESCRIPTION = "x" * (SHIPPED_LIMIT * 2)
SHORT_LIMIT = 10
FILTER_MARKER = "ka_capability_scope"
CONVERSATION_LOOP = "agent/conversation_loop.py"
TOOLS_ASSIGNMENT = "tools_for_api = agent.tools"
CACHE_PLAN_CALL = "build_prompt_cache_plan"
FILTER_WINDOW_BEFORE = 600   # characters between the tools assignment and the filter call
FILTER_WINDOW_AFTER = 1200   # characters between the filter call and the cache plan
CACHE_KEY_SEAMS = ('environ.get("%s", "")' % INDEX_MODE_ENV, 'environ.get("%s", "")' % DESC_LIMIT_ENV)
SNAPSHOT_GATE = "_ka_os.environ.get"
NAMES_ONLY_MARK = "[names only]"
PROBE_CATEGORY = "alpha"
PROBE_SKILL = "probe-skill-one"
PROBE_SKILL_TWO = "probe-skill-two"
PROBE_PHRASE = "Distinctive alpha phrase"
PROBE_SKILL_MD = "---\nname: %s\ndescription: %s\n---\n# %s\n"
TOOLS_FOR_INDEX = {"skills_list", "skill_view", "skill_manage", "terminal", "read_file"}
CONFIG_WITH_EXTERNAL = "skills:\n  external_dirs:\n    - %s\n"


def fail(seam: str, detail: str) -> None:
    print(f"verify_capability_scope: {seam}: {detail}", file=sys.stderr)
    sys.exit(1)


def clear() -> None:
    for key in (INDEX_MODE_ENV, DESC_LIMIT_ENV, EXTRA_DIRS_ENV):
        os.environ.pop(key, None)


def check_description_limit() -> None:
    clear()
    skill_utils = importlib.import_module("agent.skill_utils")
    shipped = skill_utils.extract_skill_description({"description": LONG_DESCRIPTION})
    if len(shipped) != SHIPPED_LIMIT or not shipped.endswith("..."):
        fail("description limit", f"unset env should keep the shipped limit; got {len(shipped)} chars")
    os.environ[DESC_LIMIT_ENV] = str(SHORT_LIMIT)
    short = skill_utils.extract_skill_description({"description": LONG_DESCRIPTION})
    if len(short) != SHORT_LIMIT:
        fail("description limit", f"{DESC_LIMIT_ENV}={SHORT_LIMIT} should truncate to {SHORT_LIMIT}; got {len(short)}")
    os.environ[DESC_LIMIT_ENV] = str(len(LONG_DESCRIPTION) + 1)
    full = skill_utils.extract_skill_description({"description": LONG_DESCRIPTION})
    if full != LONG_DESCRIPTION:
        fail("description limit", "a limit above the length should return the description whole")
    clear()


def _probe_skills_dir(home: pathlib.Path) -> pathlib.Path:
    skills = home / "skills"
    for rel, name in ((pathlib.Path(PROBE_CATEGORY) / PROBE_SKILL, PROBE_SKILL), (pathlib.Path(PROBE_SKILL_TWO), PROBE_SKILL_TWO)):
        d = skills / rel
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(PROBE_SKILL_MD % (name, PROBE_PHRASE, name), encoding="utf-8")
    return skills


def check_index_mode() -> None:
    """Render a two-skill index from a throwaway home: descriptions as shipped, names only with
    the variable set. The disk snapshot the first render writes must not leak into the second."""
    clear()
    prompt_builder = importlib.import_module("agent.prompt_builder")
    with tempfile.TemporaryDirectory() as tmp:
        skills = _probe_skills_dir(pathlib.Path(tmp))
        shipped = prompt_builder.build_skills_system_prompt(available_tools=TOOLS_FOR_INDEX, skills_dir_override=skills)
        if PROBE_PHRASE not in shipped or NAMES_ONLY_MARK in shipped:
            fail("index mode", f"unset env should render descriptions; got:\n{shipped[-600:]}")
        os.environ[INDEX_MODE_ENV] = "names"
        names = prompt_builder.build_skills_system_prompt(available_tools=TOOLS_FOR_INDEX, skills_dir_override=skills)
        if NAMES_ONLY_MARK not in names or PROBE_PHRASE in names or PROBE_SKILL not in names or PROBE_SKILL_TWO not in names:
            fail("index mode", f"{INDEX_MODE_ENV}=names should list every skill by name only; got:\n{names[-600:]}")
        clear()
    src = open(prompt_builder.__file__, encoding="utf-8").read()
    for seam in CACHE_KEY_SEAMS:
        if seam not in src:
            fail("index mode", f"cache key does not include {seam}")
    load_src = src[src.index("def _load_skills_snapshot"):src.index("def _write_skills_snapshot")]
    write_src = src[src.index("def _write_skills_snapshot"):]
    write_src = write_src[: write_src.index("\ndef ", 1)]
    if SNAPSHOT_GATE not in load_src or SNAPSHOT_GATE not in write_src:
        fail("snapshot gate", "the read or the write side of the skills snapshot is not gated on the KA_ variables")


def _external_dirs(skill_utils, home: pathlib.Path) -> list:
    os.environ[HOME_ENV] = str(home)
    skill_utils._external_dirs_cache_clear()
    return [str(p) for p in skill_utils.get_external_skills_dirs()]


def check_extra_dirs() -> None:
    """Both branches of the resolver: a home with no config.yaml (the early return) and a home
    whose config names an external dir (the merge path every deployed profile takes)."""
    clear()
    skill_utils = importlib.import_module("agent.skill_utils")
    saved_home = os.environ.get(HOME_ENV)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        bare_home, configured_home = root / "bare", root / "configured"
        from_config, from_env = root / "from-config", root / "from-env"
        for d in (bare_home, configured_home, from_config, from_env):
            d.mkdir()
        (configured_home / "config.yaml").write_text(CONFIG_WITH_EXTERNAL % from_config, encoding="utf-8")
        if _external_dirs(skill_utils, bare_home) != []:
            fail("extra dirs", "a home with no config and no env should resolve no external dirs")
        os.environ[EXTRA_DIRS_ENV] = str(from_env)
        if _external_dirs(skill_utils, bare_home) != [str(from_env.resolve())]:
            fail("extra dirs", "with no config, the env dir should be the only external dir")
        got = _external_dirs(skill_utils, configured_home)
        if got != [str(from_config.resolve()), str(from_env.resolve())]:
            fail("extra dirs", f"config dir then env dir expected; got {got}")
        clear()
        if _external_dirs(skill_utils, configured_home) != [str(from_config.resolve())]:
            fail("extra dirs", "with the env unset only the configured dir should remain")
    if saved_home is None:
        os.environ.pop(HOME_ENV, None)
    else:
        os.environ[HOME_ENV] = saved_home
    skill_utils._external_dirs_cache_clear()


def check_tool_filter_seam() -> None:
    src = open(CONVERSATION_LOOP, encoding="utf-8").read()
    if src.count(FILTER_MARKER) != 1:
        fail("tool filter", f"expected one {FILTER_MARKER} lookup in {CONVERSATION_LOOP}, found {src.count(FILTER_MARKER)}")
    idx = src.index(FILTER_MARKER)
    if TOOLS_ASSIGNMENT not in src[idx - FILTER_WINDOW_BEFORE:idx] or CACHE_PLAN_CALL not in src[idx:idx + FILTER_WINDOW_AFTER]:
        fail("tool filter", "the filter is not between the tools assignment and the cache plan")


def main() -> None:
    sys.path.insert(0, os.getcwd())
    check_description_limit()
    check_index_mode()
    check_extra_dirs()
    check_tool_filter_seam()
    print("verify_capability_scope: all seams behave")


if __name__ == "__main__":
    main()
