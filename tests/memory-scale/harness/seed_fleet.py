#!/usr/bin/env python3
"""Seed the Meridian fleet corpus into the live Hindsight bank, one rung at a time.

Runs as a Kubernetes Job so seeding survives the laptop closing. Reads the
generated corpus mounted at --corpus and writes each document through the same
retain strategy and tags the production provider uses, so what lands in the bank
is what a real conversation would have produced.

Deliberately stdlib-only (urllib, no requests): it runs inside the agent image,
and depending on a package that image happens to carry today is how a test
harness breaks on an unrelated image roll.

The rung ladder
---------------
`--rung N` seeds every document whose rung is at or below N. Because the rungs
are nested, walking the ladder is just re-running with a larger N: the resume
logic below means each run writes only the delta. Rung 0 documents — the 250
per-user facts — are written at every rung, which is why the isolation probe is
meaningful even at the smallest one.

Resumption
----------
Every document carries a `context` naming its id. Hindsight stores and returns
`context` verbatim, so a re-run lists the bank, collects the contexts already
present, and skips them. Realistic prose for the extractor and an exact resume
key are the same string; nothing extra is stored to get it.

That matters more than it sounds. The failure this guards against is a Job that
dies at document 600 and, on retry, writes 600 duplicates into a bank whose whole
purpose is measuring retrieval precision.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Must match kube_agents_memory: DEFAULT_BANK_ID, SHARED_TAG, USER_TAG_PREFIX,
# PERSONAL_STRATEGY, SHARED_STRATEGY. Not imported — this runs as a bare
# subprocess with no Hermes profile on the path, exactly as memory_ttl_curator
# does, and for the same reason. The strategy check below catches drift at
# runtime rather than letting it silently change what gets extracted.
DEFAULT_BANK_ID = "kube-agents-memory"
SHARED_TAG = "scope:shared"
USER_TAG_PREFIX = "user:"
PERSONAL_STRATEGY = "personal"
SHARED_STRATEGY = "shared"

CONTEXT_SUFFIX = "(kube-agents fleet test)"
PAGE_SIZE = 200

DIRECTIVE = re.compile(r"^<!--\s*(id|scope|rung|order|title)\s*:\s*(.+?)\s*-->\s*$")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class Hindsight:
    """Minimal HTTP client. Mirrors memory_ttl_curator.Hindsight."""

    def __init__(self, base_url: str, tenant: str = "default"):
        self._base = base_url.rstrip("/")
        self._prefix = f"/v1/{tenant}"
        self._headers = {"Content-Type": "application/json"}

    def call(self, method: str, path: str, body: dict | None = None,
             timeout: int = 300, retries: int = 8) -> dict:
        """Issue a request, backing off on rate limits.

        Extraction runs through the shared LiteLLM pool. A 1,600-document seed is
        precisely the burst that pool answers with 429s, so retrying is the normal
        path here, not the exceptional one.
        """
        url = self._base + self._prefix + path
        data = json.dumps(body).encode() if body is not None else None
        delay = 5
        for attempt in range(retries):
            request = urllib.request.Request(url, data=data, method=method, headers=self._headers)
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

    def bank_config(self, bank_id: str) -> dict:
        return (self.call("GET", f"/banks/{bank_id}/config", timeout=60) or {}).get("config") or {}

    def units(self, bank_id: str) -> list[dict]:
        found, offset = [], 0
        while True:
            page = self.call("GET", f"/banks/{bank_id}/memories/list"
                                    f"?limit={PAGE_SIZE}&offset={offset}", timeout=120)
            items = page.get("items") or []
            found.extend(items)
            offset += len(items)
            if len(items) < PAGE_SIZE or offset >= int(page.get("total") or 0):
                return found

    def retain(self, bank_id: str, items: list[dict], timeout: int) -> dict:
        # Synchronous. "Async accepted" would let the Job exit reporting success
        # while the bank is still half-written, which is the one outcome that
        # would waste the whole run.
        return self.call("POST", f"/banks/{bank_id}/memories",
                         {"items": items, "async": False}, timeout=timeout)

    def consolidate(self, bank_id: str, timeout: int) -> dict:
        return self.call("POST", f"/banks/{bank_id}/consolidate", {}, timeout=timeout)


def parse_corpus(paths: list[Path]) -> list[dict]:
    """Turn the generated Markdown back into documents.

    Each document is a run of `<!-- key: value -->` directives followed by its
    body, terminated by the next directive block or end of file. The generator
    and this parser are the only two things that need to agree on the format, so
    a malformed block is a hard error rather than a skipped document — silently
    dropping a gold document would make every downstream number wrong in a way
    that looks like a provider result.
    """
    docs: list[dict] = []
    for path in paths:
        meta: dict[str, str] = {}
        body: list[str] = []

        def flush(where: str) -> None:
            if not meta and not any(b.strip() for b in body):
                return
            text = "\n".join(body).strip()
            if not text:
                raise SystemExit(f"{path.name}:{where}: document {meta.get('id')} has no body")
            for required in ("id", "scope", "rung"):
                if required not in meta:
                    raise SystemExit(f"{path.name}:{where}: document missing {required!r}")
            # The generator writes the bare scope ("shared" / "user:user07");
            # the bank wants the provider's tag form.
            scope = meta["scope"]
            if not scope.startswith(USER_TAG_PREFIX):
                scope = SHARED_TAG if scope in ("shared", SHARED_TAG) else f"scope:{scope}"
            docs.append({
                "id": meta["id"],
                "scope": scope,
                "rung": int(meta["rung"]),
                "title": meta.get("title", ""),
                "category": path.stem,
                "content": text,
                "context": _context_for(meta["id"], path.stem, scope),
            })
            meta.clear()
            body.clear()

        lines = path.read_text(encoding="utf-8").splitlines()
        for lineno, line in enumerate(lines, 1):
            match = DIRECTIVE.match(line)
            if match:
                # A directive after a body means the previous document ended.
                if body and any(b.strip() for b in body):
                    flush(str(lineno))
                meta[match.group(1)] = match.group(2)
                continue
            if line.startswith("# "):
                continue
            body.append(line)
        flush("EOF")
    return docs


def _context_for(doc_id: str, category: str, scope: str) -> str:
    """A human-readable context that doubles as an exact resume key."""
    if scope.startswith(USER_TAG_PREFIX):
        user = scope[len(USER_TAG_PREFIX):]
        return f"DM from {user} — personal note {doc_id} {CONTEXT_SUFFIX}"
    return f"Meridian platform {category} {doc_id} {CONTEXT_SUFFIX}"


def already_present(api: Hindsight, bank_id: str) -> set[str]:
    """Contexts already in the bank, so a re-run does not duplicate them."""
    contexts = {(u.get("context") or "").strip() for u in api.units(bank_id)}
    return {c for c in contexts if c.endswith(CONTEXT_SUFFIX)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--api-url", default=os.environ.get(
        "HINDSIGHT_API_URL",
        "http://hindsight-api.kubeagents-system.svc.cluster.local:8888"))
    ap.add_argument("--bank", default=os.environ.get("BANK_ID", DEFAULT_BANK_ID))
    ap.add_argument("--corpus", default="/corpus", help="directory of .md files")
    ap.add_argument("--rung", type=int, default=int(os.environ.get("RUNG", "0")),
                    help="seed documents at or below this rung (0 = all)")
    ap.add_argument("--batch", type=int, default=5, help="documents per retain call")
    ap.add_argument("--sleep", type=float, default=2.0, help="seconds between batches")
    ap.add_argument("--timeout", type=int, default=1800, help="per-retain timeout")
    ap.add_argument("--consolidate", action="store_true", default=True)
    ap.add_argument("--no-consolidate", dest="consolidate", action="store_false")
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

    if args.dry_run:
        for d in docs[:5]:
            log(f"  [{d['category']} r{d['rung']}] {d['scope']} :: {d['context']}\n"
                f"      {d['content'][:120]}")
        log(f"dry run — nothing written ({len(docs)} documents would be sent)")
        return 0

    api = Hindsight(args.api_url)

    # Fail loud if the bank is not the one the provider provisioned. Writing the
    # personal/shared strategy names when the bank does not define them would
    # silently fall back to the default mission, so every fact would be extracted
    # under the wrong instructions and the whole run would measure nothing.
    config = api.bank_config(args.bank)
    strategies = config.get("retain_strategies") or {}
    missing = [s for s in (PERSONAL_STRATEGY, SHARED_STRATEGY) if s not in strategies]
    if missing:
        sys.exit(f"bank {args.bank!r} has no {missing} retain strategy — is the provider deployed? "
                 f"present: {sorted(strategies)}")
    log(f"bank {args.bank}: strategies {sorted(strategies)} ✓")

    seen = already_present(api, args.bank)
    if seen:
        log(f"resume: {len(seen)} fleet-test contexts already in the bank, skipping those")
    pending = [d for d in docs if d["context"] not in seen]
    log(f"to write: {len(pending)} documents in batches of {args.batch}")

    written, failed, started = 0, 0, time.time()
    for i in range(0, len(pending), args.batch):
        batch = pending[i:i + args.batch]
        items = [{
            "content": d["content"],
            "context": d["context"],
            "tags": [d["scope"]],
            "strategy": SHARED_STRATEGY if d["scope"] == SHARED_TAG else PERSONAL_STRATEGY,
        } for d in batch]
        try:
            api.retain(args.bank, items, timeout=args.timeout)
        except RuntimeError as e:
            # One bad batch must not lose the rest. The context resume key means a
            # later re-run picks up exactly what is missing.
            log(f"  BATCH FAILED at {i} ({batch[0]['id']}..{batch[-1]['id']}): {e}")
            failed += len(batch)
            continue
        written += len(batch)
        elapsed = time.time() - started
        rate = written / elapsed if elapsed else 0
        remaining = (len(pending) - written - failed) / rate if rate else 0
        log(f"  {written}/{len(pending)} written "
            f"({elapsed / 60:.1f}m elapsed, ~{remaining / 60:.0f}m remaining)")
        time.sleep(args.sleep)

    log(f"seeding done: {written} written, {failed} failed, "
        f"in {(time.time() - started) / 60:.1f}m")

    if args.consolidate:
        # The point of the run, not an afterthought: consolidation is where 450
        # near-identical cluster facts either stay distinct or collapse into one
        # useless generalisation. Non-fatal — the facts are already durable, and
        # consolidation can be re-run by hand.
        log("consolidating (this is the slow part; failure here is not data loss)")
        try:
            api.consolidate(args.bank, timeout=args.timeout)
            log("consolidation done")
        except RuntimeError as e:
            log(f"consolidation failed (facts are still written): {e}")

    total = len(api.units(args.bank))
    log(f"RUNG {args.rung or 'all'} COMPLETE — bank {args.bank} now holds {total} memory units")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
