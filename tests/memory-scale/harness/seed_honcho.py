#!/usr/bin/env python3
"""Seed the Meridian fleet corpus into Honcho, one rung at a time.

The Hindsight counterpart is `seed_fleet.py`, and this deliberately reuses its
corpus parser so the two backends are fed from one definition of what a
document is. Everything below is about the mapping, which is where the two
systems genuinely differ.

Corpus to peers
---------------
Honcho has no tags. It has **peers** — the entities it models — so the corpus
scope maps onto them directly:

    scope: shared        -> peer `meridian-platform`, one session per category
    scope: user:userNN   -> peer `userNN`, session `dm-userNN`

That mapping is what makes the isolation probes meaningful. A workspace-wide
search returns messages from every peer, and the evaluator turns the returned
`peer_id` back into a `user:` / `scope:shared` tag, so a cross-user hit is
caught by the same tag check that guards Hindsight. Isolation is not
implemented for Honcho in this experiment — this ensures that shows up as a
measured leak rather than as a silent zero.

Information parity with the Hindsight seed
------------------------------------------
Hindsight receives two fields per document: `content` (the body) and `context`
(a human-readable line naming the document id). Honcho takes one string, so
each message is `f"{context}\\n\\n{body}"`. Both stores therefore see exactly
the same text, including the document id — anything less would hand one backend
a retrieval handle the other never got.

Settle time is the cost, and it is real
---------------------------------------
Seeding is not the end of the work. Every message enqueues a deriver work unit,
and nothing is fully scoreable until that queue drains — see `drain_honcho.py`.
Hindsight's equivalent cost is paid inline (extraction happens during `retain`),
so a like-for-like wall-clock number has to count both.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from seed_fleet import (  # noqa: E402  — same-directory sibling, see docstring
    CONTEXT_SUFFIX,
    SHARED_TAG,
    USER_TAG_PREFIX,
    parse_corpus,
)

SHARED_PEER = "meridian-platform"
DEFAULT_WORKSPACE = "meridian"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class Honcho:
    """Minimal HTTP client. Same shape and retry policy as seed_fleet.Hindsight."""

    def __init__(self, base_url: str, workspace: str):
        self._base = base_url.rstrip("/")
        self._ws = workspace
        self._headers = {"Content-Type": "application/json"}

    def call(self, method: str, path: str, body: dict | None = None,
             timeout: int = 300, retries: int = 8) -> dict:
        url = f"{self._base}/v3/workspaces/{self._ws}{path}"
        data = json.dumps(body).encode() if body is not None else None
        delay = 5
        for attempt in range(retries):
            request = urllib.request.Request(url, data=data, method=method,
                                             headers=self._headers)
            try:
                return json.loads(urllib.request.urlopen(request, timeout=timeout).read() or "{}")
            except urllib.error.HTTPError as e:
                detail = e.read()[:300]
                if e.code not in (429, 500, 502, 503, 504) or attempt == retries - 1:
                    raise RuntimeError(f"HTTP {e.code} on {method} {path}: {detail!r}") from e
                log(f"  HTTP {e.code} on {method} {path}; retrying in {delay}s")
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt == retries - 1:
                    raise RuntimeError(f"{type(e).__name__} on {method} {path}: {e}") from e
                log(f"  {type(e).__name__} on {method} {path}; retrying in {delay}s")
            time.sleep(delay)
            delay = min(delay * 2, 120)
        raise RuntimeError(f"exhausted retries on {method} {path}")

    def ensure_workspace(self) -> None:
        req = urllib.request.Request(
            f"{self._base}/v3/workspaces", method="POST", headers=self._headers,
            data=json.dumps({"name": self._ws}).encode())
        urllib.request.urlopen(req, timeout=60).read()

    def ensure_peer(self, peer: str) -> None:
        self.call("POST", "/peers", {"name": peer}, timeout=60)

    def ensure_session(self, session: str, peer: str) -> None:
        self.call("POST", "/sessions", {"name": session}, timeout=60)
        # Idempotent: adding a peer already on the session is not an error.
        self.call("POST", f"/sessions/{session}/peers", {peer: {}}, timeout=60)

    def session_lead_lines(self, session: str) -> set[str]:
        """First line of every message already in a session — the resume key."""
        seen: set[str] = set()
        page = 1
        while True:
            try:
                resp = self.call("POST", f"/sessions/{session}/messages/list"
                                         f"?page={page}&size=100", {}, timeout=120)
            except RuntimeError as e:
                if "HTTP 404" in str(e):
                    return seen          # session does not exist yet
                raise
            items = resp.get("items") or []
            for m in items:
                first = (m.get("content") or "").split("\n", 1)[0].strip()
                if first.endswith(CONTEXT_SUFFIX):
                    seen.add(first)
            if page >= int(resp.get("pages") or 1) or not items:
                return seen
            page += 1

    def post_messages(self, session: str, peer: str, contents: list[str],
                      timeout: int) -> dict:
        # The wire field is peer_id, aliasing peer_name (src/schemas/api.py:256).
        return self.call("POST", f"/sessions/{session}/messages",
                         {"messages": [{"peer_id": peer, "content": c} for c in contents]},
                         timeout=timeout)

    def queue_status(self) -> dict:
        return self.call("GET", "/queue/status", timeout=120)


def route(doc: dict) -> tuple[str, str]:
    """Map a document's scope onto (peer, session)."""
    scope = doc["scope"]
    if scope.startswith(USER_TAG_PREFIX):
        user = scope[len(USER_TAG_PREFIX):]
        return user, f"dm-{user}"
    return SHARED_PEER, f"platform-{doc['category']}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api-url", default=os.environ.get(
        "HONCHO_API_URL",
        "http://honcho-api.kubeagents-system.svc.cluster.local:8000"))
    ap.add_argument("--workspace", default=os.environ.get("HONCHO_WORKSPACE", DEFAULT_WORKSPACE))
    ap.add_argument("--corpus", default="/corpus", help="directory of .md files")
    ap.add_argument("--rung", type=int, default=int(os.environ.get("RUNG", "0")),
                    help="seed documents at or below this rung (0 = all)")
    ap.add_argument("--batch", type=int, default=25, help="messages per POST")
    ap.add_argument("--sleep", type=float, default=0.5, help="seconds between batches")
    ap.add_argument("--timeout", type=int, default=600, help="per-POST timeout")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    paths = sorted(Path(args.corpus).glob("*.md"))
    if not paths:
        sys.exit(f"no .md files under {args.corpus}")
    everything = parse_corpus(paths)
    docs = ([d for d in everything if d["rung"] <= args.rung] if args.rung else everything)
    shared = sum(1 for d in docs if d["scope"] == SHARED_TAG)
    log(f"corpus: {len(everything)} documents on disk; rung {args.rung or 'all'} selects "
        f"{len(docs)} ({shared} shared, {len(docs) - shared} personal)")

    # Group by destination before touching the network, so the peer/session
    # count is known up front and a dry run can show the whole mapping.
    grouped: dict[tuple[str, str], list[dict]] = {}
    for d in docs:
        grouped.setdefault(route(d), []).append(d)
    peers = sorted({p for p, _ in grouped})
    log(f"mapping: {len(peers)} peers, {len(grouped)} sessions")

    if args.dry_run:
        for (peer, session), items in sorted(grouped.items())[:5]:
            log(f"  {peer} / {session}: {len(items)} messages")
            log(f"      {items[0]['context']}")
        log(f"dry run — nothing written ({len(docs)} messages would be sent)")
        return 0

    api = Honcho(args.api_url, args.workspace)
    api.ensure_workspace()
    log(f"workspace {args.workspace} ✓")

    written, failed, skipped, started = 0, 0, 0, time.time()
    for n, ((peer, session), items) in enumerate(sorted(grouped.items()), 1):
        api.ensure_peer(peer)
        api.ensure_session(session, peer)

        seen = api.session_lead_lines(session)
        pending = [d for d in items if d["context"] not in seen]
        skipped += len(items) - len(pending)
        if not pending:
            continue

        for i in range(0, len(pending), args.batch):
            batch = pending[i:i + args.batch]
            # Information parity with the Hindsight seed: context line, then body.
            contents = [f"{d['context']}\n\n{d['content']}" for d in batch]
            try:
                api.post_messages(session, peer, contents, timeout=args.timeout)
            except RuntimeError as e:
                log(f"  BATCH FAILED {session} at {i} "
                    f"({batch[0]['id']}..{batch[-1]['id']}): {e}")
                failed += len(batch)
                continue
            written += len(batch)
            time.sleep(args.sleep)

        elapsed = time.time() - started
        rate = written / elapsed if elapsed else 0
        left = (len(docs) - written - failed - skipped) / rate if rate else 0
        log(f"  [{n}/{len(grouped)}] {session}: {written} written "
            f"({elapsed / 60:.1f}m elapsed, ~{left / 60:.0f}m remaining)")

    log(f"seeding done: {written} written, {skipped} already present, {failed} failed, "
        f"in {(time.time() - started) / 60:.1f}m")

    # The queue depth at exit is the honest headline: seeding has returned but
    # the corpus is not yet scoreable. drain_honcho.py is what waits on it.
    q = api.queue_status()
    log(f"deriver queue: {q.get('pending_work_units')} pending, "
        f"{q.get('in_progress_work_units')} in progress, "
        f"{q.get('completed_work_units')}/{q.get('total_work_units')} complete")
    log(f"RUNG {args.rung or 'all'} SEEDED — run drain_honcho.py before scoring")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
