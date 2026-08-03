#!/usr/bin/env python3
"""Provision the `kube-agents-memory` bank exactly as the live provider would.

Why this exists
---------------
`_ensure_bank()` runs on the first memory operation of a session, not at gateway
startup, so after a PVC wipe the bank stays absent until a human sends a DM. The
seed job needs the bank's retain strategies to already exist — it exits rather
than write under the wrong extraction mission.

Why it is not a re-implementation
---------------------------------
`BANK_MISSION`, `RETAIN_STRATEGIES` and `PERSONAL_STRATEGY` are imported from the
real provider source rather than retyped here. If the provider's missions change,
this script changes with them; there is no second copy to drift. The two HTTP
calls mirror `_ensure_bank` exactly:

    client.create_bank(bank_id, mission=BANK_MISSION)
      -> PUT   /v1/default/banks/{bank_id}          {"mission": ...}
    client.set_bank_config(bank_id, retain_strategies=..., retain_default_strategy=...)
      -> PATCH /v1/default/banks/{bank_id}/config   {"retain_strategies": ..., ...}

Both are idempotent, which is the same property `_ensure_bank` relies on.
"""

import argparse
import json
import sys
import types
import urllib.error
import urllib.request
from pathlib import Path

PROVIDER_SRC = Path("/Users/dmitryshnayder/git/kube-agents/agents/chat/plugins/"
                    "memory/kube_agents_memory/__init__.py")


def load_constants(src: Path):
    """Import the provider module for its constants, stubbing Hermes imports."""
    if not src.is_file():
        sys.exit(f"provider source not found at {src}")

    mp = types.ModuleType("agent.memory_provider")
    mp.MemoryProvider = object
    agent_pkg = types.ModuleType("agent")
    agent_pkg.memory_provider = mp
    pm = types.ModuleType("plugins.memory")
    pm.load_memory_provider = lambda *a, **k: None
    plugins_pkg = types.ModuleType("plugins")
    plugins_pkg.memory = pm
    reg = types.ModuleType("tools.registry")
    reg.tool_error = lambda m: json.dumps({"success": False, "error": m})
    tools_pkg = types.ModuleType("tools")
    tools_pkg.registry = reg
    for name, mod in [("agent", agent_pkg), ("agent.memory_provider", mp),
                      ("plugins", plugins_pkg), ("plugins.memory", pm),
                      ("tools", tools_pkg), ("tools.registry", reg)]:
        sys.modules[name] = mod

    import importlib.util
    spec = importlib.util.spec_from_file_location("kube_agents_memory_ref", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def call(base: str, method: str, path: str, body: dict | None = None, timeout: int = 60):
    url = base.rstrip("/") + "/v1/default" + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read() or "{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} on {method} {path}: {e.read()[:400]!r}") from e


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--api-url", default="http://127.0.0.1:18888")
    ap.add_argument("--source", default=str(PROVIDER_SRC))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    m = load_constants(Path(a.source))
    bank = m.DEFAULT_BANK_ID
    print(f"bank            {bank}")
    print(f"mission         {len(m.BANK_MISSION)} chars, starts {m.BANK_MISSION[:60]!r}")
    print(f"strategies      {sorted(m.RETAIN_STRATEGIES)}")
    print(f"default         {m.PERSONAL_STRATEGY}")
    if a.dry_run:
        print("\ndry run — nothing written")
        return 0

    call(a.api_url, "PUT", f"/banks/{bank}", {"mission": m.BANK_MISSION})
    print("\ncreated bank with mission")

    # The config PATCH takes its fields nested under `updates`, not at the top
    # level (CreateBankRequest and BankConfigUpdate differ here).
    call(a.api_url, "PATCH", f"/banks/{bank}/config", {
        "updates": {
            "retain_strategies": m.RETAIN_STRATEGIES,
            "retain_default_strategy": m.PERSONAL_STRATEGY,
        }
    })
    print("applied retain strategies")

    # Verify against what the seeder will itself check, so a mismatch surfaces
    # here rather than as a confusing exit inside the Job.
    config = (call(a.api_url, "GET", f"/banks/{bank}/config") or {}).get("config") or {}
    got = config.get("retain_strategies") or {}
    if got != m.RETAIN_STRATEGIES:
        print(f"\nMISMATCH: bank has {sorted(got)}, provider defines "
              f"{sorted(m.RETAIN_STRATEGIES)}", file=sys.stderr)
        return 1
    if config.get("retain_default_strategy") != m.PERSONAL_STRATEGY:
        print(f"\nMISMATCH: default strategy is {config.get('retain_default_strategy')!r}",
              file=sys.stderr)
        return 1
    print(f"verified: strategies {sorted(got)}, default "
          f"{config.get('retain_default_strategy')!r} ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
