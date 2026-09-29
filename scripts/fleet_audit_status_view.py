#!/usr/bin/env python3
"""Render the fleet-audit report store as an operator dashboard.

Read side of docs/designs/fleet-audit-report-store.md. The store lives on the
volume of the pod the audit's shell runs in (`/opt/data/fleet-audit/reports/
<audit-id>/<owner>/<name>/`) — the shell sandbox's, or the gateway's on an install without
one — so this tool reads it through one projection: it streams the harness's own
`report_status.py` into the pod (`kubectl exec -i … -- python3 -`) and parses
the single JSON document that comes back. Streaming the script in rather than
calling an installed path keeps the view working against any image, including
one built before the store existed.

The projection is also the offline format: `--json` emits exactly what `--file`
consumes, so the view is reproducible without a cluster.

The ENABLED and SCHEDULE columns come from the checked-in cron roster
(`agents/platform/cron/jobs.json`), not the runtime copy on the pod, and the
header says which file it read; a stream disabled at runtime therefore shows
its seed state. A roster that cannot be read is named in that field and in the
lead rather than swallowed: it disarms NEVER and STALE, so the empty flag list
it produces is "not checked", not "clean", and the exit code is 1.

Four flags the raw rows cannot be trusted without:
  - NO STORE: the store directory is absent, or this stream's files could not
    be read. "I could not look" is not "nothing is wrong", and the exit code
    says so too.
  - DIED: a run took the stream's in-flight lease and never released it, from
    the lease's age against its own two-hour TTL alone. No roster and no
    schedule parsing, so it fires within two hours on every cron shape and on
    kanban-dispatched runs that have no schedule at all.
  - NEVER: roster-enabled, the store was readable, and the stream has neither
    file — it has genuinely never run.
  - STALE: now is past the next expected fire plus slack. A silent stream is
    rendered loudly — this is the whole reason the surface exists.

Only STALE and NEVER consult the roster, so roster drift can no longer suppress
a death. `⚠` on STATUS marks a partial run; the default view counts its coverage
gaps below the table and `--gaps` spells them out, so the fact that a run read
less than the fleet is never off-screen even when the text of it is.
Unknown status values render as themselves with the warning marker,
never as success. Model-influenced text is scrubbed of terminal control
characters at exactly one boundary, `scrub()`.

The presentation half -- the box table, the palette, OSC 8 links, the width
fitting -- is imported from `terminal_table` rather than reimplemented. Two
terminal tables in one repository that disagree about how to measure a coloured
cell is two bugs, and this view is not the place to grow a second copy of that
code.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from terminal_table import (  # noqa: E402
        BOX_ASCII,
        BOX_UNICODE,
        Column,
        Palette,
        ago,
        hyperlink,
        plain,
        pr_ref,
        render_table,
        want_colour,
    )
except ImportError as exc:  # pragma: no cover - a checked-in sibling file
    raise SystemExit(
        "fleet-audit view: scripts/terminal_table.py must sit beside this "
        "script; it owns the table, colour and hyperlink primitives (%s)" % exc
    )

DEFAULT_ROSTER = REPO_ROOT / "agents" / "platform" / "cron" / "jobs.json"
PROJECTION_SCRIPT = (
    REPO_ROOT / "agents" / "platform" / "skills" / "fleet-audit" / "scripts" /
    "report_status.py"
)
DEFAULT_NAMESPACE = "kubeagents-system"
# Every pod the operator runs for an agent carries this label: the gateway, the
# shell sandbox and the credential proxy alike. The store is on the volume of
# whichever pod `audit_report.py` runs in, which is the shell sandbox's `shell`
# container when the sandbox is enabled and the gateway's agent container when
# it is not, so discovery prefers the first and falls back to the second, in
# that order. The gateway also runs `fluent-bit` and the proxy only `envoy`,
# neither of which has a store or a python3.
POD_SELECTOR = "app.kubernetes.io/name=platform-agent"
STORE_CONTAINERS = ("shell", "platform-agent")
# `--pod` without `--container`: the sandbox's, because that is where a default
# install keeps the store.
DEFAULT_CONTAINER = STORE_CONTAINERS[0]
# How long past the expected fire a stream may run before it is called stale:
# the longest observed audit run is ~20 minutes, so an hour of slack flags
# real silence without paging on a slow morning.
STALE_SLACK = timedelta(hours=1)

#: How many kubeconfig contexts the "no agent pod" path will probe looking for
#: the install, and how long it gives each. A laptop that has collected forty
#: contexts should not turn one wrong `--context` into a forty-second wait.
CONTEXT_PROBE_LIMIT = 12
CONTEXT_PROBE_TIMEOUT = 6

#: The two reads that are not probes: finding the pod, and running the
#: projection inside it. Untimed, an API server that accepts the connection and
#: then says nothing hangs the view indefinitely -- and under `--watch` it hangs
#: with the last frame still on screen, which reads as a fleet that has stopped
#: changing rather than as a tool that is stuck. The exec gets the longer budget
#: because it walks the whole store on the far side.
DISCOVER_TIMEOUT = 20
EXEC_TIMEOUT = 120

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\x9b\x90\x9d]")

#: Every outcome `finish` stores. Anything else is styled as a warning rather
#: than as a success -- an outcome this view has never heard of is exactly the
#: one a reader should look at, and green would hide it. HELD is a clean run
#: the ledger stayed open over, which is a reason to look, not a success.
STATUS_STYLE = {
    "CLEAN": "green",
    "HELD": "yellow",
    "OPENED": "cyan",
    "UPDATED": "cyan",
    "running…": "yellow",
    "never ran": "dim",
}
KNOWN_STATUS = frozenset(STATUS_STYLE)

FLAG_STYLE = {"NO STORE": "crit", "DIED": "crit", "NEVER": "yellow", "STALE": "yellow"}

SORTS = ("stream", "last", "findings", "flags")


class ProjectionError(RuntimeError):
    """The projection could not be run at all — exit 2, never a blank table.

    `search_namespace` is set only for "nothing is here" rather than "I could
    not ask", which is the one failure a wrong `kubectl` context produces. It
    tells `main` it is worth looking through the other contexts for the
    install before giving up, because the message that failure deserves is
    "your context points somewhere else, try this one".
    """

    def __init__(self, message: str, search_namespace: str | None = None) -> None:
        super().__init__(message)
        self.search_namespace = search_namespace
        #: Contexts the fallback probe found an agent pod on, once it has run.
        #: None means it has not; a list of two or more is why this error was
        #: raised rather than one of them being picked.
        self.candidates: list[str] | None = None


def scrub(text: object) -> str:
    """The one boundary untrusted text crosses on its way to a terminal."""
    return _CONTROL.sub("�", str(text or ""))


def _oneline(text: object) -> str:
    return " ".join(str(text or "").split())


def _kubectl(args: list[str], context: str | None) -> list[str]:
    return ["kubectl"] + (["--context", context] if context else []) + args


def _pods_query(namespace: str) -> list[str]:
    return [
        "get", "pods", "-n", namespace,
        "-l", POD_SELECTOR,
        "--field-selector", "status.phase=Running",
        "-o",
        "jsonpath={range .items[*]}{.metadata.name}{' '}"
        "{.spec.containers[*].name}{'\\n'}{end}",
    ]


def run_kubectl(
    args: list[str],
    context: str | None,
    timeout: int,
    what: str,
    stdin: str | None = None,
) -> subprocess.CompletedProcess:
    """kubectl, with the two process-level failures turned into ProjectionError.

    A kubectl that is not installed and an API server that never answers both
    reach the operator as a sentence saying which read was being attempted,
    rather than as a traceback or as an indefinite wait. Neither sets
    `search_namespace`: "I could not ask" is not "nothing is here", so the
    fallback that probes a dozen other contexts stays out of it.
    """
    try:
        return subprocess.run(
            _kubectl(args, context),
            capture_output=True,
            text=True,
            timeout=timeout,
            input=stdin,
        )
    except subprocess.TimeoutExpired:
        raise ProjectionError(f"{what} timed out after {timeout}s") from None
    except OSError as exc:
        raise ProjectionError(f"{what} could not run kubectl: {_oneline(exc)}") from exc


def store_targets(listing: str) -> list[tuple[str, str]]:
    """(pod, container) pairs that can hold the store, best first.

    `listing` is `_pods_query`'s output, one `<pod> <container>...` line per
    pod. Every pod with a `shell` container sorts ahead of every pod with only
    the agent container, so an install with the sandbox is read where its
    audits write and one without is still read at all.
    """
    found: list[tuple[int, str, str]] = []
    for line in listing.splitlines():
        name, *containers = line.split()
        for rank, container in enumerate(STORE_CONTAINERS):
            if container in containers:
                found.append((rank, name, container))
                break
    return [(name, container) for _, name, container in sorted(found)]


def discover_pod(namespace: str, context: str | None = None) -> list[tuple[str, str]]:
    """Running (pod, container) pairs that can hold the store, best first.
    Never empty — an empty result is the "no agent pod" failure, which the
    caller must not render as an empty fleet."""
    where = context or "the current context"
    res = run_kubectl(
        _pods_query(namespace),
        context,
        DISCOVER_TIMEOUT,
        f"the agent pod lookup in {namespace} on {where}",
    )
    if res.returncode != 0:
        raise ProjectionError(
            f"no agent pod found in namespace {namespace} on {where}: "
            f"{_oneline(res.stderr) or 'kubectl get pods failed'}",
            search_namespace=namespace,
        )
    targets = store_targets(res.stdout or "")
    if not targets:
        raise ProjectionError(
            f"no agent pod found in namespace {namespace} on {where} "
            f"(label {POD_SELECTOR}, phase Running, container "
            f"{' or '.join(STORE_CONTAINERS)})",
            search_namespace=namespace,
        )
    return targets


def kubeconfig_contexts() -> list[str]:
    try:
        res = subprocess.run(
            ["kubectl", "config", "get-contexts", "-o", "name"],
            capture_output=True,
            text=True,
            timeout=CONTEXT_PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return sorted(res.stdout.split()) if res.returncode == 0 else []


def current_context() -> str:
    try:
        res = subprocess.run(
            ["kubectl", "config", "current-context"],
            capture_output=True,
            text=True,
            timeout=CONTEXT_PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return res.stdout.strip() if res.returncode == 0 else ""


def contexts_with_agent(namespace: str, skip: str) -> list[str]:
    """Which other kubeconfig contexts do hold an agent pod in `namespace`.

    The commonest way this view "breaks" is not a broken install: it is a
    kubeconfig whose current context is one of the clusters the fleet audit
    *manages* rather than the hub it runs on, which every parallel session
    that runs `kubectl config use-context` can cause. Printing the answer
    beats printing the symptom, so the failure path spends a couple of seconds
    finding out. Probed in parallel and time-boxed, because a context pointing
    at a cluster that no longer exists blocks until its own timeout.
    """
    candidates = [c for c in kubeconfig_contexts() if c != skip][:CONTEXT_PROBE_LIMIT]
    if not candidates:
        return []

    def probe(context: str) -> str:
        try:
            res = subprocess.run(
                _kubectl(_pods_query(namespace), context),
                capture_output=True,
                text=True,
                timeout=CONTEXT_PROBE_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return context if res.returncode == 0 and store_targets(res.stdout) else ""

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(candidates)) as pool:
        return [hit for hit in pool.map(probe, candidates) if hit]


def resolve_target(
    namespace: str, context: str | None
) -> tuple[list[tuple[str, str]], str | None]:
    """Running (pod, container) pairs, and the context they were found on.

    A wrong current context is the commonest way this view "does not work",
    and it is a question with an answer rather than one to hand back: if
    exactly one context in the kubeconfig has an agent pod in the namespace,
    read that one and say so. Two or more is genuinely ambiguous -- a fleet
    with a second install in it -- and gets the error and the list.

    An explicit `--context` is never overridden. Someone who named a cluster
    and got nothing wants to know that, not to be quietly redirected.
    """
    try:
        return discover_pod(namespace, context), context
    except ProjectionError as exc:
        if context or not exc.search_namespace:
            raise
        found = contexts_with_agent(namespace, current_context())
        exc.candidates = found
        if len(found) != 1:
            raise
        # Silently, because the header's `context` field already says which
        # cluster was read and a note saying the same thing on stderr is one
        # more line between the operator and the table.
        return discover_pod(namespace, found[0]), found[0]


def context_hint(exc: ProjectionError, context: str | None) -> list[str]:
    """The stderr lines that turn "not found" into "here is where it is"."""
    namespace = exc.search_namespace or DEFAULT_NAMESPACE
    here = context or current_context()
    lines = []
    if here:
        lines.append(f"  the context read was {here}")
    found = exc.candidates
    if found is None:
        found = contexts_with_agent(namespace, here)
    if found:
        # One is reachable here even though `resolve_target` only re-raises on
        # zero or two-plus: an explicit `--context` is never overridden, so that
        # path raises before probing and this function does the probe itself.
        lines.append(
            "  an agent pod is running on %s:"
            % ("another context" if len(found) == 1 else "%d other contexts" % len(found))
        )
        lines += [f"    --context {name}" for name in found]
    elif kubeconfig_contexts():
        lines.append(
            f"  no context in the kubeconfig has one in {namespace}; "
            "--namespace, or the install is down"
        )
    return lines


def fetch_projection(
    pod: str, container: str, namespace: str, context: str | None = None
) -> dict:
    """Run report_status.py inside the pod and parse its one JSON document.

    The script is piped in on stdin rather than invoked by path so the view
    reads a store on an image that ships no copy of it.
    """
    try:
        script = PROJECTION_SCRIPT.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProjectionError(
            f"projection script unreadable at {PROJECTION_SCRIPT}: {_oneline(exc)}"
        ) from exc
    res = run_kubectl(
        ["exec", "-i", pod, "-c", container, "-n", namespace, "--", "python3", "-"],
        context,
        EXEC_TIMEOUT,
        f"the projection in {namespace}/{pod} [{container}]",
        stdin=script,
    )
    if res.returncode != 0:
        raise ProjectionError(
            f"pod {namespace}/{pod} found but exec failed: "
            f"{_oneline(res.stderr) or f'kubectl exec exited {res.returncode}'}"
        )
    return as_projection(res.stdout, f"{namespace}/{pod}")


def as_projection(text: str, origin: str) -> dict:
    """One JSON object out of the projection's stdout, or the exit-2 error.

    Anything the pod printed before the document — a shell warning, a Python
    traceback — lands here rather than becoming a table of empty rows.
    """
    try:
        doc = json.loads(text)
    except ValueError:
        raise ProjectionError(
            f"the projection returned output that is not JSON, from {origin}: "
            f"{_oneline(text)[:200] or '(no output)'}"
        ) from None
    if not isinstance(doc, dict) or not isinstance(doc.get("streams"), dict):
        raise ProjectionError(
            f"the projection returned output that is not JSON, from {origin}: "
            "JSON without a `streams` object"
        )
    return doc


def load_roster(path: Path) -> tuple[dict[str, dict], str]:
    """The roster's fleet-audit jobs, and why the file could not be read.

    The reason is returned rather than swallowed because an unreadable roster
    silently disarms two of the four flags: NEVER and STALE both gate on
    `job.get("enabled")`, which is False for every stream when the roster came
    back empty, so a fleet that has been silent for a week renders as a table of
    calm blank rows under the word "all clear". Same principle the NO STORE flag
    exists for -- "I could not look" is not "nothing is wrong" -- applied to the
    other half of the inputs. An empty roster is not this case: a file that
    parses and holds no fleet-audit job returns no reason.
    """
    try:
        jobs = json.loads(path.read_text(encoding="utf-8")).get("jobs") or []
    except (OSError, ValueError) as exc:
        return {}, _oneline(exc)
    out = {}
    for job in jobs:
        if "fleet-audit" in (job.get("skills") or []):
            out[str(job.get("id", ""))] = {
                "enabled": bool(job.get("enabled")),
                "expr": str(((job.get("schedule") or {}).get("expr")) or ""),
            }
    return out, ""


def next_fire(expr: str, after: datetime) -> datetime | None:
    """Next fire for the roster's cron shapes: `M H * * *` and `M H * * D`.

    The governance roster only uses these two forms. Anything fancier returns
    None and the STALE flag abstains for that stream rather than guessing.

    Out-of-range fields (`99 3 * * *`) abstain the same way. They reach here
    through the same door a typo in the roster does, and `.replace()` raises on
    them, so the check has to be inside the `try` -- a single bad entry would
    otherwise take down every row on its way past `render`.
    """
    parts = expr.split()
    if len(parts) != 5 or parts[2] != "*" or parts[3] != "*":
        return None
    try:
        minute, hour = int(parts[0]), int(parts[1])
        dows = None if parts[4] == "*" else {int(d) % 7 for d in parts[4].split(",")}
        candidate = after.replace(minute=minute, hour=hour, second=0, microsecond=0)
    except ValueError:
        return None
    if candidate <= after:
        candidate += timedelta(days=1)
    for _ in range(8):
        # cron dow: 0=Sunday; Python: Monday=0 → cron = (weekday+1) % 7
        if dows is None or ((candidate.weekday() + 1) % 7) in dows:
            return candidate
        candidate += timedelta(days=1)
    return None


def parse_iso(value: object) -> datetime | None:
    """An aware timestamp, or None. A naive one is not comparable to `now`."""
    try:
        at = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    return at if at.tzinfo else None


def local_time(at: datetime | None, tz: timezone | None = None) -> str:
    """The system's local zone, 12-hour, lowercase am/pm. `tz` exists for
    tests; every real caller leaves it None and gets the machine's zone."""
    if at is None:
        return "—"
    text = at.astimezone(tz).strftime("%b %d %I:%M %p").replace(" 0", " ")
    return text[:-2] + text[-2:].lower()


def when_cell(at: datetime | None, utc: bool) -> str:
    if at is None:
        return "—"
    return at.astimezone(timezone.utc).strftime("%b %d %H:%M") if utc else local_time(at)


def duration(seconds: object) -> str:
    if not isinstance(seconds, (int, float)):
        return "?"
    seconds = int(seconds)
    return f"{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def count_cell(value: object) -> str:
    """A URL list rendered as its length. The envelope stores `prs_opened` as
    URLs; the column has always been a number and a cell has no room for
    three GitHub links."""
    if isinstance(value, list):
        return str(len(value))
    return str(value) if isinstance(value, int) else "—"


def flags_for(stream: dict, job: dict, now: datetime, root_exists: bool) -> list[str]:
    """The four flags, in severity order.

    DIED reads off the projection's liveness alone, so a roster that has
    drifted from runtime cannot suppress the death of a running stream: were
    it gated on the schedule, `at is None` → `expected is None` → `stale is
    False` would render every failure as the same calm blank row.
    """
    flags = []
    unreadable = not root_exists or bool(stream.get("error"))
    if unreadable:
        flags.append("NO STORE")
    liveness = stream.get("liveness") or "never"
    if liveness == "died":
        flags.append("DIED")
    enabled = bool(job.get("enabled"))
    if liveness == "never" and enabled and not unreadable:
        flags.append("NEVER")
    if enabled:
        at = parse_iso((stream.get("latest") or {}).get("finished_at"))
        expected = next_fire(job.get("expr", ""), at) if at else None
        if expected is not None and now > expected + STALE_SLACK:
            flags.append("STALE")
    return flags


def issue_ref(url: object) -> str:
    match = re.search(r"/issues/(\d+)$", str(url or ""))
    return f"#{match.group(1)}" if match else "—"


def exit_code(projection: dict) -> int:
    """1 when the store could not be read: "I could not look" is not a
    healthy fleet, and a caller scripting this view must be able to tell."""
    if not projection.get("root_exists"):
        return 1
    streams = projection.get("streams") or {}
    return 1 if any((s or {}).get("error") for s in streams.values()) else 0


def unreadable_reason(projection: dict) -> str | None:
    if not projection.get("root_exists"):
        return f"store directory absent on the pod: {projection.get('root')}"
    bad = sorted(
        audit_id
        for audit_id, stream in (projection.get("streams") or {}).items()
        if (stream or {}).get("error")
    )
    return f"unreadable stream files: {', '.join(bad)}" if bad else None


GAP_WIDTH = 400


def clip_gap(text: str, width: int = GAP_WIDTH) -> str:
    """A ceiling on one gap, because a collector writes these at whatever length.

    Generous — `--gaps` is asked for, so a reader who opened it wants the
    sentences — but not unbounded: a collector that dumps a stack trace into the
    field would otherwise scroll the nine gaps around it off the terminal. The
    whole text is still in the envelope, which is what `fleet-audit-reports`
    reads; this is the index.
    """
    line = " ".join(str(text).split())
    return line if len(line) <= width else line[: width - 1].rstrip() + "…"


def gap_parts(text: str) -> tuple[str, str]:
    """Split `prod-eu-1: the api server refused the read` into its two halves.

    Collectors write a gap either way round — some name the cluster they could
    not read, some describe a check that ran nowhere — so the scope is taken
    only when the prefix reads like one rather than like a sentence that happens
    to contain a colon.
    """
    line = " ".join(str(text).split())
    scope, sep, rest = line.partition(": ")
    if sep and rest and len(scope) <= 40 and " " not in scope.strip():
        return scope, rest
    return "", line


def short_path(path: str) -> str:
    """Repo-relative when it is in the repo. The roster's default is an
    absolute path eighty characters long on a worktree checkout, which pushes
    the one interesting part of it -- which file was read -- off the line."""
    try:
        return str(Path(path).resolve().relative_to(REPO_ROOT))
    except (OSError, ValueError):
        return path


def status_cell(stream: dict, latest: dict) -> tuple[str, str]:
    """The STATUS text and the style it earns.

    A partial run keeps its own outcome and gains `⚠`; it is not a failure, it
    is a result computed over less than the whole fleet, and rendering it as
    an error would train a reader to ignore the marker that says so.
    """
    text = scrub(latest.get("status") or stream.get("error") or "never ran")
    if stream.get("liveness") == "running":
        age = (stream.get("started") or {}).get("age_s")
        # The lease's age, so a run twenty minutes in reads differently from
        # one about to hit the TTL. Its own style: the STATUS_STYLE key is the
        # bare word, and the age would never match it.
        return ("running… " + duration(age) if age is not None else "running…"), "yellow"
    style = STATUS_STYLE.get(text, "yellow")
    if stream.get("error") and not latest.get("status"):
        style = "red"
    if latest.get("partial"):
        return text + " ⚠", "yellow"
    if text not in KNOWN_STATUS:
        return text + " ?", style  # unknown outcome renders as a warning, never green
    return text, style


def row_for(
    audit_id: str,
    stream: dict,
    job: dict,
    now: datetime,
    root_exists: bool,
    utc: bool,
) -> tuple[list[tuple], list[str], dict]:
    """One table row, its flags, and the `latest` envelope behind it."""
    latest = stream.get("latest") or {}
    flags = flags_for(stream, job, now, root_exists)
    status, status_style = status_cell(stream, latest)

    findings = latest.get("findings")
    crit = latest.get("critical")
    if isinstance(findings, int):
        findings_text = f"{findings} ({crit} c)" if crit else str(findings)
        findings_style = "crit" if crit else ("dim" if not findings else None)
    else:
        findings_text, findings_style = "—", "dim"

    new, resolved = latest.get("new"), latest.get("resolved")
    if isinstance(new, int) and isinstance(resolved, int):
        delta_text = f"+{new} / −{resolved}"
        delta_style = "yellow" if new else ("green" if resolved else "dim")
    else:
        delta_text, delta_style = "—", "dim"

    prs = latest.get("prs_opened")
    pr_text = count_cell(prs)
    # Linked only when there is exactly one, because a count of three cannot
    # honestly point at whichever URL happens to be first. The rest are listed
    # in full under the table.
    pr_url = prs[0] if isinstance(prs, list) and len(prs) == 1 else ""
    at = parse_iso(latest.get("finished_at"))

    enabled = "yes" if job.get("enabled") else ("no" if job else "?")
    row = [
        (scrub(audit_id), "bold" if flags else None),
        (enabled, {"yes": "green", "no": "dim"}.get(enabled, "yellow")),
        (job.get("expr", "?"), "dim"),
        (when_cell(at, utc), None),
        (ago(at, now) if at else "—", "dim"),
        (status, status_style),
        (findings_text, findings_style),
        (delta_text, delta_style),
        # Link targets are store text too, so they cross `scrub()` like
        # every other cell: an escape in a URL is an escape in the terminal.
        (pr_text, "dim" if pr_text in ("0", "—") else None, scrub(pr_url)),
        (
            issue_ref(latest.get("issue_url")),
            "dim" if not latest.get("issue_url") else "cyan",
            scrub(latest.get("issue_url")),
        ),
        (
            " ".join(flags),
            "crit" if {"NO STORE", "DIED"} & set(flags) else ("yellow" if flags else None),
        ),
    ]
    return row, flags, latest


COLUMNS = [
    Column("STREAM"),
    Column("ON", align="c", expendable=4),
    Column("SCHEDULE", expendable=3),
    Column("LAST RUN"),
    Column("AGE", align="r", expendable=6),
    Column("STATUS", wrap=True, min_width=11),
    Column("FINDINGS", align="r"),
    Column("Δ", align="r", expendable=2),
    Column("PRS", align="r", expendable=5),
    Column("ISSUE", align="r"),
    Column("FLAGS"),
]

#: `--gaps`. Wrapped rather than clipped: the reader asked for the text, so the
#: column that holds it is the one that gets the terminal's spare width.
GAP_COLUMNS = [
    Column("STREAM"),
    Column("SCOPE", expendable=1),
    Column("GAP", wrap=True, min_width=28),
]

#: `render_table` is given this when the caller asked for no width limit, which
#: is the default for `render()` used as a library function and what `--width 0`
#: means: draw every column at its natural width and drop nothing.
UNBOUNDED = 10_000


def stream_rows(streams: dict, roster: dict) -> list[tuple[str, str, dict]]:
    """(label, audit id, row source) for every stream and repository.

    The store keeps a stream once per repository it publishes to, so a stream
    an SOP finishes across several managed repositories is several rows, each
    labelled with its repository; one repository, or none yet, is one row
    under the bare id. The row source is the stream's lease and liveness with
    that repository's `latest`, which is the shape `row_for` reads.
    """
    out = []
    for audit_id in sorted(set(roster) | set(streams)):
        stream = streams.get(audit_id) or {}
        repos = stream.get("repos") or {}
        if not repos:
            out.append((audit_id, audit_id, {**stream, "latest": None}))
            continue
        for repo, entry in sorted(repos.items()):
            label = audit_id if len(repos) == 1 else f"{audit_id} {repo}"
            source = {
                **stream,
                "latest": (entry or {}).get("latest"),
                "error": (entry or {}).get("error"),
            }
            out.append((label, audit_id, source))
    return out


def render(
    projection: dict,
    roster: dict,
    now: datetime,
    roster_path: str,
    source: str,
    *,
    palette: Palette | None = None,
    width: int = 0,
    box: dict | None = None,
    utc: bool = False,
    context: str = "",
    sort: str = "stream",
    patterns: tuple[str, ...] = (),
    flagged_only: bool = False,
    show_gaps: bool = False,
    roster_error: str = "",
) -> str:
    palette = palette or Palette(False)
    box = box or BOX_UNICODE
    streams = projection.get("streams") or {}
    root_exists = bool(projection.get("root_exists"))

    built = []
    for label, audit_id, stream in stream_rows(streams, roster):
        row, flags, latest = row_for(
            label, stream, roster.get(audit_id) or {}, now, root_exists, utc
        )
        built.append({"id": label, "row": row, "flags": flags, "latest": latest})

    shown = [
        entry for entry in built
        if (not patterns or any(p.lower() in entry["id"].lower() for p in patterns))
        and (not flagged_only or entry["flags"] or entry["latest"].get("partial"))
    ]

    def key(entry: dict):
        latest = entry["latest"]
        at = parse_iso(latest.get("finished_at"))
        if sort == "last":
            return (0 if at else 1, -(at.timestamp() if at else 0), entry["id"])
        if sort == "findings":
            crit = latest.get("critical") if isinstance(latest.get("critical"), int) else 0
            total = latest.get("findings") if isinstance(latest.get("findings"), int) else -1
            return (-crit, -total, entry["id"])
        if sort == "flags":
            return (0 if entry["flags"] else 1, entry["id"])
        return (entry["id"],)

    shown.sort(key=key)

    out = header_lines(
        projection, built, source, roster_path, context, palette, now, utc, roster_error
    )
    out += ["", palette("STREAMS", "head")]
    out += render_table(
        COLUMNS, [entry["row"] for entry in shown], palette,
        width if width else UNBOUNDED, box,
    )
    if len(shown) != len(built):
        out.append(
            palette(
                "  %d of %d streams shown; drop --stream/--flagged for the rest"
                % (len(shown), len(built)),
                "dim",
            )
        )

    gaps = [
        (entry["id"], gap_parts(scrub(gap)))
        for entry in shown
        for gap in entry["latest"].get("coverage_gaps") or []
    ]
    if gaps:
        streams_with = len({audit_id for audit_id, _ in gaps})
        count = "%d coverage gap%s in %d stream%s" % (
            len(gaps), "" if len(gaps) == 1 else "s",
            streams_with, "" if streams_with == 1 else "s",
        )
        if show_gaps:
            out += ["", palette("COVERAGE GAPS", "head")]
            out.append(
                palette("  " + count + " — what each run did not read, in its own words", "dim")
            )
            out += render_table(
                GAP_COLUMNS,
                [
                    [(audit_id, "dim"), (scope, "yellow"), (clip_gap(text),)]
                    for audit_id, (scope, text) in gaps
                ],
                palette, width if width else UNBOUNDED, box, separator="blank",
            )
        else:
            out.append(palette("  " + count + "; --gaps for the text", "yellow"))

    prs = [
        (entry["id"], url)
        for entry in shown
        for url in entry["latest"].get("prs_opened") or []
    ]
    if prs:
        out += ["", palette("PULL REQUESTS OPENED", "head")]
        width_id = max(len(audit_id) for audit_id, _ in prs)
        out += [
            "  %s  %s"
            % (
                palette(audit_id.ljust(width_id), "dim"),
                hyperlink(palette(pr_ref(scrub(url)), "cyan"), scrub(url), palette),
            )
            for audit_id, url in prs
        ]

    reason = unreadable_reason(projection)
    if reason:
        out += ["", palette("! " + scrub(reason), "crit")]
    out += ["", palette("  --sort last · --flagged · --gaps · --stream <name> · --help for the rest", "dim")]
    return "\n".join(out)


def header_lines(
    projection: dict,
    built: list[dict],
    source: str,
    roster_path: str,
    context: str,
    palette: Palette,
    now: datetime,
    utc: bool,
    roster_error: str = "",
) -> list[str]:
    total = len(built)
    attention = [e for e in built if e["flags"] or e["latest"].get("partial")]
    findings = sum(
        e["latest"].get("findings") or 0
        for e in built
        if isinstance(e["latest"].get("findings"), int)
    )
    critical = sum(
        e["latest"].get("critical") or 0
        for e in built
        if isinstance(e["latest"].get("critical"), int)
    )
    ran = [e for e in built if e["latest"].get("finished_at")]
    newest = max(
        (parse_iso(e["latest"].get("finished_at")) for e in ran),
        default=None,
        key=lambda at: at.timestamp() if at else 0,
    )

    lead = "%s  %s  %s" % (
        palette("fleet-audit", "bold"),
        palette("·", "dim"),
        palette(
            "%d stream%s" % (total, "" if total == 1 else "s"), "bold"
        ),
    )
    # With no roster, NEVER and STALE cannot fire. That makes both halves of
    # the usual verdict untrustworthy rather than just one: "all clear" is the
    # outright lie, but a count is one too -- it is a count of what was looked
    # for, and the two flags that catch a silent stream were not among them. So
    # the caveat is appended in either case, and only "all clear" is withheld.
    verdicts = []
    if attention:
        verdicts.append(palette("%d need attention" % len(attention), "yellow"))
    elif not roster_error:
        verdicts.append(palette("all clear", "green"))
    if roster_error:
        verdicts.append(
            palette("roster unreadable — NEVER and STALE not checked", "crit")
        )
    for verdict in verdicts:
        lead += "  %s  %s" % (palette("·", "dim"), verdict)
    if newest:
        lead += "  %s  %s" % (
            palette("·", "dim"),
            palette("last run %s" % ago(newest, now), "dim"),
        )
    lines = [lead, ""]

    def field(label: str, value: str) -> str:
        return "  %s %s" % (palette(label.ljust(9), "dim"), value)

    lines.append(field("store", scrub(projection.get("root"))))
    lines.append(field("source", scrub(source)))
    if context:
        lines.append(field("context", scrub(context)))
    lines.append(
        field(
            "roster",
            short_path(roster_path)
            + (
                palette(" — unreadable: " + scrub(roster_error), "crit")
                if roster_error
                else ""
            ),
        )
    )
    lines.append(
        field(
            "findings",
            "%s %s"
            % (
                palette(str(findings), "bold"),
                palette(
                    "across %d run stream%s · %d critical"
                    % (len(ran), "" if len(ran) == 1 else "s", critical),
                    "crit" if critical else "dim",
                ),
            ),
        )
    )
    scopes = [
        e["latest"]["clusters"]
        for e in ran
        if isinstance(e["latest"].get("clusters"), int)
    ]
    if scopes:
        skipped = sum(
            e["latest"].get("skipped") or 0
            for e in ran
            if isinstance(e["latest"].get("skipped"), int)
        )
        # Widest, not summed: the streams overlap almost entirely -- nearly
        # all of them audit the same fleet -- so a total would report one
        # 16-cluster fleet as 150 clusters audited. The widest run is the
        # closest honest read of how much there is to cover, and the median
        # says whether the rest keep up with it.
        tail = "widest · %d median across %d run stream%s" % (
            sorted(scopes)[len(scopes) // 2],
            len(scopes),
            "" if len(scopes) == 1 else "s",
        )
        lines.append(
            field(
                "scope",
                "%s %s"
                % (
                    palette("%d units" % max(scopes), "bold"),
                    palette(
                        tail + (" · %d skipped" % skipped if skipped else ""),
                        "yellow" if skipped else "dim",
                    ),
                ),
            )
        )
    if total:
        clean = total - len(attention)
        cells = 18
        filled = int(round(cells * clean / float(total)))
        bar = "█" * filled + "░" * (cells - filled)
        lines.append(
            field(
                "health",
                "%s %s"
                % (
                    palette(bar, "green" if clean == total else "yellow"),
                    palette("%d of %d streams clean" % (clean, total), "dim"),
                ),
            )
        )
    return lines


def load_projection(args: argparse.Namespace) -> tuple[dict, str, str]:
    """The projection, where it came from, and the context it was read on."""
    if args.file:
        if args.file == "-":
            return as_projection(sys.stdin.read(), "stdin"), "stdin", ""
        try:
            text = Path(args.file).read_text(encoding="utf-8")
        except OSError as exc:
            raise ProjectionError(f"could not read {args.file}: {_oneline(exc)}") from exc
        return as_projection(text, args.file), f"file {args.file}", ""
    pod, container, context = args.pod, args.container, args.context
    if not pod:
        targets, context = resolve_target(args.namespace, args.context)
        pod, found = targets[0]
        container = container or found
        # Only rivals for the same container compete: the gateway beside a
        # sandbox is the fallback, not a second choice worth a note.
        rivals = [name for name, c in targets if c == found]
        if len(rivals) > 1:
            print(
                f"note: {len(rivals)} Running agent pods in {args.namespace}; "
                f"reading {pod} (--pod overrides)",
                file=sys.stderr,
            )
    container = container or DEFAULT_CONTAINER
    return (
        fetch_projection(pod, container, args.namespace, context),
        f"{args.namespace}/{pod} [{container}]",
        context or current_context(),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fleet_audit_status_view.py",
        description="Render the fleet-audit report store as an operator dashboard.",
        epilog=(
            "examples:\n"
            "  scripts/fleet_audit_status_view.py\n"
            "  scripts/fleet_audit_status_view.py --sort findings --flagged\n"
            "  scripts/fleet_audit_status_view.py --stream drift --stream cost\n"
            "  scripts/fleet_audit_status_view.py --watch 60\n"
            "  scripts/fleet_audit_status_view.py --json > store.json\n"
            "  scripts/fleet_audit_status_view.py --file store.json\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--namespace", "-n", default=DEFAULT_NAMESPACE)
    parser.add_argument(
        "--context",
        default=None,
        help="kubectl context; defaults to the current one, which on a shared "
             "kubeconfig is often a managed cluster rather than the hub",
    )
    parser.add_argument(
        "--pod",
        help=f"agent pod to read (default: discover by {POD_SELECTOR})",
    )
    parser.add_argument(
        "--container",
        default=None,
        help="container to exec into (default: the discovered pod's "
        f"{' or '.join(STORE_CONTAINERS)}, or {DEFAULT_CONTAINER} with --pod)",
    )
    parser.add_argument(
        "--file",
        help="read the projection's JSON from a file instead of the pod ('-' for stdin)",
    )
    parser.add_argument(
        "--roster",
        default=str(DEFAULT_ROSTER),
        help="cron roster for ENABLED/SCHEDULE (default: the checked-in seed)",
    )
    parser.add_argument(
        "--stream", "-s", action="append", default=[], metavar="NAME",
        help="only streams whose id contains this; repeatable",
    )
    parser.add_argument(
        "--flagged", action="store_true",
        help="only streams carrying a flag or a partial run",
    )
    parser.add_argument(
        "--gaps", action="store_true",
        help="spell out what each partial run did not read, instead of counting it",
    )
    parser.add_argument("--sort", choices=SORTS, default="stream")
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit the projection as JSON — exactly what --file consumes",
    )
    parser.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    parser.add_argument(
        "--ascii", action="store_true", help="ASCII borders instead of box-drawing characters"
    )
    parser.add_argument("--utc", action="store_true", help="timestamps in UTC, not local time")
    parser.add_argument("--width", type=int, default=0, help="output width; 0 detects the terminal")
    parser.add_argument(
        "--watch", type=int, default=0, metavar="SECONDS",
        help="redraw every SECONDS until interrupted",
    )
    return parser


def draw(args: argparse.Namespace, palette: Palette, box: dict, width: int) -> tuple[str, int]:
    projection, source, context = load_projection(args)
    if args.json:
        return json.dumps(projection, indent=2, sort_keys=True), exit_code(projection)
    roster, roster_error = load_roster(Path(args.roster))
    text = render(
        projection,
        roster,
        datetime.now(timezone.utc),
        args.roster,
        source,
        palette=palette,
        width=width,
        box=box,
        utc=args.utc,
        context=context,
        sort=args.sort,
        patterns=tuple(args.stream),
        flagged_only=args.flagged,
        show_gaps=args.gaps,
        roster_error=roster_error,
    )
    return text, max(exit_code(projection), 1 if roster_error else 0)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    palette = Palette(want_colour(args.color))
    box = BOX_ASCII if args.ascii else BOX_UNICODE
    width = args.width if args.width else max(
        80, min(shutil.get_terminal_size((160, 40)).columns, 220)
    )

    while True:
        try:
            text, code = draw(args, palette, box, width)
        except ProjectionError as exc:
            lines = [f"fleet-audit view: {exc}"]
            if exc.search_namespace:
                lines += context_hint(exc, args.context)
            if not args.watch:
                for line in lines:
                    print(line, file=sys.stderr)
                return 2
            # A watch that exits on the first failure is a dashboard nobody
            # leaves open: a rolled agent pod, an API server restart, or a
            # laptop that slept all end it, and the operator comes back to a
            # dead terminal rather than to the fleet. Draw the failure into the
            # frame and try again on the next tick.
            text, code = "\n".join(palette(scrub(line), "crit") for line in lines), 2
        if not args.watch:
            print(text)
            return code
        # Home then erase, so the frame is drawn over the old one rather than
        # after a scroll: a dashboard that walks down the scrollback is not one
        # anybody leaves open.
        sys.stdout.write("\x1b[H\x1b[2J" + text + "\n")
        sys.stdout.write(
            palette("  refreshing every %ds · ctrl-c to stop\n" % args.watch, "dim")
        )
        sys.stdout.flush()
        try:
            time.sleep(args.watch)
        except KeyboardInterrupt:
            return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
