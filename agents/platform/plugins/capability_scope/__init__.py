"""Hermes plugin entry point for the capability-scoping prototype (see scope.py)."""

import logging
import os
import pathlib
import sys

CONTROL_FILE_ENV = "KA_SCOPE_CONTROL_FILE"
DEFAULT_CONTROL_FILE = "/opt/data/capability_scope.env"
CONTROL_PREFIXES = ("KA_", "HERMES_MAX_ITERATIONS")

logger = logging.getLogger("hermes.plugin.capability_scope")


def _load_control_file() -> None:
    """Load the arm's variables from a file on the data volume.

    The operator does not pass spec.deployment.env to the gateway container, so the experiment
    switches arms by rewriting this file and restarting the gateway. Only experiment keys are
    read, and a value already present in the environment wins.
    """
    path = pathlib.Path(os.environ.get(CONTROL_FILE_ENV, DEFAULT_CONTROL_FILE))
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith(CONTROL_PREFIXES) and key not in os.environ:
            os.environ[key] = value.strip()
    logger.info("capability_scope: control file %s loaded", path)


_load_control_file()

from . import scope  # noqa: E402  (after the control file so scope reads the arm's values)

# The patched conversation loop finds the filter through sys.modules rather than by package
# name, because Hermes loads plugins by path and the import name is not stable.
sys.modules.setdefault("ka_capability_scope", scope)


def register(ctx):
    ctx.register_hook("pre_llm_call", scope.handle_pre_llm_call)
    ctx.register_hook("pre_tool_call", scope.handle_pre_tool_call)
    ctx.register_hook("post_api_request", scope.handle_post_api_request)
    ctx.register_hook("on_session_end", scope.handle_session_end)


__all__ = ["register", "scope"]
