#!/usr/bin/env python3
"""Measure what each access design puts in front of the model, per probe, per rung.

Runs inside the sandbox pod. Speaks to the credential broker on loopback and,
in directory mode, reads the leased checkout the broker hands back.

What is being compared is not "an API" against "a filesystem". It is the same
thing the memory experiment compared: **the bytes a model would have to take
into its context to answer the probe**. For arm A that is the output of a
recursive grep over a working tree plus the files it names; for arm B it is the
result set of a tracked-file search plus the files it names. Both are produced
by the same canonical access pattern, stated per probe in `probes.json`, so the
numbers are a property of the design rather than of a prompt.

Metrics, per probe:

  reach          share of the probe's gold artefacts whose content landed
  context_bytes  total bytes the pattern put in front of the caller
  contamination  must_not_contain strings present in those bytes
  ordering       first must_contain offset < first must_not_contain offset
  ops            layer calls issued
  expressible    whether the arm can answer at all

`ops` is reported but is not comparable across arms without reading the
caveat in the README: a directory-mode read is a local file open and a content
read is an HTTP round trip, so the counts measure different things. Bytes are
the comparable quantity.

Usage:
    ladder.py --arm content --rung 200 --repo dshnayder-org/infra
    ladder.py --arm directory --rung 200 --repo dshnayder-org/infra
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROXY = os.environ.get("CREDENTIAL_PROXY_URL", "http://127.0.0.1:8765")
CHARS_PER_TOKEN = 4  # the same crude divisor the memory harness used


class Layer:
    """Base for the two access designs. Tracks calls and bytes returned."""

    name = "?"

    def __init__(self) -> None:
        self.ops = 0
        self.bytes = 0
        self.trace: list[dict] = []

    def record(self, verb: str, size: int, status: str = "ok") -> None:
        self.ops += 1
        self.bytes += size
        self.trace.append({"verb": verb, "bytes": size, "status": status})


def post(route: str, payload: dict) -> tuple[int, dict]:
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        f"{PROXY}{route}", data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"error": raw[:200].decode("utf-8", "replace")}


def refresh_token(repo: str) -> None:
    post("/v1/github/refresh", {"repository": repo})


# ---------------------------------------------------------------------------
# Arm B - content passing
# ---------------------------------------------------------------------------


class ContentLayer(Layer):
    name = "content"

    def __init__(self, repo: str, base: str) -> None:
        super().__init__()
        refresh_token(repo)
        status, answer = post(
            "/v1/workspace/open", {"repo": repo, "base": base, "depth": 1}
        )
        if status != 200:
            raise SystemExit(f"open failed {status}: {answer}")
        self.handle = answer["handle"]
        self.record("open", len(json.dumps(answer)))

    def search(self, pattern: str) -> str:
        status, answer = post(
            "/v1/workspace/grep", {"handle": self.handle, "pattern": pattern}
        )
        text = json.dumps(answer, ensure_ascii=False)
        self.record("grep", len(text.encode()), "ok" if status == 200 else str(status))
        return text

    def read(self, paths: list[str]) -> tuple[str, set[str]]:
        if not paths:
            return "", set()
        status, answer = post(
            "/v1/workspace/read", {"handle": self.handle, "paths": paths}
        )
        if status != 200:
            self.record("read", len(json.dumps(answer).encode()), str(status))
            return json.dumps(answer), set()
        out, got = [], set()
        for entry in answer.get("files", answer.get("results", [])):
            raw = entry.get("contentBase64") or ""
            try:
                out.append(base64.b64decode(raw).decode("utf-8", "replace"))
            except Exception:
                out.append(f"<{len(raw)} base64 chars>")
            got.add(entry.get("path", ""))
        text = "\n".join(out)
        self.record("read", len(text.encode()))
        return text, got

    def history(self, path: str) -> tuple[str, str]:
        return "", "not_expressible"

    def mode(self, path: str) -> tuple[str, str]:
        return "", "not_expressible"

    def close(self) -> None:
        post("/v1/workspace/close", {"handle": self.handle})
        self.record("close", 0)


# ---------------------------------------------------------------------------
# Arm A - shared working tree
# ---------------------------------------------------------------------------


class DirectoryLayer(Layer):
    name = "directory"

    def __init__(self, repo: str, base: str, root: Path) -> None:
        super().__init__()
        refresh_token(repo)
        self.tree = root / f"corpus-{base.replace('/', '-')}"
        if not (self.tree / ".git").exists():
            self.tree.parent.mkdir(parents=True, exist_ok=True)
            result = self.git(
                "clone", "--depth", "1", "--branch", base,
                f"https://github.com/{repo}.git", str(self.tree),
                cwd=self.tree.parent,
            )
            if not (self.tree / ".git").exists():
                raise SystemExit(f"clone failed: {result[:400]}")
        else:
            self.record("clone", 0)

    def git(self, *args: str, cwd: Path | None = None) -> str:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd or self.tree),
            capture_output=True,
            text=True,
        )
        out = proc.stdout + proc.stderr
        self.record(f"git {args[0]}", len(out.encode()))
        return out

    def search(self, pattern: str) -> str:
        # What a model reaches for on a working tree: recursive grep over
        # everything, because there is no tracked-file distinction on offer.
        proc = subprocess.run(
            ["grep", "-rn", "--binary-files=without-match", pattern, "."],
            cwd=str(self.tree),
            capture_output=True,
            text=True,
        )
        out = proc.stdout
        self.record("grep -rn", len(out.encode()))
        return out

    def read(self, paths: list[str]) -> tuple[str, set[str]]:
        out, got = [], set()
        for path in paths:
            target = self.tree / path
            try:
                out.append(target.read_text(encoding="utf-8", errors="replace"))
                got.add(path)
            except (OSError, UnicodeError):
                try:
                    out.append(f"<{target.stat().st_size} binary bytes>")
                    got.add(path)
                except OSError:
                    out.append("")
            self.record("cat", len(out[-1].encode()))
        return "\n".join(out), got

    def history(self, path: str) -> tuple[str, str]:
        out = self.git("log", "--format=%s%n%n%b", "--", path)
        return out, "ok"

    def mode(self, path: str) -> tuple[str, str]:
        out = self.git("ls-files", "-s", "--", path)
        return out, "ok"

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def groups(probe: dict) -> list[list[str]]:
    """`must_contain` entries, normalised to alternative groups.

    Shared shape with `agent_probe.py`: a group is satisfied by any one of its
    alternatives, so the same answer key scores both halves of the experiment.
    """
    out = []
    for entry in probe.get("must_contain", []):
        out.append([entry] if isinstance(entry, str) else list(entry))
    return out


def score(probe: dict, context: str, layer: Layer, notes: list[str],
          reached: set[str]) -> dict:
    lowered = context.lower()

    # A gold artefact counts as reached when its body came back, not when a
    # grep hit named the path. The layer reports what it actually returned, so
    # this is exact rather than a string match against the context.
    gold = probe.get("gold", [])
    reach = len(reached & set(gold)) / len(gold) if gold else 1.0

    wanted = groups(probe)
    must = [g for g in wanted if any(s.lower() in lowered for s in g)]
    must_not = [s for s in probe.get("must_not_contain", []) if s.lower() in lowered]

    first_good = min(
        (lowered.index(s.lower()) for g in wanted for s in g
         if s.lower() in lowered),
        default=-1,
    )
    first_bad = min(
        (lowered.index(s.lower()) for s in probe.get("must_not_contain", [])
         if s.lower() in lowered),
        default=-1,
    )
    if first_good < 0:
        ordering = None
    elif first_bad < 0:
        ordering = True
    else:
        ordering = first_good < first_bad

    return {
        "probe": probe["id"],
        "class": probe["class"],
        "arm": layer.name,
        "reach": round(reach, 3),
        "must_contain_hit": len(must),
        "must_contain_total": len(wanted),
        "contamination": len(must_not),
        "contamination_total": len(probe.get("must_not_contain", [])),
        "ordering_correct": ordering,
        "context_bytes": layer.bytes,
        "context_tokens": layer.bytes // CHARS_PER_TOKEN,
        "ops": layer.ops,
        "notes": notes,
        "trace": layer.trace,
    }


def run_probe(probe: dict, layer: Layer) -> dict:
    # Counters reset per probe: the one-time setup (a clone in arm A, an open
    # in arm B) is excluded from both, because a real session pays it once and
    # including it would make the first probe of a run look like the design.
    Layer.__init__(layer)
    notes: list[str] = []
    parts: list[str] = []
    reached: set[str] = set()

    parts.append(layer.search(probe["search"]))

    gold = probe.get("gold", [])
    if gold:
        text, got = layer.read(gold)
        parts.append(text)
        reached |= got

    needs = probe.get("needs", [])
    if "history" in needs:
        for path in gold:
            text, status = layer.history(path)
            if status == "not_expressible":
                notes.append("history: not_expressible")
            else:
                parts.append(text)
    if "mode" in needs:
        for path in gold:
            text, status = layer.mode(path)
            if status == "not_expressible":
                notes.append("mode: not_expressible")
            else:
                parts.append(text)

    context = "\n".join(parts)
    return score(probe, context, layer, notes, reached)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", required=True, choices=("content", "directory"))
    parser.add_argument("--rung", required=True, type=int)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--probes", type=Path, default=Path("probes.json"))
    parser.add_argument("--root", type=Path, default=Path("/opt/data/scratch/git-access-ab"))
    parser.add_argument("--only", default="")
    arguments = parser.parse_args()

    base = f"git-access-ab/r{arguments.rung}"
    probes = json.loads(arguments.probes.read_text())["probes"]
    wanted = set(arguments.only.split(",")) if arguments.only else None
    probes = [
        p for p in probes
        if p["class"] not in ("write", "adversarial")
        and (wanted is None or p["id"] in wanted)
    ]

    if arguments.arm == "content":
        layer: Layer = ContentLayer(arguments.repo, base)
    else:
        arguments.root.mkdir(parents=True, exist_ok=True)
        layer = DirectoryLayer(arguments.repo, base, arguments.root)

    rows = []
    for probe in probes:
        started = time.time()
        try:
            row = run_probe(probe, layer)
        except Exception as exc:  # a probe that cannot run is data, not a crash
            row = {
                "probe": probe["id"],
                "class": probe["class"],
                "arm": arguments.arm,
                "error": f"{type(exc).__name__}: {exc}",
            }
        row["seconds"] = round(time.time() - started, 2)
        row["rung"] = arguments.rung
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), file=sys.stderr)

    layer.close()
    print(json.dumps({"arm": arguments.arm, "rung": arguments.rung, "rows": rows},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
