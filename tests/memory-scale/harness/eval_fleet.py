#!/usr/bin/env python3
"""Score the memory providers against the same 26-probe answer key.

What is being compared
----------------------
Not "the retrieval API" against "a file". The comparable unit is **what the
model sees when it answers**: for Hindsight that is the recall result set, for
`multiuser_memory` it is the entire `system_prompt_block()`, and for Honcho it
is the budget-matched message search result set. All three are scored by the
same code against the same `queries.json`, so every number below is a property
of the architecture rather than of the harness.

Honcho is scored with per-user isolation deliberately not implemented, so its
two isolation probes are expected to fail; `--allow-leaks` records that without
aborting the ladder. See `HonchoBackend` for why the message-search surface is
the one scored.

The four metrics, and what each one is worth
--------------------------------------------
`gold_recall` — of the documents the answer key names as necessary, how many are
present in the context, matched on document ID. Treat it as a **strict lower
bound on Hindsight**, not a like-for-like recall figure: Hindsight stores
paraphrased observations, and a measured 45/82 units retain the source ID in
their text, so a gold document whose substance survived but whose ID was
stripped scores as a miss. (The unit's `context` field is not a fallback — it
comes back `None` on recall.) File-based ships raw text, so its IDs always
survive and its recall is 1.0 by construction. The metric therefore flatters
file-based by design; `must_contain` below is the fairer substance check, and
both are reported so the gap between them is visible rather than chosen.

`ordering` — the decisive one for supersession, and the only metric that reads
the same way for both architectures. Retrieval does *not* make superseded
content disappear: a live recall for the service-account-key policy returns
ADR-2026-052 ("banned outright") and ADR-2024-014 ("rotated every 90 days")
together. So the question is not whether the stale version is present but
whether the current one comes first. Measured as the character offset of the
first `must_contain` hit versus the first `must_not_contain` hit in the context
the model receives — which works for a ranked result set and for a flat file
alike, and needs no cooperation from either provider.

`contamination` — how many `must_not_contain` strings are in the context. These
are the *superseded* values: the 90-day key rotation that ADR-2026-052 banned,
the nginx ingress that ADR-2026-047 removed. A provider that ships all three
versions of a policy has not answered the question, it has handed the model a
contradiction and hoped. This is the metric that inverts the recall result, and
it is why "perfect recall" and "correct answer" are not the same claim.

`must_contain` — a lexical proxy for whether the answer is *derivable* from the
context. Weaker than the other two: Hindsight stores paraphrases, so a dropped
synonym scores as a miss even when the fact survived. Read it as a floor on
Hindsight and a ceiling on nothing; `gold_recall` is the honest recall number.

`cost` — characters and estimated tokens of context consumed, per turn. The
whole crossover lives here.

Isolation is not scored, it is enforced
---------------------------------------
Any result tagged `user:X` returned to an identity that is not X is a leak, and
the script exits non-zero. That check reads the tags on the returned units
directly rather than looking for names in prose, so it cannot be passed by a
paraphrase that happens to omit the name.
"""

import argparse
import json
import re
import sys
import types
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

SHARED_TAG = "scope:shared"
USER_TAG_PREFIX = "user:"
TAGS_MATCH = "any_strict"          # 'any' would also match untagged rows
RECALL_TYPES = ["observation"]     # the only type the provider surfaces
DEFAULT_BUDGET = "mid"
TOK_PER_CHAR = 0.25                # stated estimator; chars below are exact

REF_FILE_PROVIDER = Path("/tmp/scaletest/multiuser_memory_ref.py")

# Honcho returns whole documents, Hindsight returns short extracted
# observations, so the two are held equal on context size rather than on result
# count. 18,000 chars is 4,500 estimated tokens at TOK_PER_CHAR, which brackets
# the 4,264-4,588 tok/turn Hindsight actually consumed at budget=mid across all
# five rungs (see results/hindsight-r*.json).
HONCHO_CHAR_BUDGET = 18_000
HONCHO_WORKSPACE = "meridian"


# --------------------------------------------------------------------------
# backends: each returns the context string a model would see for one probe
# --------------------------------------------------------------------------

class HindsightBackend:
    """Recall against the live bank, exactly as the provider's `both` scope does."""

    name = "hindsight"

    def __init__(self, api_url: str, bank: str, budget: str = DEFAULT_BUDGET):
        self.api_url, self.bank, self.budget = api_url.rstrip("/"), bank, budget

    def _call(self, path: str, body: dict, timeout: int = 180):
        url = f"{self.api_url}/v1/default/banks/{self.bank}{path}"
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        try:
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read() or "{}")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code} on POST {path}: {e.read()[:300]!r}") from e

    def context_for(self, query: str, user: str) -> tuple[str, list[dict]]:
        tags = [f"{USER_TAG_PREFIX}{user}", SHARED_TAG] if user else [SHARED_TAG]
        resp = self._call("/memories/recall", {
            "query": query,
            "types": RECALL_TYPES,
            "budget": self.budget,
            "tags": tags,
            "tags_match": TAGS_MATCH,
        })
        results = resp.get("results") or []
        # The model sees the unit texts. Context lines are joined the way the
        # plugin renders them, so the character count is the real one.
        return "\n".join(r.get("text") or "" for r in results), results


class HonchoBackend:
    """Workspace-wide message search, budget-matched to Hindsight's `mid` recall.

    Which surface, and why this one
    -------------------------------
    Honcho exposes several readable surfaces (raw message search, derived
    conclusions, a peer representation, and a dialectic LLM answer). This scores
    `POST /v3/workspaces/{ws}/search` — hybrid semantic + keyword over messages —
    for two reasons. It is the path the Hermes provider's own `honcho_search`
    tool calls, so it is the product behaviour rather than a capability demo.
    And it is the only retrieval surface that needs no per-peer scoping:
    `conclusions/query` **requires** `observer` and `observed` filters, and
    supplying them would amount to implementing the per-user isolation this
    experiment deliberately leaves out, which would quietly turn the isolation
    probes into passes.

    Read the recall numbers with the same caveat the file backend carries:
    search returns messages **verbatim**, so document IDs always survive and
    `gold_recall` is flattered relative to Hindsight, which paraphrases. The
    comparison that survives that asymmetry is `contamination` and `ordering`.

    Fairness is enforced on context size, not hit count
    ---------------------------------------------------
    Honcho returns whole documents where Hindsight returns short extracted
    observations, so matching on number of results would hand Honcho several
    times the context. Hits are taken in rank order until the character budget
    is spent, which holds "what the model sees" — the comparable unit this
    harness is built on — equal between the two.
    """

    name = "honcho"
    SHARED_PEER = "meridian-platform"

    def __init__(self, api_url: str, workspace: str, limit: int = 100,
                 char_budget: int = HONCHO_CHAR_BUDGET):
        self.api_url = api_url.rstrip("/")
        self.workspace, self.limit, self.char_budget = workspace, limit, char_budget

    def _call(self, path: str, body: dict, timeout: int = 180):
        url = f"{self.api_url}/v3/workspaces/{self.workspace}{path}"
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        try:
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read() or "{}")
        except urllib.error.HTTPError as e:
            # Honcho reports every embedding failure as a token-limit error
            # (src/utils/search.py:383-394 catches a bare ValueError), so the
            # body is not to be trusted on a 4xx — say so rather than repeat it.
            detail = e.read()[:300]
            hint = ("  (a 'token limit' message here is unreliable — check the "
                    "honcho-api pod log for the real cause)" if e.code == 422 else "")
            raise RuntimeError(f"HTTP {e.code} on POST {path}: {detail!r}{hint}") from e

    def context_for(self, query: str, user: str) -> tuple[str, list[dict]]:
        resp = self._call("/search", {"query": query, "limit": self.limit})
        hits = resp if isinstance(resp, list) else (resp.get("items") or [])

        results, chars = [], 0
        for h in hits:
            text = h.get("content") or ""
            if chars + len(text) > self.char_budget:
                break                      # stop at the budget; do not reorder
            chars += len(text) + 1
            peer = h.get("peer_id") or ""
            # Honcho has no tags. The peer a message belongs to *is* its scope,
            # so it is translated into the tag vocabulary the isolation check
            # already speaks — a cross-user hit therefore trips the same wire.
            tag = SHARED_TAG if peer == self.SHARED_PEER else f"{USER_TAG_PREFIX}{peer}"
            results.append({"text": text, "tags": [tag], "peer_id": peer})

        return "\n".join(r["text"] for r in results), results


class FileBackend:
    """The whole `system_prompt_block()`, which is what the file provider shows.

    Identical output to the in-pod provider: `system_prompt_block()` is pure
    string concatenation over a file read — no model, no socket, no config
    beyond `$HERMES_HOME`. See measure_file_based.py for the full argument.
    """

    name = "file-based"

    def __init__(self, home: Path, user: str):
        self.mod = self._load()
        self.provider = self.mod.MultiUserFileMemoryProvider()
        self.provider.initialize("eval-session", hermes_home=str(home),
                                 user_id=user, chat_type="dm")
        self._block = self.provider.system_prompt_block()

    @staticmethod
    def _load():
        if not REF_FILE_PROVIDER.is_file():
            sys.exit(f"file provider reference not found at {REF_FILE_PROVIDER}")
        mp = types.ModuleType("agent.memory_provider"); mp.MemoryProvider = object
        agent_pkg = types.ModuleType("agent"); agent_pkg.memory_provider = mp
        reg = types.ModuleType("tools.registry")
        reg.tool_error = lambda m: json.dumps({"success": False, "error": m})
        tools_pkg = types.ModuleType("tools"); tools_pkg.registry = reg
        utils = types.ModuleType("utils"); utils.atomic_replace = lambda s, d: Path(s).replace(d)
        for n, m in [("agent", agent_pkg), ("agent.memory_provider", mp),
                     ("tools", tools_pkg), ("tools.registry", reg), ("utils", utils)]:
            sys.modules[n] = m
        import importlib.util
        spec = importlib.util.spec_from_file_location("multiuser_memory_ref", REF_FILE_PROVIDER)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def context_for(self, query: str, user: str) -> tuple[str, list[dict]]:
        # The block does not depend on the query. That is the finding, not a
        # shortcut: this provider has no retrieval step to vary.
        return self._block, []


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

STOPWORDS = frozenset("""
a an and are as at be by for from has have in is it its of on or that the this to
with was were will not no any all each per its it's than then when which who whom
""".split())


def content_words(text: str) -> set:
    """Lowercased content tokens, for measuring overlap between two texts."""
    toks = re.findall(r"[a-z0-9][a-z0-9\-_./]{2,}", text.lower())
    return {t for t in toks if t not in STOPWORDS}


def diagnose_miss(doc_id: str, doc_body: str, results: list[dict], context: str) -> dict:
    """Decide what a gold-recall miss actually cost.

    `gold_recall` matches on document ID. Hindsight paraphrases, so a miss has
    two very different possible causes and they must not be reported as one
    number:

    "id_stripped"  — the document's substance is in the context under a unit
        that dropped the identifier. The model can still answer; only the
        citation is gone. Nothing the user asked about is lost.
    "absent"       — no recalled unit carries the document's content. This is
        real loss: the fact did not survive into the context and the model
        cannot answer from it.

    Decided by content-word overlap against the best-matching recalled unit,
    measured as the share of the gold document's own content words that unit
    reproduces. Recall of the document by the unit, not similarity between them
    — a long unit covering the document should score high even if it also says
    much else, and a short unit quoting one clause should not.
    """
    gold_words = content_words(doc_body)
    if not gold_words:
        return {"doc": doc_id, "verdict": "unknown", "coverage": None, "best_unit": None}

    best, best_cov = None, 0.0
    for r in results:
        cov = len(gold_words & content_words(r.get("text") or "")) / len(gold_words)
        if cov > best_cov:
            best, best_cov = r, cov

    # 0.30 is a deliberately low bar, because the question it answers is "is any
    # of this document here at all", not "is it here in full". Set higher and a
    # heavily-compressed but perfectly usable observation reads as loss; the
    # partial band below keeps that case visible rather than silently binned.
    if best_cov >= 0.30:
        verdict = "id_stripped"
    elif best_cov >= 0.15:
        verdict = "partial"
    else:
        verdict = "absent"
    return {
        "doc": doc_id,
        "verdict": verdict,
        "coverage": round(best_cov, 3),
        "best_unit": ((best.get("text") or "")[:240] if best else None),
    }


def score_probe(probe: dict, context: str, results: list[dict],
                probe_user: str, doc_text: dict[str, str]) -> dict:
    lower = context.lower()

    gold = probe.get("gold_docs") or []
    # A gold document counts as present if its ID survives into the context, or
    # (for providers that ship raw text) its body does. ID match is what makes
    # this mechanical across a paraphrasing store.
    found = []
    for doc_id in gold:
        body = doc_text.get(doc_id, "")
        hit = doc_id.lower() in lower or (body and body[:120].lower() in lower)
        if hit:
            found.append(doc_id)

    must = probe.get("must_contain") or []
    must_hits = [s for s in must if s.lower() in lower]
    mustnt = probe.get("must_not_contain") or []
    mustnt_hits = [s for s in mustnt if s.lower() in lower]

    # Probes marked scored_at="answer" are recorded but not rated here. Their
    # question is whether the model invented something, which the retrieved
    # text cannot answer either way; rating them at this layer would put a
    # number on the frequency of common English in the corpus.
    at_context = probe.get("scored_at", "context") == "context"

    # Rank of the first gold unit, where the backend has an ordering. Position
    # matters: a gold fact ranked 40th in a 40-unit budget is one unlucky
    # tie-break from being absent.
    rank = None
    for i, r in enumerate(results, 1):
        if any(g.lower() in (r.get("text") or "").lower() for g in gold):
            rank = i
            break

    # Ordering: does the current answer reach the model before the stale one?
    # Character offsets rather than result indices, so a ranked list and a flat
    # file are measured by the same rule.
    def first_offset(needles):
        offs = [lower.find(s.lower()) for s in needles]
        offs = [o for o in offs if o >= 0]
        return min(offs) if offs else None

    cur_at = first_offset(must) if at_context else None
    stale_at = first_offset(mustnt) if at_context else None
    current_first = None
    if cur_at is not None and stale_at is not None:
        current_first = cur_at < stale_at

    # Hard isolation check on tags, not prose.
    leaks = []
    for r in results:
        for t in r.get("tags") or []:
            if t.startswith(USER_TAG_PREFIX) and t != f"{USER_TAG_PREFIX}{probe_user}":
                leaks.append({"tag": t, "text": (r.get("text") or "")[:120]})

    chars = len(context)
    return {
        "id": probe["id"],
        "class": probe["class"],
        "scored_at": probe.get("scored_at", "context"),
        "as_user": probe_user,
        "units_returned": len(results),
        "context_chars": chars,
        "est_tokens": int(chars * TOK_PER_CHAR),
        "gold_docs": gold,
        "gold_found": found,
        "gold_recall": (len(found) / len(gold)) if gold else None,
        "gold_miss_diagnosis": [
            diagnose_miss(d, doc_text.get(d, ""), results, context)
            for d in gold if d not in found
        ],
        "first_gold_rank": rank,
        "must_contain": must,
        "must_contain_hits": must_hits,
        "must_contain_rate": (len(must_hits) / len(must)) if (must and at_context) else None,
        "must_not_contain": mustnt,
        "contamination_hits": mustnt_hits,
        "contamination_rate": (len(mustnt_hits) / len(mustnt)) if (mustnt and at_context) else None,
        "current_offset": cur_at,
        "superseded_offset": stale_at,
        "current_ranked_first": current_first,
        "tag_leaks": leaks,
    }


def summarize(rows: list[dict]) -> dict:
    by_class = defaultdict(list)
    for r in rows:
        by_class[r["class"]].append(r)

    def mean(vals):
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 3) if vals else None

    def ordering(rs):
        """Share of contested probes where the current answer precedes the stale one.

        Only probes where both a current and a superseded value are actually
        present are counted — elsewhere there is nothing to order, and folding
        those in as wins would inflate the number.
        """
        judged = [r["current_ranked_first"] for r in rs if r.get("current_ranked_first") is not None]
        return (round(sum(judged) / len(judged), 3), len(judged)) if judged else (None, 0)

    classes = {}
    for cls, rs in sorted(by_class.items()):
        order_rate, order_n = ordering(rs)
        classes[cls] = {
            "probes": len(rs),
            "gold_recall": mean(r["gold_recall"] for r in rs),
            "must_contain_rate": mean(r["must_contain_rate"] for r in rs),
            "contamination_rate": mean(r["contamination_rate"] for r in rs),
            "current_ranked_first": order_rate,
            "contested_probes": order_n,
            "avg_units": mean(float(r["units_returned"]) for r in rs),
            "avg_context_tokens": int(mean(float(r["est_tokens"]) for r in rs) or 0),
        }
    all_order, all_order_n = ordering(rows)
    # What the recall misses actually cost, rolled up across every probe.
    verdicts = defaultdict(int)
    for r in rows:
        for d in r.get("gold_miss_diagnosis") or []:
            verdicts[d["verdict"]] += 1
    return {
        "overall": {
            "gold_miss_verdicts": dict(verdicts),
            "probes": len(rows),
            "gold_recall": mean(r["gold_recall"] for r in rows),
            "must_contain_rate": mean(r["must_contain_rate"] for r in rows),
            "contamination_rate": mean(r["contamination_rate"] for r in rows),
            "current_ranked_first": all_order,
            "contested_probes": all_order_n,
            "avg_context_tokens": int(mean(float(r["est_tokens"]) for r in rows) or 0),
            "total_tag_leaks": sum(len(r["tag_leaks"]) for r in rows),
            "deferred_to_answer_layer": sorted(
                r["id"] for r in rows if r.get("scored_at") == "answer"),
        },
        "by_class": classes,
    }


def load_doc_text(corpus: Path) -> dict[str, str]:
    texts, current, body = {}, None, []
    for md in sorted(corpus.glob("*.md")):
        for line in md.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("<!--") and s.endswith("-->"):
                k, _, v = s[4:-3].strip().partition(":")
                if k.strip() == "id":
                    if current and body:
                        texts[current] = "\n".join(body).strip()
                    current, body = v.strip(), []
                continue
            if line.startswith("# "):
                continue
            body.append(line)
        if current and body:
            texts[current] = "\n".join(body).strip()
            current, body = None, []
    return texts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider", choices=["hindsight", "file", "honcho"], required=True)
    ap.add_argument("--rung", required=True, help="ladder rung, for the output filename")
    ap.add_argument("--queries", default="/tmp/scaletest/v2/queries.json")
    ap.add_argument("--corpus", default="/tmp/scaletest/v2/corpus")
    ap.add_argument("--user", default="user07", help="identity for probes with no as_user")
    ap.add_argument("--api-url", default="http://127.0.0.1:18888")
    ap.add_argument("--bank", default="kube-agents-memory")
    ap.add_argument("--budget", default=DEFAULT_BUDGET)
    ap.add_argument("--home", default="", help="file provider HERMES_HOME (file backend)")
    ap.add_argument("--workspace", default=HONCHO_WORKSPACE, help="honcho workspace")
    ap.add_argument("--honcho-limit", type=int, default=100,
                    help="hits requested per search; Honcho caps this at 100")
    ap.add_argument("--honcho-char-budget", type=int, default=HONCHO_CHAR_BUDGET)
    ap.add_argument("--allow-leaks", action="store_true",
                    help="record cross-user leaks but still exit 0. For backends "
                         "where per-user isolation is knowingly not implemented "
                         "(Honcho here), so the ladder completes instead of "
                         "aborting on a failure that is the expected result.")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    probes = json.loads(Path(a.queries).read_text())["queries"]
    doc_text = load_doc_text(Path(a.corpus))

    if a.provider == "hindsight":
        backend = HindsightBackend(a.api_url, a.bank, a.budget)
        label = f"hindsight (budget={a.budget}, tags_match={TAGS_MATCH}, types={RECALL_TYPES})"
    elif a.provider == "honcho":
        backend = HonchoBackend(a.api_url, a.workspace, a.honcho_limit, a.honcho_char_budget)
        label = (f"honcho (workspace={a.workspace}, surface=message-search, "
                 f"limit={a.honcho_limit}, char_budget={a.honcho_char_budget:,})")
    else:
        home = Path(a.home or f"/tmp/scaletest/v2/filestore/rung-{a.rung}")
        if not (home / "memories" / "MEMORY.md").is_file():
            sys.exit(f"no file store at {home}; run measure_file_based.py first")
        backend = FileBackend(home, a.user)
        label = f"multiuser_memory (real source, HERMES_HOME={home})"

    print(f"provider  {label}")
    print(f"rung      {a.rung}")
    print(f"probes    {len(probes)}\n")

    rows = []
    for p in probes:
        user = p.get("as_user") or a.user
        # An isolation probe run as the same identity the store was built for
        # measures nothing; the file backend is rebuilt for the probe identity.
        be = backend
        if a.provider == "file" and user != a.user:
            be = FileBackend(Path(a.home or f"/tmp/scaletest/v2/filestore/rung-{a.rung}"), user)
        try:
            context, results = be.context_for(p["query"], user)
        except Exception as e:
            print(f"  {p['id']:<22} ERROR {e}")
            rows.append({"id": p["id"], "class": p["class"], "error": str(e),
                         "as_user": user, "units_returned": 0, "context_chars": 0,
                         "est_tokens": 0, "gold_docs": p.get("gold_docs") or [],
                         "gold_found": [], "gold_recall": 0.0, "first_gold_rank": None,
                         "gold_miss_diagnosis": [],
                         "must_contain": p.get("must_contain") or [], "must_contain_hits": [],
                         "must_contain_rate": 0.0 if p.get("must_contain") else None,
                         "must_not_contain": p.get("must_not_contain") or [],
                         "contamination_hits": [], "contamination_rate": None,
                         "tag_leaks": []})
            continue
        row = score_probe(p, context, results, user, doc_text)
        rows.append(row)
        gr = "  -  " if row["gold_recall"] is None else f"{row['gold_recall']:.2f}"
        cr = "  -  " if row["contamination_rate"] is None else f"{row['contamination_rate']:.2f}"
        mc = "  -  " if row["must_contain_rate"] is None else f"{row['must_contain_rate']:.2f}"
        flag = "  LEAK" if row["tag_leaks"] else ""
        print(f"  {row['id']:<22} {row['class']:<13} gold {gr}  must {mc}  contam {cr}  "
              f"{row['units_returned']:>3}u {row['est_tokens']:>7,}tok{flag}")

    summary = summarize(rows)
    o = summary["overall"]
    print(f"\noverall   gold_recall {o['gold_recall']}  must_contain {o['must_contain_rate']}  "
          f"contamination {o['contamination_rate']}  "
          f"current-first {o['current_ranked_first']} (n={o['contested_probes']})  "
          f"avg {o['avg_context_tokens']:,} tok/turn")
    print("\nby class:")
    for cls, s in summary["by_class"].items():
        cf = "  -  " if s["current_ranked_first"] is None else f"{s['current_ranked_first']:.2f}"
        print(f"  {cls:<14} n={s['probes']:<2} gold {str(s['gold_recall']):<6} "
              f"must {str(s['must_contain_rate']):<6} contam {str(s['contamination_rate']):<6} "
              f"first {cf}  {s['avg_context_tokens']:>7,} tok")

    out = Path(a.out or f"/tmp/scaletest/v2/{backend.name}-results-r{a.rung}.json")
    out.write_text(json.dumps({
        "provider": backend.name, "label": label, "rung": a.rung,
        "default_user": a.user, "tok_per_char": TOK_PER_CHAR,
        # So a reader of the JSON alone can tell a clean isolation result from
        # one that was never going to pass.
        "isolation_enforced": not a.allow_leaks,
        "summary": summary, "probes": rows,
    }, indent=2))
    print(f"\nwrote {out}")

    if o["total_tag_leaks"]:
        if a.allow_leaks:
            # Not softened into a pass: the count is printed, stored, and named
            # as unimplemented isolation rather than as a clean run.
            print(f"\nEXPECTED FAILURE: {o['total_tag_leaks']} cross-user tag leak(s). "
                  f"--allow-leaks is set, so this exits 0; per-user isolation is not "
                  f"implemented for this backend and these probes are expected to fail.",
                  file=sys.stderr)
            return 0
        print(f"\nFAIL: {o['total_tag_leaks']} cross-user tag leak(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
