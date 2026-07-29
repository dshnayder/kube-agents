#!/usr/bin/env python3
"""Bulk-load the org corpus into the shared memory bank.

The `kage_memory` provider gives every user a private Hindsight bank plus one
bank shared by everyone. The shared bank is the org's knowledge — SOPs,
conventions, timezone and on-call facts, release and change history — and is
expected to be much larger than any personal bank. Filling it is not something
the agent can do: `memory_retain` takes one string at a time and auto-retain is
deliberately off for the shared bank, so seeding a document set through chat
would be absurd. The client's `retain_files` and `retain_batch` take the whole
set in one call, which is what this does.

This script does **not** set the bank's mission. The provider does that itself
when a session starts (`kage_memory._ensure_mission`), for the shared and
personal banks alike, so there is no provisioning step to run at install time
and nothing to redo when Hindsight's database is rebuilt.

Run it from the gateway pod, which already has `hindsight_client` and can reach
the Hindsight service (paths are resolved inside the pod):

    kubectl exec -n kubeagents-system deploy/platform-agent-gateway \
        -c platform-agent -- /opt/hermes/.venv/bin/python3 \
        /opt/data/scripts/seed_shared_memory.py --files '/opt/data/corpus/*.md'

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
    parser.add_argument("--files", nargs="+", metavar="GLOB",
                        help="Documents to ingest (paths resolved where this runs).")
    parser.add_argument("--text", nargs="+", metavar="FACT",
                        help="Literal facts to retain.")
    parser.add_argument("--context", default="organisation-wide operational knowledge",
                        help="Provenance label attached to what is ingested.")
    args = parser.parse_args()

    if not (args.files or args.text):
        parser.error("nothing to do: pass --files or --text")

    config = load_hindsight_config()
    bank_id = args.bank_id or config.get("shared_bank_id") or config.get("bank_id") or DEFAULT_BANK_ID
    client = connect(config)
    try:
        if args.files:
            retain_files(client, bank_id, args.files, args.context)
        if args.text:
            retain_text(client, bank_id, args.text, args.context)
    finally:
        client.close()


if __name__ == "__main__":
    main()
