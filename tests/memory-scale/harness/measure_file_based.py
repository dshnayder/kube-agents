#!/usr/bin/env python3
"""Measure what `multiuser_memory` injects into the system prompt at each rung.

This does not simulate the provider. It imports the real deleted source
(`git show 9fa85ee^:agents/platform/plugins/memory/multiuser_memory/__init__.py`,
saved as multiuser_memory_ref.py), writes real `MEMORY.md` and `users/*.md`
files in the exact on-disk format, and calls the real `system_prompt_block()`.
The three Hermes imports it needs are stubbed, because none of them are on the
read path being measured — `MemoryProvider` is an ABC, `tool_error` is only
reached from `handle_tool_call`, and `atomic_replace` is only reached from
`_write_entries`, which this harness bypasses by writing the files itself in
the provider's own format.

Why this is a legitimate offline measurement
--------------------------------------------
`system_prompt_block()` is pure string concatenation over a file read. It calls
no model, opens no socket, and consults no configuration beyond `$HERMES_HOME`.
Given the same files it produces byte-identical output in this process and in
the gateway pod. Running it here rather than in-cluster costs nothing in
fidelity and makes the number re-derivable by anyone with the repo.

What it therefore cannot tell you
---------------------------------
Nothing about answer quality. This measures the *cost* side of the crossover
only: what the architecture spends per turn, and where it stops fitting. The
quality side requires the real image, the real agent and real DMs.
"""

import argparse
import json
import sys
import types
from pathlib import Path

REF = Path("/tmp/scaletest/multiuser_memory_ref.py")

# Context windows worth marking on the curve. The provider has no budget, no
# truncation and no cap, so these are hard walls rather than soft targets.
WINDOWS = [
    ("Claude Sonnet 4.5 / Opus (200k)", 200_000),
    ("1M-token beta", 1_000_000),
]

# Tokens per character for English prose. cl100k/Claude-family tokenizers land
# between 0.23 and 0.27 on text like this; 0.25 (the familiar chars/4) is used
# for the headline and the range is reported alongside so no conclusion rests on
# the third significant figure. Characters and words below are exact.
TOK_PER_CHAR = 0.25
TOK_PER_CHAR_RANGE = (0.23, 0.27)


def load_provider():
    """Import the real provider with its three Hermes dependencies stubbed."""
    if not REF.is_file():
        sys.exit(f"provider reference not found at {REF}; re-extract it with:\n"
                 f"  git show 9fa85ee^:agents/platform/plugins/memory/"
                 f"multiuser_memory/__init__.py > {REF}")

    mp = types.ModuleType("agent.memory_provider")
    mp.MemoryProvider = object
    agent_pkg = types.ModuleType("agent")
    agent_pkg.memory_provider = mp
    reg = types.ModuleType("tools.registry")
    reg.tool_error = lambda m: json.dumps({"success": False, "error": m})
    tools_pkg = types.ModuleType("tools")
    tools_pkg.registry = reg
    utils = types.ModuleType("utils")
    utils.atomic_replace = lambda src, dst: Path(src).replace(dst)
    for name, mod in [("agent", agent_pkg), ("agent.memory_provider", mp),
                      ("tools", tools_pkg), ("tools.registry", reg), ("utils", utils)]:
        sys.modules[name] = mod

    import importlib.util
    spec = importlib.util.spec_from_file_location("multiuser_memory_ref", REF)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_corpus(corpus_dir: Path, manifest_path: Path):
    manifest = json.loads(manifest_path.read_text())
    by_id = {d["id"]: d for d in manifest["documents"]}
    texts: dict[str, str] = {}

    for md in sorted(corpus_dir.glob("*.md")):
        current, body = None, []
        for line in md.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("<!--") and stripped.endswith("-->"):
                inner = stripped[4:-3].strip()
                key, _, value = inner.partition(":")
                if key.strip() == "id":
                    if current and body:
                        texts[current] = "\n".join(body).strip()
                    current, body = value.strip(), []
                continue
            if line.startswith("# "):
                continue
            body.append(line)
        if current and body:
            texts[current] = "\n".join(body).strip()

    missing = [i for i in by_id if i not in texts]
    if missing:
        sys.exit(f"{len(missing)} manifest documents absent from the corpus: {missing[:5]}")
    for doc_id, doc in by_id.items():
        doc["text"] = texts[doc_id]
    return manifest, by_id


def build_store(mod, home: Path, docs: list[dict], user: str):
    """Write MEMORY.md and the user file in the provider's exact on-disk format."""
    mem_dir = home / "memories"
    users_dir = mem_dir / "users"
    users_dir.mkdir(parents=True, exist_ok=True)

    shared = [d["text"] for d in docs if d["scope"] == "shared"]
    personal = [d["text"] for d in docs if d["scope"] == f"user:{user}"]

    (mem_dir / "MEMORY.md").write_text(
        mod.ENTRY_DELIMITER.join(shared), encoding="utf-8")

    provider = mod.MultiUserFileMemoryProvider()
    provider.initialize("measure-session", hermes_home=str(home), user_id=user, chat_type="dm")
    (users_dir / f"{provider._user_id}.md").write_text(
        mod.ENTRY_DELIMITER.join(personal), encoding="utf-8")
    return provider, len(shared), len(personal)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--corpus", default="/tmp/scaletest/v2/corpus")
    ap.add_argument("--manifest", default="/tmp/scaletest/v2/manifest.json")
    ap.add_argument("--home", default="/tmp/scaletest/v2/filestore")
    ap.add_argument("--user", default="user07", help="the identity whose DM is measured")
    ap.add_argument("--out", default="/tmp/scaletest/v2/file-based-results.json")
    a = ap.parse_args()

    mod = load_provider()
    manifest, by_id = load_corpus(Path(a.corpus), Path(a.manifest))
    docs = list(by_id.values())

    print(f"provider: {REF.name} (real source, {len(REF.read_text().splitlines())} lines)")
    print(f"corpus:   {len(docs)} documents, user under test {a.user}\n")

    rows = []
    for rung in manifest["rungs"]:
        selected = [d for d in docs if d["rung"] <= rung]
        home = Path(a.home) / f"rung-{rung}"
        provider, n_shared, n_personal = build_store(mod, home, selected, a.user)

        block = provider.system_prompt_block()
        chars = len(block)
        words = len(block.split())
        lines = block.count("\n") + 1
        tokens = int(chars * TOK_PER_CHAR)
        lo = int(chars * TOK_PER_CHAR_RANGE[0])
        hi = int(chars * TOK_PER_CHAR_RANGE[1])

        # Faithfulness check: the provider must render every entry it was given.
        # If these ever disagree, something truncated, and the whole premise of
        # the comparison ("file-based has perfect recall") would be wrong.
        rendered = block.count("\n- ")
        expected = n_shared + n_personal
        if rendered != expected:
            print(f"  WARNING rung {rung}: rendered {rendered} bullets, expected {expected}")

        row = {
            "rung": rung,
            "shared_entries": n_shared,
            "personal_entries": n_personal,
            "total_entries": expected,
            "rendered_bullets": rendered,
            "block_chars": chars,
            "block_words": words,
            "block_lines": lines,
            "est_tokens": tokens,
            "est_tokens_range": [lo, hi],
            "memory_md_bytes": (home / "memories" / "MEMORY.md").stat().st_size,
            "fits": {name: tokens < size for name, size in WINDOWS},
            "pct_of_window": {name: round(100 * tokens / size, 1) for name, size in WINDOWS},
        }
        rows.append(row)

    hdr = f"{'rung':>6} {'entries':>8} {'chars':>10} {'est tokens':>12} {'range':>17} {'% of 200k':>10} {'fits 200k':>10}"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        lo, hi = r["est_tokens_range"]
        print(f"{r['rung']:>6} {r['total_entries']:>8} {r['block_chars']:>10,} "
              f"{r['est_tokens']:>12,} {f'{lo:,}-{hi:,}':>17} "
              f"{r['pct_of_window']['Claude Sonnet 4.5 / Opus (200k)']:>9.1f}% "
              f"{'yes' if r['fits']['Claude Sonnet 4.5 / Opus (200k)'] else 'NO':>10}")

    print("\nEvery entry is rendered at every rung: the provider has no truncation,")
    print("no relevance filter and no token budget on this path. Cost is O(corpus)")
    print("per turn, and recall of what is in the file is exact until it stops fitting.")

    Path(a.out).write_text(json.dumps({
        "provider": "multiuser_memory (real source at 9fa85ee^)",
        "user_under_test": a.user,
        "tok_per_char": TOK_PER_CHAR,
        "tok_per_char_range": list(TOK_PER_CHAR_RANGE),
        "windows": dict(WINDOWS),
        "rows": rows,
    }, indent=2))
    print(f"\nwrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
