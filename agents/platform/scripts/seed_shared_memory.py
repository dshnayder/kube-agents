#!/usr/bin/env python3
"""Provision the shared memory bank and bulk-load the org corpus into it.

The `kage_memory` provider gives every user a private Hindsight bank plus one
bank shared by everyone. The shared bank is the org's knowledge — SOPs,
conventions, timezone and on-call facts, release and change history — and is
expected to be much larger than any personal bank. Two things follow from that,
and neither is reachable from the agent:

1. **The bank needs a mission.** Hindsight uses a bank's `mission` to decide
   what is worth keeping and its `retain_mission` to shape extraction. The
   Hermes plugin exposes `bank_mission`/`bank_retain_mission` config keys but
   never sends them anywhere — they are read into attributes nothing reads back
   (`plugins/memory/hindsight/__init__.py:1308-1309`). The mission is a property
   of the bank, so it is set here, once, through the API.

2. **The corpus is loaded out of band.** `memory_retain` takes one string at a
   time and auto-retain is deliberately off for the shared bank. Seeding a
   document set through the chat agent would be absurd; the client's
   `retain_files` and `retain_batch` take the whole set in one call.

Run it from the gateway pod, which already has `hindsight_client` and can reach
the Hindsight service:

    kubectl exec -n kubeagents-system deploy/platform-agent-gateway \
        -c platform-agent -- /opt/hermes/.venv/bin/python3 \
        /opt/data/scripts/seed_shared_memory.py --mission-only

    # …then load a corpus (paths are read inside the pod):
    … seed_shared_memory.py --files /opt/data/corpus/*.md
    … seed_shared_memory.py --text "Release 0.9 shipped 2026-07-14."

Connection settings come from $HERMES_HOME/hindsight/config.json — the same file
the provider reads — so there is no second place to keep the URL in sync.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path

DEFAULT_BANK_ID = "kage-shared"

# What the bank is for. Hindsight weighs candidate facts against this, so it is
# the main lever on what a large corpus admits and what it drops.
MISSION = (
    "The shared operational knowledge of this organisation's Kubernetes "
    "platform team, readable by every user of the Kage agent. It holds "
    "standard operating procedures, platform conventions and defaults, "
    "on-call and timezone facts, cluster and environment inventory, and the "
    "history of releases and infrastructure changes. Facts here are true for "
    "everybody. Nothing about an individual person belongs in this bank: "
    "personal preferences, one user's clusters, and anything phrased about "
    "'me' or 'my' belong in that user's own bank instead."
)

# How to extract from the corpus. Documents, not conversation — the default
# extraction assumes dialogue and would keep asides that read as commitments.
RETAIN_MISSION = (
    "Extract durable, self-contained operational facts. Each fact must stand "
    "alone without the surrounding document: name the cluster, environment, "
    "component, version or date it is about rather than saying 'this' or "
    "'the above'. Keep procedures, thresholds, ownership, defaults, and dated "
    "changes. Preserve exact identifiers, versions and dates verbatim. Drop "
    "narrative framing, TODOs, unresolved proposals, and anything true only "
    "while a document was being written."
)

# Reflect over an org corpus should answer from the record, not infer about a
# person the way the default (conversation-derived) mission does.
REFLECT_MISSION = (
    "Answer from the organisation's recorded operational knowledge. Cite the "
    "specific procedure, version or dated change that supports the answer, and "
    "say plainly when the record does not cover the question rather than "
    "generalising from adjacent facts."
)


def load_hindsight_config() -> dict:
    """Read the provider's own config so this script cannot drift from it."""
    home = os.environ.get("HERMES_HOME", "/opt/data")
    path = Path(home) / "hindsight" / "config.json"
    if not path.exists():
        sys.exit(f"No Hindsight config at {path}. Is the provider deployed?")
    try:
        return json.loads(path.read_text()) or {}
    except (OSError, ValueError) as e:
        sys.exit(f"Could not read {path}: {e}")


def connect(config: dict):
    try:
        from hindsight_client import Hindsight
    except ImportError:
        sys.exit(
            "hindsight_client is not importable. Run this inside the gateway "
            "pod's venv: /opt/hermes/.venv/bin/python3"
        )
    api_url = str(config.get("api_url") or "").strip()
    if not api_url:
        sys.exit("No api_url in the Hindsight config.")
    api_key = config.get("api_key") or config.get("apiKey") or None
    return Hindsight(base_url=api_url, api_key=api_key)


def ensure_bank(client, bank_id: str) -> None:
    """Create the bank with its mission, or update the mission if it exists.

    create_bank is not idempotent, so an existing bank takes the update path.
    Both are safe to re-run: the mission is declarative, not appended.
    """
    try:
        client.get_bank_config(bank_id)
    except Exception:
        client.create_bank(
            bank_id=bank_id,
            name="Kage shared memory",
            mission=MISSION,
            retain_mission=RETAIN_MISSION,
            reflect_mission=REFLECT_MISSION,
        )
        print(f"created bank {bank_id} with mission")
        return
    client.set_mission(bank_id, MISSION)
    client.update_bank_config(bank_id, retain_mission=RETAIN_MISSION)
    client.set_reflect_mission(bank_id, REFLECT_MISSION)
    print(f"updated mission on existing bank {bank_id}")


def retain_files(client, bank_id: str, patterns: list[str], context: str) -> None:
    paths: list[str] = []
    for pattern in patterns:
        matched = sorted(glob.glob(pattern))
        if not matched:
            print(f"warning: no files matched {pattern!r}", file=sys.stderr)
        paths.extend(matched)
    if not paths:
        sys.exit("No files to ingest.")
    print(f"ingesting {len(paths)} file(s) into {bank_id}…")
    response = client.retain_files(bank_id=bank_id, files=paths, context=context)
    print(response)


def retain_text(client, bank_id: str, texts: list[str], context: str) -> None:
    items = [{"content": text, "context": context} for text in texts]
    print(f"retaining {len(items)} item(s) into {bank_id}…")
    print(client.retain_batch(bank_id=bank_id, items=items))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bank-id", default=None,
                        help=f"Shared bank id (default: from config, else {DEFAULT_BANK_ID})")
    parser.add_argument("--mission-only", action="store_true",
                        help="Create/refresh the bank and its mission, ingest nothing.")
    parser.add_argument("--files", nargs="+", metavar="GLOB",
                        help="Documents to ingest (paths resolved where this runs).")
    parser.add_argument("--text", nargs="+", metavar="FACT",
                        help="Literal facts to retain.")
    parser.add_argument("--context", default="organisation-wide operational knowledge",
                        help="Provenance label attached to what is ingested.")
    args = parser.parse_args()

    if not (args.mission_only or args.files or args.text):
        parser.error("nothing to do: pass --mission-only, --files, or --text")

    config = load_hindsight_config()
    bank_id = args.bank_id or config.get("shared_bank_id") or config.get("bank_id") or DEFAULT_BANK_ID
    client = connect(config)
    try:
        ensure_bank(client, bank_id)
        if args.files:
            retain_files(client, bank_id, args.files, args.context)
        if args.text:
            retain_text(client, bank_id, args.text, args.context)
    finally:
        client.close()


if __name__ == "__main__":
    main()
