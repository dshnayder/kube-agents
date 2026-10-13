#!/usr/bin/env python3
"""Decision memory: durable infrastructure decisions and lessons, learned from outcomes.

Prototype for docs/designs/decision-memory.md. Harness-neutral by construction:
it imports nothing from Hermes or the A2A stack, talks to Hindsight over its
REST API only, and reads and writes git. Every subcommand prints JSON on stdout
so any harness (a Hermes skill, a mode-next worker, a Kubernetes CronJob, a CI
step) can drive it.

System of record: OKF records in the GitOps repository,
``knowledge/decisions/*.md`` and ``knowledge/lessons/*.md`` (markdown + YAML
frontmatter, ``docs/architecture/06-api-and-data-contracts.md`` §5). The
Hindsight bank is a derived index of them and can be rebuilt from git at any
time.

Subcommands:
  validate  check every record's frontmatter
  index     upsert records into the Hindsight bank (one document per record)
  check     match a change (diff or file list) against recorded decisions and lessons
  why       explain the recorded rationale (or the history) behind a path
  from-outcome  draft a lesson record from a merged fix PR / resolved finding
  dream     nightly pass: verify, detect reverts/supersession/recurrence, reconcile index
"""

from __future__ import annotations

import argparse
import datetime as _dt
import fnmatch
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml

# --------------------------------------------------------------------------
# Constants. The bank is separate from the conversational bank on purpose: it is
# a derived index (wipe-and-rebuild from git is a valid recovery), it must not be
# paraphrased by consolidation (identifiers are the point), and it carries a
# different config (no observations).
# --------------------------------------------------------------------------

DEFAULT_BANK_ID = "kube-agents-decisions"
SHARED_TAG = "scope:shared"
RECORD_DIRS = ("knowledge/decisions", "knowledge/lessons")
RECORD_TYPES = ("decision", "lesson")
STATUSES = ("proposed", "active", "superseded", "retired")
SEVERITIES = ("block", "warn", "info")
RECORD_STRATEGY = "record"
DOC_PREFIX = "okf:"

BANK_CONFIG = {
    # Records are retained verbatim: `chunks` runs no LLM, so an identifier, a
    # threshold or a PR link can never be paraphrased away.
    "retain_strategies": {RECORD_STRATEGY: {"retain_extraction_mode": "chunks"}},
    "retain_default_strategy": RECORD_STRATEGY,
    # No observation layer: synthesis is the dream's job, done explicitly and
    # with provenance, not by background consolidation.
    "enable_observations": False,
    "reflect_mission": (
        "The decision register of a Kubernetes platform team. Each memory is a "
        "recorded infrastructure decision or a lesson learned from a fixed "
        "incident, with its identifier, status and pull request. Answer by citing "
        "record identifiers and pull requests; prefer active records over "
        "superseded ones and say so when only a superseded record matches."
    ),
}


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------

@dataclass
class Record:
    path: str               # repo-relative path of the markdown file
    meta: Dict[str, Any]
    body: str

    @property
    def id(self) -> str:
        return str(self.meta.get("id", ""))

    @property
    def type(self) -> str:
        return str(self.meta.get("type", ""))

    @property
    def status(self) -> str:
        return str(self.meta.get("status", "active"))

    @property
    def title(self) -> str:
        return str(self.meta.get("title", ""))

    @property
    def scope(self) -> Dict[str, List[str]]:
        s = self.meta.get("scope") or {}
        return {k: [str(x) for x in (s.get(k) or [])] for k in ("paths", "resources", "clusters", "checks")}

    @property
    def guard(self) -> Dict[str, Any]:
        return self.meta.get("guard") or {}

    @property
    def evidence(self) -> Dict[str, Any]:
        return self.meta.get("evidence") or {}

    def document_id(self, repo_id: str) -> str:
        return f"{DOC_PREFIX}{repo_id}:{self.path}"

    def tags(self) -> List[str]:
        tags = [SHARED_TAG, f"kind:{self.type}", f"status:{self.status}", f"id:{self.id}"]
        s = self.scope
        tags += [f"cluster:{c}" for c in s["clusters"]]
        tags += [f"check:{c}" for c in s["checks"]]
        tags += [f"res:{r}" for r in s["resources"]]
        if self.guard.get("severity"):
            tags.append(f"guard:{self.guard['severity']}")
        return sorted(set(tags))

    def render_for_index(self) -> str:
        """The text the index stores: self-describing, identifiers in the prose."""
        ev = self.evidence
        lines = [
            f"{self.id} ({self.type}, {self.status}, {self.meta.get('date', '')}): {self.title}",
        ]
        s = self.scope
        if s["paths"] or s["resources"]:
            lines.append("Governs: " + ", ".join(s["resources"] + s["paths"]))
        if s["clusters"]:
            lines.append("Clusters: " + ", ".join(s["clusters"]))
        if self.guard.get("rule"):
            lines.append(f"Guard ({self.guard.get('severity', 'warn')}): {self.guard['rule']}")
        if ev.get("pull_request"):
            lines.append(f"Pull request: {ev['pull_request']}")
        if self.meta.get("supersedes"):
            lines.append("Supersedes: " + ", ".join(self.meta["supersedes"]))
        if self.meta.get("superseded_by"):
            lines.append(f"Superseded by: {self.meta['superseded_by']}")
        lines.append("")
        lines.append(self.body.strip())
        return "\n".join(lines)

    def summary(self) -> Dict[str, Any]:
        return {
            "id": self.id, "type": self.type, "status": self.status, "title": self.title,
            "path": self.path, "guard": self.guard or None,
            "pull_request": self.evidence.get("pull_request"),
            "superseded_by": self.meta.get("superseded_by"),
        }


FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)


def parse_record(text: str, path: str) -> Record:
    m = FRONTMATTER.match(text)
    if not m:
        raise ValueError(f"{path}: no YAML frontmatter")
    meta = yaml.safe_load(m.group(1)) or {}
    if not isinstance(meta, dict):
        raise ValueError(f"{path}: frontmatter is not a mapping")
    return Record(path=path, meta=meta, body=m.group(2))


def render_record(meta: Dict[str, Any], body: str) -> str:
    return "---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True) + "---\n\n" + body.strip() + "\n"


def validate_record(rec: Record) -> List[str]:
    errs = []
    if rec.type not in RECORD_TYPES:
        errs.append(f"type must be one of {RECORD_TYPES}")
    if not re.fullmatch(r"(DEC|LES)-\d{4}-\d{3,}", rec.id):
        errs.append("id must look like DEC-2026-001 or LES-2026-001")
    if rec.status not in STATUSES:
        errs.append(f"status must be one of {STATUSES}")
    if not rec.title:
        errs.append("title is required")
    s = rec.scope
    if not (s["paths"] or s["resources"] or s["checks"]):
        errs.append("scope needs at least one of paths, resources, checks — an unscoped record can never be matched")
    g = rec.guard
    if g:
        if g.get("severity") and g["severity"] not in SEVERITIES:
            errs.append(f"guard.severity must be one of {SEVERITIES}")
        exp = g.get("expect")
        if exp and not all(k in exp for k in ("file", "path", "op", "value")):
            errs.append("guard.expect needs file, path, op, value")
    if rec.type == "lesson" and not rec.evidence.get("pull_request"):
        errs.append("a lesson needs evidence.pull_request — it is learned from a merged fix")
    if rec.status == "superseded" and not rec.meta.get("superseded_by"):
        errs.append("a superseded record must name superseded_by")
    return errs


def load_records(repo: Path) -> List[Record]:
    out = []
    for d in RECORD_DIRS:
        base = repo / d
        if not base.is_dir():
            continue
        for p in sorted(base.glob("*.md")):
            if p.name.lower() in ("index.md", "readme.md"):
                continue
            out.append(parse_record(p.read_text(), str(p.relative_to(repo))))
    return out


# --------------------------------------------------------------------------
# Guards: a machine-checkable assertion on the GitOps tree.
#   expect: {file: <glob>, path: spec.template.spec.containers[name=app].resources.requests.memory,
#            op: ">=", value: "256Mi"}
# --------------------------------------------------------------------------

_QTY = re.compile(r"^([0-9.]+)\s*([A-Za-z]*)$")
_UNITS = {"": 1, "m": 1e-3, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12,
          "Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4}


def quantity(v: Any) -> Optional[float]:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    m = _QTY.match(str(v).strip())
    if not m or m.group(2) not in _UNITS:
        return None
    return float(m.group(1)) * _UNITS[m.group(2)]


def _path_tokens(path: str) -> List[Tuple[str, Optional[Tuple[str, str]]]]:
    """Split on unescaped dots; `\\.` is a literal dot (ResourceQuota keys such as
    `requests.memory` contain one)."""
    toks = []
    for part in re.split(r"(?<!\\)\.", path):
        part = part.replace("\\.", ".")
        m = re.fullmatch(r"([^\[]+)(?:\[([^=\]]+)=([^\]]+)\])?", part)
        if not m:
            raise ValueError(f"bad guard path segment {part!r}")
        toks.append((m.group(1), (m.group(2), m.group(3)) if m.group(2) else None))
    return toks


def yaml_get(doc: Any, path: str) -> Any:
    cur = doc
    for key, sel in _path_tokens(path):
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
        if sel:
            k, v = sel
            if not isinstance(cur, list):
                return None
            cur = next((x for x in cur if isinstance(x, dict) and str(x.get(k)) == v), None)
            if cur is None:
                return None
    return cur


def compare(actual: Any, op: str, expected: Any) -> bool:
    if op in ("==", "!="):
        eq = str(actual) == str(expected)
        return eq if op == "==" else not eq
    a, e = quantity(actual), quantity(expected)
    if a is None or e is None:
        return False
    return {">=": a >= e, ">": a > e, "<=": a <= e, "<": a < e}[op]


def evaluate_guard(expect: Dict[str, Any], files: Dict[str, str]) -> List[Dict[str, Any]]:
    """Evaluate a guard over {path: content}. One result per matching document."""
    results = []
    for fpath, content in sorted(files.items()):
        if not fnmatch.fnmatch(fpath, expect["file"]):
            continue
        try:
            docs = [d for d in yaml.safe_load_all(content) if d is not None]
        except yaml.YAMLError:
            continue
        for doc in docs:
            actual = yaml_get(doc, expect["path"])
            if actual is None:
                continue
            results.append({"file": fpath, "actual": actual,
                            "ok": compare(actual, expect["op"], expect["value"])})
    return results


# --------------------------------------------------------------------------
# Matching a change against records (deterministic; Hindsight optional)
# --------------------------------------------------------------------------

def _resources_in(content: str) -> List[str]:
    out = []
    try:
        for d in yaml.safe_load_all(content):
            if isinstance(d, dict) and d.get("kind") and isinstance(d.get("metadata"), dict):
                md = d["metadata"]
                out.append(f"{d['kind']}/{md.get('namespace', '')}/{md.get('name', '')}")
    except yaml.YAMLError:
        pass
    return out


def match_change(records: Iterable[Record], changed: Dict[str, Optional[str]]) -> List[Dict[str, Any]]:
    """changed: {path: new content, or None if deleted}. Returns findings."""
    findings = []
    new_resources = {p: _resources_in(c) for p, c in changed.items() if c}
    for rec in records:
        if rec.status not in ("active", "proposed"):
            continue
        s = rec.scope
        hits = set()
        for p in changed:
            if any(fnmatch.fnmatch(p, g) for g in s["paths"]):
                hits.add(p)
            for r in new_resources.get(p, []):
                if any(fnmatch.fnmatch(r, g) for g in s["resources"]):
                    hits.add(p)
        if not hits:
            continue
        verdict = "related"
        detail = []
        exp = rec.guard.get("expect")
        if exp:
            files = {p: c for p, c in changed.items() if c and p in hits}
            detail = evaluate_guard(exp, files)
            deleted = [p for p in hits if changed.get(p) is None and fnmatch.fnmatch(p, exp["file"])]
            if any(not d["ok"] for d in detail) or deleted:
                verdict = "violates"
                detail += [{"file": p, "actual": None, "ok": False, "deleted": True} for p in deleted]
            elif detail:
                verdict = "consistent"
        severity = rec.guard.get("severity", "warn") if verdict == "violates" else "info"
        findings.append({**rec.summary(), "verdict": verdict, "severity": severity,
                         "files": sorted(hits), "guard_results": detail})
    order = {"block": 0, "warn": 1, "info": 2}
    findings.sort(key=lambda f: (order.get(f["severity"], 3), f["id"]))
    return findings


def render_check_markdown(findings: List[Dict[str, Any]], semantic: List[Dict[str, Any]]) -> str:
    if not findings and not semantic:
        return "No recorded decision or lesson governs the files this change touches."
    lines = ["### Prior decisions and lessons this change touches", ""]
    icon = {"violates": "⛔", "consistent": "✅", "related": "ℹ️"}
    for f in findings:
        pr = f" — learned in {f['pull_request']}" if f.get("pull_request") else ""
        lines.append(f"- {icon[f['verdict']]} **{f['id']}** ({f['type']}): {f['title']}{pr}")
        if f.get("guard") and f["guard"].get("rule"):
            lines.append(f"  - Guard ({f['guard'].get('severity', 'warn')}): {f['guard']['rule']}")
        for g in f["guard_results"]:
            if not g["ok"]:
                what = "deleted" if g.get("deleted") else f"is now `{g['actual']}`"
                lines.append(f"  - `{g['file']}` {what}")
    if semantic:
        lines += ["", "Possibly related (semantic match, not path-scoped — verify before relying on it):"]
        for s in semantic:
            lines.append(f"- {s.get('id') or '?'}: {s['text'][:160]}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Git helpers
# --------------------------------------------------------------------------

def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


def changed_files(repo: Path, base: str, head: str = "HEAD") -> Dict[str, Optional[str]]:
    out: Dict[str, Optional[str]] = {}
    for line in git(repo, "diff", "--name-status", f"{base}...{head}").splitlines():
        parts = line.split("\t")
        status, path = parts[0], parts[-1]
        if status.startswith("D"):
            out[path] = None
        else:
            out[path] = git(repo, "show", f"{head}:{path}")
    return out


def worktree_files(repo: Path, globs: Iterable[str]) -> Dict[str, str]:
    files = {}
    for p in git(repo, "ls-files").splitlines():
        if any(fnmatch.fnmatch(p, g) for g in globs):
            files[p] = (repo / p).read_text()
    return files


PR_REF = re.compile(r"\(#(\d+)\)|Merge pull request #(\d+)")


def history(repo: Path, path: str, limit: int = 10) -> List[Dict[str, Any]]:
    fmt = "%H%x1f%an%x1f%aI%x1f%s%x1f%b%x1e"
    raw = git(repo, "log", f"-n{limit}", f"--format={fmt}", "--", path)
    out = []
    for entry in raw.split("\x1e"):
        entry = entry.strip("\n")
        if not entry:
            continue
        sha, author, date, subject, body = (entry.split("\x1f") + [""] * 5)[:5]
        m = PR_REF.search(subject + "\n" + body)
        out.append({"commit": sha[:12], "author": author, "date": date, "subject": subject,
                    "pull_request": (m.group(1) or m.group(2)) if m else None,
                    "body": body.strip()[:600]})
    return out


# --------------------------------------------------------------------------
# Hindsight REST client (stdlib only)
# --------------------------------------------------------------------------

class Hindsight:
    def __init__(self, url: str, bank: str = DEFAULT_BANK_ID, timeout: float = 60.0):
        self.base = url.rstrip("/") + "/v1/default/banks/" + urllib.parse.quote(bank, safe="")
        self.bank = bank
        self.timeout = timeout

    def _req(self, method: str, path: str, body: Any = None, query: Optional[Dict[str, Any]] = None) -> Any:
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query, doseq=True)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None

    def ensure_bank(self) -> None:
        self._req("PUT", "", {"mission": BANK_CONFIG["reflect_mission"]})
        self._req("PATCH", "/config", {"updates": BANK_CONFIG})

    def retain(self, rec: Record, repo_id: str) -> None:
        item = {
            "content": rec.render_for_index(),
            "document_id": rec.document_id(repo_id),
            "update_mode": "replace",
            "context": f"{rec.id}: {rec.title}"[:200],
            "tags": rec.tags(),
            "metadata": {"id": rec.id, "type": rec.type, "status": rec.status, "path": rec.path,
                         "pull_request": str(rec.evidence.get("pull_request") or "")},
            "strategy": RECORD_STRATEGY,
        }
        if rec.meta.get("date"):
            item["timestamp"] = f"{rec.meta['date']}T00:00:00Z"
        self._req("POST", "/memories", {"items": [item], "async": False})

    def documents(self) -> List[Dict[str, Any]]:
        out, offset = [], 0
        while True:
            page = self._req("GET", "/documents", query={"limit": 100, "offset": offset,
                                                         "tags": SHARED_TAG, "tags_match": "any_strict"})
            items = page.get("items", []) if isinstance(page, dict) else page
            out += items
            if len(items) < 100:
                return out
            offset += 100

    def delete_document(self, doc_id: str) -> None:
        self._req("DELETE", "/documents/" + urllib.parse.quote(doc_id, safe=""))

    def recall(self, query: str, tag_groups: Optional[List[Dict[str, Any]]] = None,
               budget: str = "mid", max_tokens: int = 2048) -> List[Dict[str, Any]]:
        body: Dict[str, Any] = {"query": query, "budget": budget, "max_tokens": max_tokens,
                                "types": ["world", "experience"]}
        if tag_groups:
            body["tag_groups"] = tag_groups
        else:
            body.update({"tags": [SHARED_TAG], "tags_match": "any_strict"})
        resp = self._req("POST", "/memories/recall", body)
        return list((resp or {}).get("results") or [])


def _id_from_tags(tags: Iterable[str]) -> Optional[str]:
    for t in tags or []:
        if t.startswith("id:"):
            return t[3:]
    return None


def semantic_related(hs: Hindsight, query: str, exclude: Iterable[str]) -> List[Dict[str, Any]]:
    """Active records related by meaning, for paths no record scopes yet."""
    groups = [{"tags": [SHARED_TAG], "match": "all_strict"},
              {"tags": ["status:active"], "match": "all_strict"}]
    seen, out = set(exclude), []
    for r in hs.recall(query, tag_groups=groups, budget="low"):
        rid = _id_from_tags(r.get("tags") or [])
        if rid in seen:
            continue
        seen.add(rid)
        out.append({"id": rid, "text": r.get("text", ""), "document_id": r.get("document_id")})
    return out


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def repo_identity(repo: Path) -> str:
    try:
        url = git(repo, "remote", "get-url", "origin").strip()
        m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?$", url)
        if m:
            return m.group(1)
    except subprocess.CalledProcessError:
        pass
    return repo.resolve().name


def cmd_validate(a) -> Dict[str, Any]:
    repo = Path(a.repo)
    problems, ids = {}, {}
    records = load_records(repo)
    for rec in records:
        errs = validate_record(rec)
        if rec.id in ids:
            errs.append(f"duplicate id, also in {ids[rec.id]}")
        ids[rec.id] = rec.path
        if errs:
            problems[rec.path] = errs
    known = set(ids)
    for rec in records:
        for ref in list(rec.meta.get("supersedes") or []) + ([rec.meta["superseded_by"]] if rec.meta.get("superseded_by") else []):
            if ref not in known:
                problems.setdefault(rec.path, []).append(f"references unknown record {ref}")
    return {"records": len(records), "ok": not problems, "problems": problems}


def cmd_index(a) -> Dict[str, Any]:
    repo = Path(a.repo)
    repo_id = a.repo_id or repo_identity(repo)
    hs = Hindsight(a.hindsight_url, a.bank)
    hs.ensure_bank()
    records = [r for r in load_records(repo) if not validate_record(r)]
    wanted = {r.document_id(repo_id): r for r in records}
    existing = {d["id"]: d for d in hs.documents() if str(d.get("id", "")).startswith(f"{DOC_PREFIX}{repo_id}:")}
    upserted, unchanged, deleted = [], [], []
    for doc_id, rec in wanted.items():
        cur = existing.get(doc_id)
        if cur and sorted(cur.get("tags") or []) == rec.tags() and not a.force:
            # Same tags; let Hindsight's content hash decide whether text changed.
            meta = cur.get("document_metadata") or {}
            if meta.get("status") == rec.status and (cur.get("text_length") or 0) == len(rec.render_for_index()):
                unchanged.append(rec.id)
                continue
        hs.retain(rec, repo_id)
        upserted.append(rec.id)
    for doc_id in existing:
        if doc_id not in wanted:
            hs.delete_document(doc_id)
            deleted.append(doc_id)
    return {"bank": a.bank, "repo": repo_id, "upserted": upserted, "unchanged": unchanged, "deleted": deleted}


def _load_changed(a, repo: Path) -> Dict[str, Optional[str]]:
    if a.base:
        return changed_files(repo, a.base, a.head)
    changed: Dict[str, Optional[str]] = {}
    for p in a.files or []:
        fp = repo / p
        changed[p] = fp.read_text() if fp.exists() else None
    return changed


def cmd_check(a) -> Dict[str, Any]:
    repo = Path(a.knowledge_repo or a.repo)
    records = [r for r in load_records(repo) if not validate_record(r)]
    changed = _load_changed(a, Path(a.repo))
    findings = match_change(records, changed)
    semantic: List[Dict[str, Any]] = []
    if a.hindsight_url and a.semantic:
        query = a.semantic if isinstance(a.semantic, str) and a.semantic != "auto" else \
            "Changes to: " + ", ".join(sorted(changed))
        try:
            semantic = semantic_related(Hindsight(a.hindsight_url, a.bank), query,
                                        exclude=[f["id"] for f in findings])[: a.semantic_limit]
        except (urllib.error.URLError, OSError) as e:
            semantic = [{"id": None, "text": f"semantic search unreachable: {e}", "document_id": None}]
    verdict = "block" if any(f["severity"] == "block" for f in findings) else \
        "warn" if any(f["severity"] == "warn" for f in findings) else "ok"
    return {"verdict": verdict, "changed": sorted(changed), "findings": findings, "semantic": semantic,
            "markdown": render_check_markdown(findings, semantic)}


def cmd_why(a) -> Dict[str, Any]:
    repo = Path(a.repo)
    target = a.path
    records = [r for r in load_records(repo) if not validate_record(r)]
    content = (repo / target).read_text() if (repo / target).exists() else None
    resources = _resources_in(content) if content else []
    governing = []
    for rec in records:
        s = rec.scope
        if any(fnmatch.fnmatch(target, g) for g in s["paths"]) or \
                any(fnmatch.fnmatch(r, g) for r in resources for g in s["resources"]):
            governing.append(rec.summary() | {"date": str(rec.meta.get("date", ""))})
    governing.sort(key=lambda r: (r["status"] != "active", r["date"]), reverse=False)
    hist = history(repo, target, a.limit)
    uncovered = [h for h in hist if h["pull_request"] and not any(
        str(g.get("pull_request") or "").rstrip("/").endswith("/" + h["pull_request"]) for g in governing)]
    return {"path": target, "resources": resources, "records": governing, "history": hist,
            "history_without_record": uncovered,
            "note": ("Recorded rationale exists; cite the record ids." if governing else
                     "No recorded decision governs this path. The history is evidence of what changed, "
                     "not of why — say so, and offer to draft a decision record from it.")}


def _next_id(repo: Path, kind: str, year: int) -> str:
    prefix = "LES" if kind == "lesson" else "DEC"
    n = 0
    for rec in load_records(repo):
        m = re.fullmatch(rf"{prefix}-{year}-(\d+)", rec.id)
        if m:
            n = max(n, int(m.group(1)))
    return f"{prefix}-{year}-{n + 1:03d}"


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


def cmd_from_outcome(a) -> Dict[str, Any]:
    """Draft a lesson from a merged fix and its verified resolution.

    Input JSON (from the outcome recorder; harness-neutral):
      {pull_request, title, merged_at, merged_by, commit, files[], cluster, check, finding_id,
       root_cause, fix, prevention, guard{rule, severity, expect{...}}, resolution{verified_by, at}}
    """
    o = json.loads(Path(a.outcome).read_text())
    repo = Path(a.repo)
    merged = str(o.get("merged_at") or _dt.date.today().isoformat())[:10]
    rid = _next_id(repo, "lesson", int(merged[:4]))
    scope = {"paths": sorted(set(o.get("files") or []))}
    if o.get("resources"):
        scope["resources"] = o["resources"]
    if o.get("cluster"):
        scope["clusters"] = [o["cluster"]]
    if o.get("check"):
        scope["checks"] = [o["check"]]
    meta: Dict[str, Any] = {
        "type": "lesson", "id": rid, "title": o["title"], "status": "active", "date": merged,
        "scope": scope,
    }
    if o.get("guard"):
        meta["guard"] = o["guard"]
    meta["evidence"] = {k: v for k, v in {
        "pull_request": o.get("pull_request"), "commit": o.get("commit"),
        "merged_by": o.get("merged_by"), "finding_id": o.get("finding_id"),
        "resolution": o.get("resolution"), "incident": o.get("incident"),
    }.items() if v}
    body = "\n".join([
        "## What happened", "", str(o.get("root_cause", "")).strip(), "",
        "## What fixed it", "", str(o.get("fix", "")).strip(), "",
        "## How we know it worked", "",
        str((o.get("resolution") or {}).get("summary", "The finding did not recur after the merge.")).strip(), "",
        "## Prevent it next time", "", str(o.get("prevention", "")).strip(),
    ])
    rel = f"knowledge/lessons/{rid}-{slug(o['title'])}.md"
    text = render_record(meta, body)
    errs = validate_record(parse_record(text, rel))
    if a.write and not errs:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    return {"id": rid, "path": rel, "valid": not errs, "problems": errs, "written": bool(a.write and not errs),
            "content": text}


def _reverted(repo: Path, commit: str) -> Optional[Dict[str, str]]:
    if not commit:
        return None
    try:
        out = git(repo, "log", "--format=%H%x1f%s", f"--grep=This reverts commit {commit}", "-n1")
    except subprocess.CalledProcessError:
        return None
    if out.strip():
        sha, subject = out.strip().split("\x1f", 1)
        return {"commit": sha[:12], "subject": subject}
    return None


def cmd_dream(a) -> Dict[str, Any]:
    """The nightly pass. Deterministic checks first; nothing here asks a model.

    1. Verify every active record against the repository as it stands:
       - its scoped paths still exist (else: obsolete candidate)
       - its guard still holds on the tree (else: at-risk — the lesson is being unlearned)
       - its fix commit was not reverted
    2. Supersession hygiene: a record superseded by another must say so on both sides.
    3. Recurrence: lessons sharing a check or a guarded field across clusters
       are promotion candidates (a policy, not another lesson).
    4. Coverage: recent changes to governed infrastructure paths with no record (harvest queue).
    5. Reconcile the Hindsight index with git (optional, with --hindsight-url).
    Output is a report plus proposed record edits; edits land through a PR, never directly.
    """
    repo = Path(a.repo)
    records = load_records(repo)
    by_id = {r.id: r for r in records}
    all_files = git(repo, "ls-files").splitlines()
    report: Dict[str, Any] = {"date": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
                              "verified": [], "at_risk": [], "obsolete": [], "reverted": [],
                              "supersession": [], "recurring": [], "uncovered_changes": [],
                              "proposed_edits": []}
    for rec in records:
        if rec.status != "active" or validate_record(rec):
            continue
        s = rec.scope
        if s["paths"] and not any(fnmatch.fnmatch(f, g) for f in all_files for g in s["paths"]):
            report["obsolete"].append({"id": rec.id, "reason": "no file matches its scope any more"})
            report["proposed_edits"].append({"id": rec.id, "path": rec.path, "set": {"status": "retired"},
                                             "why": "governed paths are gone"})
            continue
        exp = rec.guard.get("expect")
        if exp:
            results = evaluate_guard(exp, worktree_files(repo, [exp["file"]]))
            bad = [r for r in results if not r["ok"]]
            if bad:
                report["at_risk"].append({"id": rec.id, "title": rec.title, "violations": bad,
                                          "guard": rec.guard.get("rule"),
                                          "introduced_by": [history(repo, b["file"], 1)[0] for b in bad
                                                            if history(repo, b["file"], 1)]})
                continue
        rv = _reverted(repo, str(rec.evidence.get("commit") or ""))
        if rv:
            report["reverted"].append({"id": rec.id, "revert": rv})
            continue
        report["verified"].append(rec.id)
    for rec in records:
        for old in rec.meta.get("supersedes") or []:
            o = by_id.get(old)
            if o and (o.status != "superseded" or o.meta.get("superseded_by") != rec.id):
                report["supersession"].append({"id": old, "superseded_by": rec.id, "current_status": o.status})
                report["proposed_edits"].append({"id": old, "path": o.path,
                                                 "set": {"status": "superseded", "superseded_by": rec.id},
                                                 "why": f"{rec.id} declares it supersedes {old}"})
    groups: Dict[str, List[Record]] = {}
    for rec in records:
        if rec.type != "lesson":
            continue
        keys = [f"check:{c}" for c in rec.scope["checks"]]
        exp = rec.guard.get("expect")
        if exp:
            keys.append(f"field:{exp['path']}")
        for k in keys:
            groups.setdefault(k, []).append(rec)
    for k, recs in sorted(groups.items()):
        clusters = sorted({c for r in recs for c in r.scope["clusters"]})
        if len(recs) >= a.recurrence_threshold:
            report["recurring"].append({"key": k, "lessons": [r.id for r in recs], "clusters": clusters,
                                        "proposal": "promote to a fleet-wide decision with a policy check "
                                                    "(policy/), instead of one lesson per cluster"})
    governed = [g for r in records for g in r.scope["paths"]]
    since = (_dt.date.today() - _dt.timedelta(days=a.since_days)).isoformat()
    known_prs = {str(r.evidence.get("pull_request") or "").rstrip("/").rsplit("/", 1)[-1] for r in records}
    for line in git(repo, "log", f"--since={since}", "--name-only", "--format=@@%H%x1f%s").split("@@"):
        if not line.strip():
            continue
        head, *files = line.strip().split("\n")
        sha, subject = head.split("\x1f", 1)
        if any(f.startswith("knowledge/") for f in files):
            continue  # the change carried its own record
        infra = [f for f in files if f and any(fnmatch.fnmatch(f, g) for g in a.infra_glob)
                 and not f.startswith("knowledge/")]
        if not infra:
            continue
        m = PR_REF.search(subject)
        pr = (m.group(1) or m.group(2)) if m else None
        if pr in known_prs:
            continue
        if any(fnmatch.fnmatch(f, g) for f in infra for g in governed) or a.include_ungoverned:
            report["uncovered_changes"].append({"commit": sha[:12], "subject": subject, "pull_request": pr,
                                                "files": infra})
    if a.hindsight_url:
        try:
            ns = argparse.Namespace(repo=a.repo, repo_id=a.repo_id, hindsight_url=a.hindsight_url,
                                    bank=a.bank, force=False)
            report["index"] = cmd_index(ns)
        except (urllib.error.URLError, OSError) as e:
            report["index"] = {"error": f"unreachable: {e}"}
    report["silent"] = not any(report[k] for k in ("at_risk", "obsolete", "reverted", "supersession",
                                                   "recurring", "uncovered_changes"))
    return report


def apply_edits(repo: Path, edits: List[Dict[str, Any]]) -> List[str]:
    """Apply a dream's proposed edits to the working tree (to be committed in a PR)."""
    touched = []
    for e in edits:
        p = repo / e["path"]
        rec = parse_record(p.read_text(), e["path"])
        rec.meta.update(e["set"])
        p.write_text(render_record(rec.meta, rec.body))
        touched.append(e["path"])
    return touched


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".", help="GitOps repository checkout")
    ap.add_argument("--hindsight-url", default=os.environ.get("DECISION_MEMORY_HINDSIGHT_URL")
                    or os.environ.get("HINDSIGHT_API_URL"))
    ap.add_argument("--bank", default=os.environ.get("DECISION_MEMORY_BANK", DEFAULT_BANK_ID))
    ap.add_argument("--repo-id", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate")
    p = sub.add_parser("index")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("check")
    p.add_argument("--base")
    p.add_argument("--head", default="HEAD")
    p.add_argument("--files", nargs="*")
    p.add_argument("--knowledge-repo", help="where knowledge/ lives, if not the changed repo")
    p.add_argument("--semantic", nargs="?", const="auto", default=None)
    p.add_argument("--semantic-limit", type=int, default=5)
    p.add_argument("--markdown", action="store_true", help="print only the markdown section")
    p = sub.add_parser("why")
    p.add_argument("path")
    p.add_argument("--limit", type=int, default=10)
    p = sub.add_parser("from-outcome")
    p.add_argument("outcome", help="outcome JSON file")
    p.add_argument("--write", action="store_true")
    p = sub.add_parser("dream")
    p.add_argument("--recurrence-threshold", type=int, default=2)
    p.add_argument("--since-days", type=int, default=7)
    p.add_argument("--infra-glob", nargs="*", default=["clusters/**", "fleet/**", "policy/**"])
    p.add_argument("--include-ungoverned", action="store_true")
    p.add_argument("--apply", action="store_true", help="write proposed edits into the working tree")
    a = ap.parse_args(argv)
    handlers = {"validate": cmd_validate, "index": cmd_index, "check": cmd_check, "why": cmd_why,
                "from-outcome": cmd_from_outcome, "dream": cmd_dream}
    result = handlers[a.cmd](a)
    if a.cmd == "dream" and a.apply:
        result["applied"] = apply_edits(Path(a.repo), result["proposed_edits"])
    if a.cmd == "check" and a.markdown:
        print(result["markdown"])
    else:
        print(json.dumps(result, indent=2, default=str))
    if a.cmd == "check":
        return 2 if result["verdict"] == "block" else 0
    if a.cmd == "validate":
        return 0 if result["ok"] else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
