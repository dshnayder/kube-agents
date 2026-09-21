"""Hermes plugin entry point for the capability-scoping prototype (see scope.py)."""

import sys

from . import scope

# The patched conversation loop finds the filter through sys.modules rather than by package
# name, because Hermes loads plugins by path and the import name is not stable.
sys.modules.setdefault("ka_capability_scope", scope)


def register(ctx):
    ctx.register_hook("pre_llm_call", scope.handle_pre_llm_call)
    ctx.register_hook("pre_tool_call", scope.handle_pre_tool_call)
    ctx.register_hook("post_api_request", scope.handle_post_api_request)
    ctx.register_hook("on_session_end", scope.handle_session_end)


__all__ = ["register", "scope"]
