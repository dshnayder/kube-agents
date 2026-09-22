#!/usr/bin/env python3
"""Behavioural check for apply_capability_scope.py, run in the image with cwd=/opt/hermes.

Grepping for the applier's own markers proves the text landed, not that the seams behave, so
this imports the patched modules from the Hermes checkout and drives each seam both ways: with
the KA_* variables unset the functions must behave as shipped, and with them set they must
change in exactly the documented way. Exit 1 with the failing seam named.
"""

import importlib
import os
import sys

INDEX_MODE_ENV = "KA_SKILLS_INDEX_MODE"
DESC_LIMIT_ENV = "KA_SKILL_DESC_LIMIT"
EXTRA_DIRS_ENV = "KA_EXTRA_SKILLS_DIRS"
SHIPPED_LIMIT = 60
LONG_DESCRIPTION = "x" * (SHIPPED_LIMIT * 2)
SHORT_LIMIT = 10
FILTER_MARKER = "ka_capability_scope"
CONVERSATION_LOOP = "agent/conversation_loop.py"
SNAPSHOT_MARKER = "_ka_os.environ.get"


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


def check_index_mode_and_snapshot_gates() -> None:
    """The names-only render and both snapshot gates read the same variable; the render is
    checked through the source seam it lives in, since building a full index needs a skills tree."""
    clear()
    prompt_builder = importlib.import_module("agent.prompt_builder")
    src = open(prompt_builder.__file__, encoding="utf-8").read()
    if src.count(f'environ.get("{INDEX_MODE_ENV}")') < 2:
        fail("index mode", "names-only seam or its cache key is missing from prompt_builder")
    if "demoted = frozenset(skills_by_category)" not in src:
        fail("index mode", "names-only demotion of every category is missing")
    load_src = src[src.index("def _load_skills_snapshot"):src.index("def _write_skills_snapshot")]
    write_src = src[src.index("def _write_skills_snapshot"):]
    write_src = write_src[: write_src.index("\ndef ", 1)]
    if SNAPSHOT_MARKER not in load_src or SNAPSHOT_MARKER not in write_src:
        fail("snapshot gate", "the read or the write side of the skills snapshot is not gated on the KA_ variables")


def check_extra_dirs() -> None:
    clear()
    skill_utils = importlib.import_module("agent.skill_utils")
    if hasattr(skill_utils, "_external_dirs_cache_clear"):
        skill_utils._external_dirs_cache_clear()
    baseline = list(skill_utils.get_external_skills_dirs())
    os.environ[EXTRA_DIRS_ENV] = os.getcwd()  # any existing directory outside the local skills dir
    if hasattr(skill_utils, "_external_dirs_cache_clear"):
        skill_utils._external_dirs_cache_clear()
    with_extra = list(skill_utils.get_external_skills_dirs())
    if len(with_extra) != len(baseline) + 1:
        fail("extra dirs", f"{EXTRA_DIRS_ENV} should add one directory; before {baseline}, after {with_extra}")
    clear()


def check_tool_filter_seam() -> None:
    src = open(CONVERSATION_LOOP, encoding="utf-8").read()
    if src.count(FILTER_MARKER) != 1:
        fail("tool filter", f"expected one {FILTER_MARKER} lookup in {CONVERSATION_LOOP}, found {src.count(FILTER_MARKER)}")
    idx = src.index(FILTER_MARKER)
    if "tools_for_api = agent.tools" not in src[idx - 600:idx] or "build_prompt_cache_plan" not in src[idx:idx + 1200]:
        fail("tool filter", "the filter is not between the tools assignment and the cache plan")


def main() -> None:
    sys.path.insert(0, os.getcwd())
    check_description_limit()
    check_index_mode_and_snapshot_gates()
    check_extra_dirs()
    check_tool_filter_seam()
    print("verify_capability_scope: all seams behave")


if __name__ == "__main__":
    main()
