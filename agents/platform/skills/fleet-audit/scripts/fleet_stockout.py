#!/usr/bin/env python3
"""fleet_stockout.py — Procedural collector for the Fleet Stockout
Prevention & Capacity Audit (`stockout-prevention`).

Its manifest is the contract in docs/designs/fleet-audit-collector-manifest.md,
which `audit_report.py finish --manifest-file` cross-checks the published
document against; the checks it runs are defined in
governance/stockout_prevention_sop.md.

**All twelve checks are converted.** Ten read structures this repository's
other collectors already read with confidence — a `ComputeClass`/`Deployment`/
`StatefulSet`/`StorageClass`/`Node` dump, `gcloud container node-pools list`,
`gcloud compute reservations list`, `gcloud compute regions describe
--format=json(quotas)`: `ccc-missing-fallbacks`, `ccc-no-ondemand-floor`,
`ccc-large-vm-scarcity`, `ccc-priority-starvation`,
`ccc-mixed-disk-generations`, `ccc-hyperdisk-incompatible`,
`quota-exhaustion-risk`, `single-zone-nodepool`, `reservation-mismatch-risk`,
`dangling-compute-class`.

The other two — `spot-scarcity-risk`, off the beta Spot capacity-advice API
(`gcloud beta compute advice capacity-history`), and
`autoscaler-out-of-resources`, off a Cloud Logging query against the
`cluster-autoscaler-visibility` schema — were prose-only for one reason: this
repository had not exercised either response shape anywhere else, and encoding
an unverified schema as tested code makes a wrong guess look like a fact. Both
shapes were read live against `adamparco-kage` on 2026-08-29 and are now
pinned by `test_fleet_stockout.py` against captured responses:

- `capacity-history` returns `{location, machineType, preemptionHistory:
  [{interval: {startTime, endTime}, preemptionRate: <float>}], priceHistory:
  [{interval, listPrice: {currencyCode, nanos, units?}}]}`. `preemptionRate`
  is a fraction, one entry per daily interval, 28 of them on a 30-day window.
  `listPrice.units` is absent below one currency unit, which is why
  `spot_list_price` reads both halves rather than `units` alone.
- `cluster-autoscaler-visibility` writes a stockout under *two* schemas, and a
  filter reading one silently passes a cluster failing under the other.
  `jsonPayload.resultInfo.results[].errorMsg.{messageId, parameters[]}` is a
  scale-up that was attempted and failed — the live sample carried
  `scale.up.error.out.of.resources` with the affected instance group in
  `parameters[0]`. `jsonPayload.noDecisionStatus.noScaleUp
  .unhandledPodGroups[].napFailureReasons[].messageId` is the
  node-auto-provisioning side, which never gets as far as an attempt. Healthy
  ticks carry neither and write `jsonPayload.status` instead.

Some sub-conditions are still uncovered, and governance/stockout_prevention_sop.md
§3 lists every one for the model to check by hand. Two are uncovered for the
reason the two checks above used to be — this repository has not exercised
the shape anywhere, and a guess encoded as tested code looks like a fact:

- **3.10(b)**, a `ComputeClass` targeting a reservation that does not exist or
  sits in an unreachable zone. `check_reservation_affinity` covers 3.10(a) and
  `check_reservation` covers 3.10(c); resolving a named reservation against the
  zones a cluster can actually reach is the part nothing here does.
- **3.12(b)**, a ComputeClass whose own `status.conditions` reports invalid
  configuration. `check_dangling_compute_class` covers 3.12(a), (c) and (d);
  that CRD's condition `type`/`reason` values are the unexercised shape.

The rest need a read this collector does not make: namespaces for 3.12(a)'s
namespace default, `advice capacity` for 3.8's obtainability arm, node pool
and workload shapes for 3.3, and the region's unreserved production workloads
for 3.10(c)'s qualifier.

The ComputeClass field names and family-generation lists below (Gen 2 vs
Gen 4/Hyperdisk-compatible in `ccc-mixed-disk-generations`, the
Hyperdisk-incompatible families in `ccc-hyperdisk-incompatible`) are exactly
what `governance/stockout_prevention_sop.md` §3 already specifies — this
collector implements that contract rather than re-deriving one, the same
choice every other converted stream makes when a field's real-world shape
is not independently verifiable from this repository alone.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, NamedTuple

MANIFEST_VERSION = 1

# A digest of this file, published in the manifest. `audit_report.py` compares
# it against the previous run's to tell a finding that stopped reproducing from
# a check that stopped looking; see `render_delta_comment`.
# Long enough that two collector sources will not collide, short enough to
# read in a log line. It has to agree across every collector: the comparison
# is between one run's revision and the last one's, so a file that truncated
# differently would report a moved collector on the run that changed it.
REVISION_DIGEST_CHARS = 12
CHECKS_REVISION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[
    :REVISION_DIGEST_CHARS
]

KUBECONFIG_DIR = Path(os.environ.get("HERMES_HOME") or "/opt/data") / ".kubeconfigs"
DEFAULT_TIMEOUT_S = 60
MAX_WORKERS = 8

GEN2_FAMILIES = {"n2", "n2d", "c2"}
GEN4_HYPERDISK_FAMILIES = {"c4", "n4", "c3"}  # §3.5's list, exactly -- §3.6 lists a different, wider set for its own check
HYPERDISK_INCOMPATIBLE_FAMILIES = {"c2", "n2", "e2"}
HYPERDISK_TYPES = {"hyperdisk-balanced", "hyperdisk-throughput", "hyperdisk-extreme"}
DEFAULT_STORAGE_CLASS_ANNOTATION = "storageclass.kubernetes.io/is-default-class"
# §3.5's exclusion: from this control-plane version the `dynamic-rwo` class
# makes the autoscaler disk-topology aware, so mixed generations stop
# deadlocking a claim that uses it.
DYNAMIC_RWO_CLASS = "dynamic-rwo"
DYNAMIC_RWO_MIN_VERSION = (1, 35, 3)
DYNAMIC_RWO_MIN_LABEL = ".".join(str(n) for n in DYNAMIC_RWO_MIN_VERSION)
GKE_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")

# §3.11. The three message ids that mean a scale-up failed for want of
# capacity, quota or pod IPs. Every other id the autoscaler emits is a
# scheduling decision rather than a stockout -- `scale.up.error.waiting.for
# .instances.timeout` and the `no.scale.up.in.backoff` family are the common
# ones -- and matching them would turn an ordinary busy cluster critical.
AUTOSCALER_LOG_ID = "container.googleapis.com/cluster-autoscaler-visibility"
AUTOSCALER_FRESHNESS = "24h"
AUTOSCALER_LOG_LIMIT = 1000
AUTOSCALER_STOCKOUT_MESSAGE_IDS = {
    "scale.up.error.out.of.resources",
    "scale.up.error.quota.exceeded",
    "scale.up.error.ip.space.exhausted",
}

# §3.8's ">20%", as a fraction, against the mean of the daily preemption
# rates the API returns.
SPOT_PREEMPTION_CEILING = 0.20
# Below this many daily intervals the mean is one bad day wide. A shape with
# fewer is reported as unmeasured rather than clean: a new machine type in a
# new region is exactly where a stockout hides, and it is also exactly where
# the history is too short to prove one.
SPOT_MIN_INTERVALS = 7
# Per cluster. A ComputeClass chain can name a dozen shapes and each is its own
# API round trip; past this the rest are named in `limitations` rather than
# read, because a collector that quietly stops looking is the failure this
# whole stream reports on.
SPOT_MAX_SHAPES = 8

# §3.3's ">32 cores", §3.4's "> 10" rules, §3.7's 90%, and §3.10(c)'s idle
# reservation: at most half in use with at least this many instances idle.
LARGE_VM_VCPUS = 32
MAX_PRIORITY_RULES = 10
QUOTA_EXHAUSTION_RATIO = 0.9
RESERVATION_IDLE_RATIO = 0.5
RESERVATION_IDLE_MIN_INSTANCES = 4
# "Alternative family fallbacks" (§3.3, §3.8) means at least a second family.
MIN_FALLBACK_FAMILIES = 2
ERROR_EXCERPT_CHARS = 300
STDERR_EXCERPT_CHARS = 200

# A cluster target is `<project>/<location>/<name>`, the qualified name
# `collect.py` publishes: a bare name is not unique once two locations of one
# project hold a cluster of the same name, and `finish` refuses a document that
# lists one name twice. Project-scoped checks keep `project/<id>`.
QUALIFIED_TARGET_SEPARATOR = "/"
PROJECT_TARGET_PREFIX = "project/"
# On a `project/<id>` entry whose `clusters list` completed and came back
# empty -- never a failed or zone-incomplete one. `audit_report.py` reads it
# (as `CLUSTERS_LISTED_KEY`) to tell a fleet with no clusters from a run that
# lost them, so the cluster checks' kind gap does not pin the run partial.
CLUSTERS_LISTED_KEY = "clusters_listed"
# §1's scope is every project the credential can see. These four are copied
# from `fleet_waste.py`, whose discovery this mirrors: a failed or narrowed
# project listing is one `project/UNENUMERATED_PROJECTS` target, so the loss
# is a row the document accounts for rather than a fleet that shrank to one
# project, and a project whose Kubernetes Engine API is off holds no cluster.
UNENUMERATED_PROJECTS_TARGET = PROJECT_TARGET_PREFIX + "UNENUMERATED_PROJECTS"
NO_PROJECT_IN_SCOPE_ERROR = (
    "no project in scope: there is no active gcloud project and `gcloud projects list` "
    "returned none, so this credential sees nothing to audit"
)
SCOPED_RUN_NOTE = (
    "scope narrowed to project {project!r} by `--project`: discovery was skipped, so no other "
    "project in this fleet was named or read, and this run cannot speak for their clusters."
)
API_DISABLED_MARKERS = ("SERVICE_DISABLED", "accessNotConfigured", "has not been used in project")
# The project an API refusal names; `fleet_waste.REFUSED_PROJECT_NUMBER_RE`
# carries the reasoning.
REFUSED_PROJECT_NUMBER_RE = re.compile(r"\bprojects?[ /](\d+)\b")
# gcloud's word for a zone that timed out during `clusters list`: the command
# still exits 0, with the clusters the other zones returned and this line on
# stderr, so the silent zone's clusters would read as nonexistent. See
# `fleet_drift.ZONE_TIMEOUT_MARKER`.
ZONE_TIMEOUT_MARKER = "did not respond"
# When the collector stops starting project reads, in seconds from its own
# start; `fleet_waste.py` carries the same bound for the same 600 s terminal
# call, and its comment has the reasoning. A project not reached by then
# becomes a `gate-failed` `project/<p>` target. It does not bound the
# cluster reads, which every fleet collector in this directory leaves open.
PROJECT_READ_DEADLINE_S = 420
# `gcloud projects list`'s own timeout; `fleet_waste.py` carries the same one
# and its reasoning.
PROJECTS_LIST_TIMEOUT_S = 240
PROJECT_DEADLINE_ERROR = (
    "not read: the collector stops starting project reads {budget} s after it starts, so the "
    "run can end inside its terminal timeout with a manifest, and this project's turn came "
    "after that. Rerun with `--project {project}` to read it on its own."
)
NO_CLUSTER_QUOTA_REASON = (
    "project {project!r} holds no cluster, so there is no region whose quota a cluster "
    "could exhaust"
)
NOTHING_COLLECTED_ERROR = (
    "nothing collected: none of the {count} project(s) in scope yielded a target -- each failed "
    "its cluster listing, went unread past the deadline, or has the Compute Engine API off "
    "and no cluster. First: {first}"
)

# §2's standard exclusions. S1's list is the one `fleet_waste.py` and
# `collect.py` carry, copied rather than imported: this collector imports only
# the leaf parsers it shares with them, so a change to either sibling's
# exclusions cannot move this stream's.
SYSTEM_NAMESPACES = frozenset(
    {
        "kube-system", "kube-public", "kube-node-lease", "gmp-system", "gmp-public", "gke-gmp-system",
        "cnrm-system", "configconnector-operator-system", "krmapihosting-system", "istio-system",
        "asm-system", "anthos-identity-service", "gatekeeper-system", "composer-system",
    }
)
SYSTEM_NAMESPACE_PREFIXES = ("gke-", "config-management-")
ADDON_MANAGER_LABEL = "addonmanager.kubernetes.io/mode"
OPT_OUT_LABEL = "kubeagents.x-k8s.io/stockout-audit"
OPT_OUT_VALUE = "exempt"
# §2's "non-production": one of these as a `-`/`_`-delimited token of a name,
# or as the value of one of the label keys below.
NON_PRODUCTION_TOKENS = frozenset({"test", "staging", "stage", "dev", "sandbox", "qa"})
ENVIRONMENT_LABEL_KEYS = ("environment", "env", "stage", "tier")
NAME_TOKEN_RE = re.compile(r"[-_]")
# §3.7 is about node capacity -- "GPU/TPU/CPU limits" in its own words -- and a
# region describe returns every Compute quota there is, 164 of them on
# `us-east4`. Without this filter a project at 92% of `BACKEND_BUCKETS` or
# `AFFINITY_GROUPS` publishes a `critical` stockout finding, and 95 of the 164
# metrics have nothing to do with whether the autoscaler can get a node.
_CAPACITY_QUOTA_RE = re.compile(r"(?:^|_)(?:CPUS|GPUS)(?:_ALL_REGIONS)?$|TPU")
# A `COMMITTED_*` metric limits how much capacity committed-use discounts can
# buy. At 100% it caps nothing the autoscaler asks for: nodes are bounded by
# the ordinary quota beside it.
_COMMITMENT_QUOTA_PREFIX = "COMMITTED_"
# How full a pool has to be for the ceiling arm to fire. §3.9 calls the
# ceiling "a hard stop, not a soft one, so 'close to it' means measurably
# close, not a judgment call".
NODEPOOL_CEILING_FRACTION = 0.9

# §3.9's Impact, one per arm. The arms share a slug and nothing else, so no
# single sentence is true of both: the zone-locked arm is about a stockout in
# one zone, and the ceiling arm fires on regional pools spanning three of
# them, where a zonal-stockout sentence is simply false. The blended sentence
# this replaces claimed both at once ("locked to a single zone or near its
# scaling ceiling: any zonal stockout or scale event halts cluster
# auto-scaling") and so was half wrong whichever arm published it.
_IMPACT_ZONE_LOCKED = (
    "Node pool is locked to a single zone: a stockout in that zone halts "
    "scale-up of this pool, and pods that can only run there -- pinned by a "
    "nodeSelector, or by a PersistentVolume that is a zonal disk in that zone "
    "-- stay Pending. The autoscaler backs off the failing node group on its "
    "own and keeps scaling the others, so the stall belongs to this pool "
    "rather than the cluster, until enough nodes cluster-wide go unready to "
    "trip the autoscaler's 45% health check and stop every operation."
)
# The ceiling arm's Impact is assembled rather than stored, because three of
# its clauses are contingent and the previous single sentence asserted all
# three unconditionally. See `_ceiling_impact`.
_CEILING_AT_LIMIT = "The pool is at its effective node ceiling ({live}/{ceiling}), so the autoscaler adds no further node to it"
_CEILING_NEAR_LIMIT = "The pool is at {percent:.0f}% of its effective node ceiling ({live}/{ceiling}), so at most {headroom} more {noun} can be added before scale-up stops there"
# Cluster autoscaler skips a node group only on `currentTargetSize >=
# MaxSize`, and target size is what it compares -- not the live Node count
# this check can see. A pool at 27 live may already be targeting 30.
_CEILING_TARGET_CAVEAT = (
    " -- and fewer if the autoscaler's target already sits above the live count, "
    "which is what it actually compares."
)
# Arm 2 does not require `not has_nap`, so on a NAP cluster every at-ceiling
# pool lands here, single-zone ones included. NAP creates a new pool for
# pending workloads, so "the next scale-up stops there" is false of the
# cluster even where it is true of the pool.
_CEILING_NAP_CLAUSE = (
    " Node auto-provisioning is on, so the cluster can create a different pool "
    "instead; what stops is this pool's growth, not the cluster's."
)
# "The limit is configuration rather than anything about supply" was the
# earlier claim and it is only half true. The *value* is configuration --
# nothing shrinks `maxSize` at runtime -- but reaching it is often a supply
# artefact: when one zonal MIG is backed off the autoscaler pushes the whole
# delta into the surviving zones, so "27/30" can be the footprint of a
# stockout next door. Regional CPU quota can also bind below the field.
_CEILING_SUPPLY_CLAUSE = (
    " That ceiling is configuration, not capacity -- but the pool may have "
    "reached it because scale-up was displaced here from a zone that could not "
    "supply, or because regional quota binds below the configured limit."
)


def log(msg: str) -> None:
    print(f"[fleet_stockout] {msg}", file=sys.stderr, flush=True)


def target_name(project: str, location: str, name: str) -> str:
    """The qualified cluster target; see `QUALIFIED_TARGET_SEPARATOR`."""
    return QUALIFIED_TARGET_SEPARATOR.join(part for part in (project, location, name) if part)


def _is_system_namespace(ns: str) -> bool:
    return ns in SYSTEM_NAMESPACES or ns.startswith(SYSTEM_NAMESPACE_PREFIXES)


def standard_excluded(obj: dict) -> str | None:
    """Which of §2's S1–S5 excludes `obj`, or None.

    Applied to every workload and ComputeClass before any check reads it, so
    an excluded workload neither fires a check nor makes a ComputeClass count
    as referenced by an inference or stateful workload.
    """
    meta = obj.get("metadata") or {}
    labels = meta.get("labels") or {}
    if _is_system_namespace(meta.get("namespace") or ""):
        return "S1"
    if ADDON_MANAGER_LABEL in labels:
        return "S2"
    if meta.get("ownerReferences"):
        return "S3"
    if labels.get(OPT_OUT_LABEL) == OPT_OUT_VALUE:
        return "S4"
    if (obj.get("spec") or {}).get("replicas") == 0:
        return "S5"
    return None


def _has_non_production_token(name: str) -> bool:
    return any(token in NON_PRODUCTION_TOKENS for token in NAME_TOKEN_RE.split((name or "").lower()))


def is_non_production(name: str, labels: dict | None = None, namespace: str = "") -> bool:
    """§2's "non-production", for the checks that name it (3.2, 3.8, 3.10)."""
    labels = labels or {}
    if labels.get(OPT_OUT_LABEL) == OPT_OUT_VALUE:
        return True
    if _has_non_production_token(name) or _has_non_production_token(namespace):
        return True
    return any(
        str(labels.get(key) or "").lower() in NON_PRODUCTION_TOKENS for key in ENVIRONMENT_LABEL_KEYS
    )


class Run(NamedTuple):
    argv: list[str]
    rc: int
    stdout: str
    stderr: str
    duration_s: float


RunFn = Callable[..., Run]


def _text(output: str | bytes | None) -> str:
    return output.decode(errors="replace") if isinstance(output, bytes) else (output or "")


def default_run(argv: list[str], *, env: dict | None = None, timeout: int = DEFAULT_TIMEOUT_S) -> Run:
    t0 = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=timeout)
        return Run(argv, proc.returncode, proc.stdout, proc.stderr, time.monotonic() - t0)
    except subprocess.TimeoutExpired as exc:
        # `TimeoutExpired` carries whatever the child wrote as bytes, `text=True`
        # notwithstanding, and every consumer of `Run` searches and slices it as
        # str. `collect.py`'s `_text` does the same.
        return Run(argv, 124, _text(exc.stdout), _text(exc.stderr), time.monotonic() - t0)
    except Exception as exc:
        return Run(argv, -1, "", str(exc), time.monotonic() - t0)


def run_and_gate(argv: list[str], *, run: RunFn, env: dict | None = None) -> tuple[object | None, Run]:
    result = run(argv, env=env)
    if result.rc != 0 or not result.stdout.strip():
        return None, result
    try:
        return json.loads(result.stdout), result
    except json.JSONDecodeError:
        return None, result


def output_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _record(argv_str: str, result: Run) -> dict:
    return {
        "command": argv_str,
        "rc": result.rc,
        "duration_s": round(result.duration_s, 2),
        "output_sha256": output_digest(result.stdout),
    }


def kubeconfig_path(project: str, cluster: str, location: str) -> Path:
    return KUBECONFIG_DIR / f"kubeconfig_{project}_{cluster}_{location}.yaml"


def fetch_credentials(project: str, cluster: str, location: str, *, run: RunFn) -> tuple[Path, Run]:
    kc = kubeconfig_path(project, cluster, location)
    kc.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "KUBECONFIG": str(kc)}
    result = run(
        ["gcloud", "container", "clusters", "get-credentials", cluster, "--location", location, "--project", project],
        env=env,
    )
    return kc, result


# See `fleet_waste.AUDITABLE_STATUSES` for the reasoning, which applies
# unchanged here: GKE sets `RECONCILING` while work proceeds on a cluster whose
# API server stays up, it is transient and ordinary, and excluding it dropped
# clusters from audits for the duration of any routine config change. The
# enumeration helpers here are near-copies of fleet_waste.py's; the two
# collectors import only the leaf parsers they share, not each other's fleet
# walk, so a change to one stream's enumeration cannot move the other's.
AUDITABLE_STATUSES = frozenset({"RUNNING", "RECONCILING"})


def not_running_entry(c: dict, project: str) -> dict:
    """A manifest target for a cluster whose state rules out auditing it.

    Filtering `clusters list` down to RUNNING is right -- a PROVISIONING
    cluster has no API server to read. Dropping the rest without a trace is
    not: the manifest is the run's only account of the fleet it saw, so a
    cluster absent from it reads exactly like a cluster that does not exist,
    and the document can publish a fleet-wide all-clear over a fleet quietly
    missing it. DEGRADED is the case that makes this bite. Recorded as a
    non-`collected` target, the loss is something the document has to place in
    `scope.skipped` with a reason. `collect.py` carries the same helper.
    """
    location = c.get("location") or c.get("zone") or ""
    return {
        "name": target_name(project, location, c.get("name", "")),
        "project": project,
        "location": location,
        "autopilot": bool((c.get("autopilot") or {}).get("enabled")),
        "has_nap": bool((c.get("autoscaling") or {}).get("enableNodeAutoprovisioning")),
        "outcome": "unreachable",
        "error": f"cluster status is {c.get('status') or 'unknown'}, which is neither RUNNING nor RECONCILING; no check was evaluated against it",
    }


class NoProjectInScope(Exception):
    """Discovery named no project at all, which is not a fleet of empty projects."""


def get_target_projects(cli_project: str | None, *, run: RunFn) -> tuple[list[str], str | None]:
    """The active project plus every other listed project, or the one
    `--project` names; a copy of `fleet_waste.get_target_projects`, which
    carries the reasoning. The second value is set when the scope is provably
    short of the fleet and becomes an `UNENUMERATED_PROJECTS_TARGET` entry.
    Raises `NoProjectInScope` when the credential sees no project."""
    if cli_project:
        return [cli_project], SCOPED_RUN_NOTE.format(project=cli_project)

    result = run(["gcloud", "config", "get-value", "project"])
    base = result.stdout.strip() if result.rc == 0 else ""
    projects = [base] if base else []

    list_result = run(["gcloud", "projects", "list", "--format", "value(projectId)"], timeout=PROJECTS_LIST_TIMEOUT_S)
    if list_result.rc != 0:
        stderr = list_result.stderr.strip()[:ERROR_EXCERPT_CHARS] or "no stderr"
        if not base:
            raise NoProjectInScope(
                f"project discovery failed: `gcloud config get-value project` rc={result.rc} "
                f"named no project and `gcloud projects list` rc={list_result.rc}: {stderr}"
            )
        partial = (
            f"`gcloud projects list` rc={list_result.rc}: {stderr}. The scope fell back to "
            f"the active project {base!r}; how many other projects the fleet holds is unknown."
        )
        log(f"WARNING: {partial}")
        return projects, partial

    listed = [p.strip() for p in (list_result.stdout or "").splitlines() if p.strip()]
    candidates = [p for p in listed if p != base]
    if not base and not candidates:
        raise NoProjectInScope(NO_PROJECT_IN_SCOPE_ERROR)
    projects.extend(candidates)
    if base and base not in listed:
        partial = (
            f"`gcloud projects list` rc=0 did not name the active project {base!r}, "
            f"so it is filtered rather than complete: it returned {len(listed)} "
            "project(s) and this run reads clusters in one it did not return. How "
            "many other projects the fleet holds is unknown."
        )
        log(f"WARNING: {partial}")
        return projects, partial
    return projects, None


def _api_disabled(result: Run) -> bool:
    return result.rc != 0 and any(marker in result.stderr for marker in API_DISABLED_MARKERS)


class IncompleteEnumeration(RuntimeError):
    """`clusters list` answered, but a zone did not respond.

    Carries the clusters that did arrive, so they are still audited, while
    the project's own target reports the enumeration as incomplete: its
    checks compare against the project's whole cluster list, and a silent
    zone's clusters are missing from it. A caller that catches only
    `RuntimeError` still gets the conservative answer, the project unread."""

    def __init__(self, message: str, running: list[dict], not_running: list[dict]):
        super().__init__(message)
        self.running = running
        self.not_running = not_running


def refusal_names_project(project: str, stderr: str, *, run: RunFn) -> bool:
    """Whether an API-disabled refusal is `project`'s own; a copy of
    `fleet_waste.refusal_names_project`, which carries the reasoning."""
    numbers = set(REFUSED_PROJECT_NUMBER_RE.findall(stderr))
    if not numbers:
        return re.search(rf"\bprojects?[ /]{re.escape(project)}(?![\w-])", stderr) is not None
    described = run(["gcloud", "projects", "describe", project, "--format", "value(projectNumber)"])
    return described.rc == 0 and numbers == {described.stdout.strip()}


def enumerate_clusters(project: str, *, run: RunFn) -> tuple[list[dict], list[dict]]:
    result = run(
        [
            "gcloud", "container", "clusters", "list", "--project", project,
            "--format", "json(name,location,status,currentMasterVersion,autopilot.enabled,autoscaling.enableNodeAutoprovisioning)",
        ]
    )
    if result.rc != 0:
        if _api_disabled(result):
            if refusal_names_project(project, result.stderr, run=run):
                log(f"{project}: Kubernetes Engine API is not enabled; no cluster can exist here")
                return [], []
            raise RuntimeError(
                f"cluster enumeration refused (rc={result.rc}) by a Kubernetes Engine API that is off in a "
                f"project other than {project!r}, such as a quota project, so this project's clusters are "
                f"unknown: {result.stderr.strip()[:ERROR_EXCERPT_CHARS]}"
            )
        raise RuntimeError(f"cluster enumeration failed (rc={result.rc}): {result.stderr.strip()[:ERROR_EXCERPT_CHARS]}")
    try:
        clusters = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"cluster enumeration returned no parseable JSON: {exc}") from exc
    running = [
        {
            "name": c["name"],
            "location": c.get("location"),
            "project": project,
            "autopilot": bool((c.get("autopilot") or {}).get("enabled")),
            # A cluster-level setting, not derivable from any one node
            # pool's own autoscaling config -- see `check_single_zone_nodepool`.
            "has_nap": bool((c.get("autoscaling") or {}).get("enableNodeAutoprovisioning")),
            "version": c.get("currentMasterVersion") or "",
        }
        for c in clusters
        if c.get("status") in AUDITABLE_STATUSES
    ]
    not_running = [not_running_entry(c, project) for c in clusters if c.get("status") not in AUDITABLE_STATUSES]
    incomplete = [line.strip() for line in result.stderr.splitlines() if ZONE_TIMEOUT_MARKER in line]
    if incomplete:
        detail = " ".join(incomplete)[:ERROR_EXCERPT_CHARS]
        log(f"{project}: clusters list returned {len(clusters)} cluster(s) but is incomplete: {detail}")
        raise IncompleteEnumeration(f"clusters list rc=0 but incomplete: {detail}", running, not_running)
    return running, not_running


def region_of(location: str) -> str:
    """A zonal location (`us-central1-a`) truncated to its region
    (`us-central1`); a regional location is returned unchanged."""
    parts = (location or "").rsplit("-", 1)
    return parts[0] if len(parts) == 2 and len(parts[1]) == 1 else location


# --------------------------------------------------------------------------- #
# ComputeClass structural analysis (3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.12)
# --------------------------------------------------------------------------- #


def _gce_int(value: object, default: int | None = None) -> int | None:
    """An int64 out of a GCE JSON response, which serialises them as strings.

    `{"count": "100", "inUseCount": "3"}` is what `gcloud compute reservations
    list --format json` returns -- the Google API int64-to-string convention,
    reproduced verbatim by gcloud's `resource_projector`. Dividing two of them
    raises `TypeError`, and `collect_fleet`'s per-target guard turns that into
    a `gate-failed` entry, so one string-typed reservation would cost its whole
    project every check rather than one finding.

    `default` is for a field the API omits when it is zero -- proto3 JSON drops
    default values, so an unused reservation carries no `inUseCount` at all,
    and reading that absence as "unknown, skip" hides the maximum-waste case
    the check exists to find.
    """
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _priority_is_spot(p: dict) -> bool:
    return bool(p.get("spot")) or p.get("provisioningModel") == "SPOT"


def _priority_family(p: dict) -> str:
    if p.get("machineFamily"):
        return p["machineFamily"]
    mt = p.get("machineType") or ""
    return mt.split("-", 1)[0] if mt else ""


def _priority_size_class(p: dict) -> str:
    """A coarse vCPU-count bucket, used only to detect that priorities vary
    *some* size dimension -- not the precise count §3.3 reasons about."""
    mt = p.get("machineType") or ""
    m = re.search(r"-(\d+)$", mt)
    return m.group(1) if m else ""


def _priority_zones(p: dict) -> tuple[str, ...]:
    """The zones a priority will attempt, from either of the CRD's two homes.

    `location.zones` is the field, per
    `skills/gke-compute-classes/references/compute-class-crd-fields.md`. There
    is no top-level `priorities[].zones`, so reading one scored the Zone
    dimension 0 on every ComputeClass in the fleet and left §3.1 counting three
    dimensions while calling it four -- a chain whose priorities differ only in
    zone and one other dimension scored 1/4 and published `critical`.

    `reservations.specific[].zones` is the second home: the same reference notes
    that `location.zones` cannot be combined with `affinity: Specific`, so a
    chain using specific reservations expresses its zone spread there instead.
    """
    location_zones = (p.get("location") or {}).get("zones") or []
    reserved_zones = [
        zone
        for entry in ((p.get("reservations") or {}).get("specific") or [])
        for zone in (entry.get("zones") or [])
    ]
    return tuple(sorted(set(location_zones) | set(reserved_zones)))


def _priority_is_pod_family(p: dict) -> bool:
    """Does this priority delegate the machine shape to GKE rather than name one?

    An Autopilot-mode ComputeClass (`spec.autopilot.enabled`) writes its chain as
    `podFamily: general-purpose` instead of a `machineFamily`/`machineType`. GKE's
    own capacity broker then chooses the shape at scale-up time, across families
    and zones.
    """
    return bool(p.get("podFamily")) and not p.get("machineFamily") and not p.get("machineType")


def check_ccc_missing_fallbacks(cc: dict) -> dict | None:
    priorities = (cc.get("spec") or {}).get("priorities") or []
    if not priorities:
        return None
    # §3.1 flags a chain "pinned to a single machine family". A pod-family
    # priority is pinned to no machine family at all -- it hands the choice to
    # GKE, which is the broadest fallback the API can express, so there is
    # nothing here to flag.
    #
    # Scoring the dimensions anyway is what made this the fleet's loudest false
    # positive. `_priority_family` reads only `machineFamily`/`machineType`, so
    # every pod-family entry returned "", `families` came back *empty*, and an
    # unreadable chain scored the same 0/4 as a genuinely pinned one. That fired
    # `critical` against `autopilot`, `autopilot-arm` and `autopilot-spot` -- the
    # classes GKE pre-installs and reconciles on every Autopilot cluster -- so
    # each cluster contributed three findings whose own remediation had to
    # conclude `kind: manual`, because a GKE-managed object has no manifest in
    # the GitOps clone to write to. 49 of one run's 67 findings were this.
    #
    # `all()`, not `any()`: a chain mixing pod-family and machine-typed entries
    # was hand-authored, its machine-typed entries are real pins, and a finding
    # against it is something an operator can act on.
    if all(_priority_is_pod_family(p) for p in priorities):
        return None
    families = {_priority_family(p) for p in priorities if _priority_family(p)}
    spots = {_priority_is_spot(p) for p in priorities}
    sizes = {_priority_size_class(p) for p in priorities if _priority_size_class(p)}
    zones = {_priority_zones(p) for p in priorities if _priority_zones(p)}
    dimensions_varied = sum(1 for s in (families, spots, sizes, zones) if len(s) > 1)
    if dimensions_varied >= 2:
        return None
    return {
        "object": f"ComputeClass/{cc['metadata']['name']}",
        "excerpt": f"priorities vary {dimensions_varied}/4 obtainability dimensions (families={sorted(families)}, spot-mix={sorted(spots)}, sizes={sorted(sizes)}, zones={sorted(zones)})",
    }


def check_ccc_no_ondemand_floor(cc: dict, referenced_by_inference: bool) -> dict | None:
    priorities = (cc.get("spec") or {}).get("priorities") or []
    if not priorities or not all(_priority_is_spot(p) for p in priorities):
        return None
    # The guard `check_ccc_missing_fallbacks` carries, for the reason §3.1
    # already gives and §3.2 omits: `autopilot-spot` is one of the three classes
    # GKE pre-installs and reconciles on every Autopilot cluster, its whole
    # chain is the single priority `{podFamily: general-purpose, spot: true}`,
    # and it is GKE-managed, so there is no manifest in the GitOps clone to
    # append an On-Demand rule to. §3.2's remediation is `kind: manifest`; on
    # this object it cannot be written, which is precisely why §3.1 excludes the
    # same three. Being all-Spot is what that class *is*, not a way someone
    # misconfigured it, and the finding's own recommendation conceded as much by
    # telling the reader to go and declare a different class instead. It fired
    # once per Autopilot cluster on every run -- 17 of one run's 18 findings.
    #
    # Not suppressed when an inference workload actually selects it. There the
    # risk is real and immediate in a way the §3.2 escalation already grades
    # `critical`, and a manual finding beats silence even though the object
    # still has no manifest. Nothing on this fleet selects any ComputeClass at
    # all, so today this branch costs the ledger nothing and saves it 17.
    if all(_priority_is_pod_family(p) for p in priorities) and not referenced_by_inference:
        return None
    hit = {"object": f"ComputeClass/{cc['metadata']['name']}", "excerpt": f"{len(priorities)} priorities, all Spot, no On-Demand floor"}
    if referenced_by_inference:
        # §3.2's escalation, reusing ai-security-audit's own discriminators
        # (`collect.py`'s image and accelerator patterns) rather than a second "which
        # workloads count as inference" rule the two SOPs could drift apart
        # on.
        hit["severity"] = "critical"
        hit["excerpt"] += "; referenced by an inference workload"
    return hit


def check_ccc_large_vm_scarcity(cc: dict) -> list[dict]:
    from fleet_waste import _machine_type_vcpus  # shared with fleet-wide-cost-analysis's own vCPU parser

    priorities = (cc.get("spec") or {}).get("priorities") or []
    families = {_priority_family(p) for p in priorities if _priority_family(p)}
    if len(families) >= MIN_FALLBACK_FAMILIES:
        return []
    # One candidate per ComputeClass, naming every large shape: the finding
    # identity is (check, cluster, namespace, object), so one candidate per
    # priority gave two findings the same id, which `finish` refuses.
    large = []
    for p in priorities:
        mt = p.get("machineType") or ""
        vcpus = _machine_type_vcpus(mt)
        if vcpus and vcpus > LARGE_VM_VCPUS and f"{mt} ({vcpus} vCPU)" not in large:
            large.append(f"{mt} ({vcpus} vCPU)")
    if not large:
        return []
    return [{"object": f"ComputeClass/{cc['metadata']['name']}", "excerpt": f"priorities request {', '.join(large)} with only {len(families)} machine famil{'y' if len(families) == 1 else 'ies'} in the chain"}]


def check_ccc_priority_starvation(cc: dict) -> dict | None:
    priorities = (cc.get("spec") or {}).get("priorities") or []
    if len(priorities) > MAX_PRIORITY_RULES:
        return {"object": f"ComputeClass/{cc['metadata']['name']}", "excerpt": f"{len(priorities)} priority rules (> {MAX_PRIORITY_RULES})"}
    return None


def check_ccc_mixed_disk_generations(cc: dict, stateful_referencing: bool) -> dict | None:
    if not stateful_referencing:
        return None
    priorities = (cc.get("spec") or {}).get("priorities") or []
    families = {_priority_family(p) for p in priorities if _priority_family(p)}
    has_gen2 = bool(families & GEN2_FAMILIES)
    has_gen4 = bool(families & GEN4_HYPERDISK_FAMILIES)
    if has_gen2 and has_gen4:
        return {"object": f"ComputeClass/{cc['metadata']['name']}", "excerpt": f"priorities mix Gen 2 ({sorted(families & GEN2_FAMILIES)}) and Gen 4/Hyperdisk ({sorted(families & GEN4_HYPERDISK_FAMILIES)}) families on a stateful, PV-backed workload"}
    return None


def check_ccc_hyperdisk_incompatible(cc: dict, uses_hyperdisk: bool) -> dict | None:
    if not uses_hyperdisk:
        return None
    priorities = (cc.get("spec") or {}).get("priorities") or []
    families = {_priority_family(p) for p in priorities if _priority_family(p)}
    incompatible = families & HYPERDISK_INCOMPATIBLE_FAMILIES
    if incompatible:
        return {"object": f"ComputeClass/{cc['metadata']['name']}", "excerpt": f"fallback includes Hyperdisk-incompatible families {sorted(incompatible)}"}
    return None


def check_dangling_compute_class(workload: dict, compute_classes_by_name: dict[str, dict], node_pool_labels: set[str] | None) -> dict | None:
    """`node_pool_labels` is `None` when the pool labels are unknown -- an
    Autopilot cluster with no user node pools, or a `node-pools list` the
    caller could not read -- and a set, possibly empty, when they are known.

    The second arm used to test the set for truthiness, which silently
    conflated the two: a Standard cluster whose pools carry no
    `cloud.google.com/compute-class` label at all is the arm's own target case,
    and an empty set turned it off there. Only `None` turns it off now, and
    §3.9's `limitations` sentence is what says the read failed.
    """
    spec = workload.get("spec") or {}
    template_spec = ((spec.get("template") or {}).get("spec")) or spec
    # Deployments and StatefulSets are namespaced, and `derive_finding_id` keys
    # on (check, cluster, namespace, object): without this, `Deployment/api` in
    # `team-a` and in `team-b` share one identity, so one is dropped and the
    # delta alternates between them run to run.
    namespace = (workload.get("metadata") or {}).get("namespace", "")
    obj = f"{workload['kind']}/{workload['metadata']['name']}"
    selector = (template_spec.get("nodeSelector") or {}).get("cloud.google.com/compute-class")
    if selector and selector not in compute_classes_by_name:
        return {"namespace": namespace, "object": obj, "excerpt": f"nodeSelector references ComputeClass {selector!r}, which does not exist"}
    if selector:
        cc = compute_classes_by_name[selector]
        # `is not True`, not `is False`: the CRD's field defaults to off, so the
        # ordinary way to leave auto-creation disabled is to omit
        # `nodePoolAutoCreation` altogether, and reading that absence as
        # "enabled" exempts the common case from the check.
        auto_create = ((cc.get("spec") or {}).get("nodePoolAutoCreation") or {}).get("enabled")
        if auto_create is not True and node_pool_labels is not None and selector not in node_pool_labels:
            return {"namespace": namespace, "object": obj, "excerpt": f"references ComputeClass {selector!r} with nodePoolAutoCreation disabled and no matching node pool label/taint"}
    if selector:
        # `limits` as well as `requests`. The canonical GPU manifest -- Google's
        # own documentation, and effectively every real workload -- sets
        # `nvidia.com/gpu` under `limits` alone. Kubernetes defaults `requests`
        # from `limits` on a Pod, but these are Deployment and StatefulSet pod
        # *templates*, which are not defaulted, so a requests-only read makes
        # this check inert rather than failing.
        def _wants_gpu(container: dict) -> bool:
            resources = container.get("resources") or {}
            return any(
                "nvidia.com/gpu" in (resources.get(field) or {})
                for field in ("requests", "limits")
            )

        requests_gpu = any(_wants_gpu(c) for c in template_spec.get("containers") or [])
        tolerates_gpu = any(t.get("key") == "nvidia.com/gpu" for t in template_spec.get("tolerations") or [])
        if requests_gpu and not tolerates_gpu:
            return {"namespace": namespace, "object": obj, "excerpt": f"GPU workload references ComputeClass {selector!r} without an nvidia.com/gpu toleration"}
    return None


# --------------------------------------------------------------------------- #
# 3.9 single-zone-nodepool
# --------------------------------------------------------------------------- #


def _ceiling_impact(ceiling: int, live: int, has_nap: bool) -> str:
    """§3.9's ceiling Impact, with only the clauses that hold.

    The sentence this replaces said "the next scale-up stops there whatever
    capacity the zone has" at any fullness over 90%, and cluster autoscaler
    skips a node group on exactly one condition: `currentTargetSize >=
    nodeGroup.MaxSize()`. At the check's own worked example, `27 >= 30` is
    false and the next scale-up adds three more nodes. A 90% threshold
    guarantees 10% headroom, so the claim was true at exactly one point in the
    band that fires it -- and a test asserted it at 27/30.
    """
    headroom = ceiling - live
    if headroom <= 0:
        return _CEILING_AT_LIMIT.format(live=live, ceiling=ceiling) + "." + (
            _CEILING_NAP_CLAUSE if has_nap else ""
        ) + _CEILING_SUPPLY_CLAUSE
    near = _CEILING_NEAR_LIMIT.format(
        percent=live / ceiling * 100,
        live=live,
        ceiling=ceiling,
        headroom=headroom,
        noun="node" if headroom == 1 else "nodes",
    )
    return (
        near
        + _CEILING_TARGET_CAVEAT
        + (_CEILING_NAP_CLAUSE if has_nap else "")
        + _CEILING_SUPPLY_CLAUSE
    )


def _pool_ceiling(autoscaling: dict, locations: list) -> tuple[int | None, str]:
    """The pool's real node ceiling, and the field the number came from.

    `maxNodeCount` is per *location* -- the GKE API's own wording is "maximum
    number of nodes for one location in the NodePool" -- while
    `totalMaxNodeCount` is pool-wide; the two are mutually exclusive. The live
    count this is compared against is a pool total summed over every zone, so
    reading the per-zone field as a pool ceiling makes a three-zone pool look
    three times as full as it is. That matters more than it sounds: multi-zone
    is precisely what the zone-locked arm's remediation tells operators to
    build, so taking this check's advice was what armed its false positive.

    Returns `(None, "")` for any input that does not pin both numbers. A pool
    whose `autoscaling` is present but disabled has no ceiling to be near, and
    a pool with no `locations` has an unknown zone span -- multiplying
    `maxNodeCount` by an assumed 1 there would call a three-zone pool at 30%
    full 90% full, which is the false positive this function exists to remove,
    re-armed by a missing field.
    """
    if not autoscaling.get("enabled"):
        return None, ""
    total_max = _gce_int(autoscaling.get("totalMaxNodeCount"))
    if total_max:
        return total_max, "totalMaxNodeCount"
    per_zone = _gce_int(autoscaling.get("maxNodeCount"))
    if not per_zone:
        return None, ""
    zones = len(locations)
    if zones == 0:
        return None, ""
    if zones == 1:
        return per_zone, "maxNodeCount"
    return per_zone * zones, f"maxNodeCount {per_zone}/zone x {zones} zones"


def check_single_zone_nodepool(pool: dict, has_nap: bool, current_node_count: int) -> dict | None:
    """`current_node_count` must be the pool's *live* node count (counted
    from the cluster's own `Node` objects, grouped by the
    `cloud.google.com/gke-nodepool` label) -- `initialNodeCount` is a
    creation-time field the GKE API never updates as the autoscaler scales
    the pool, so it cannot stand in for "how close to `maxNodeCount` is this
    pool right now".

    The two arms are not exclusive and the check must not pick one. A
    single-zone pool at 2/2 returned only the zone-locked sentence, which
    makes the stall contingent on a future stockout when scale-up is already
    stopped for a reason that has nothing to do with supply. Both conditions
    hold, so both are reported.
    """
    locations = pool.get("locations") or []
    autoscaling = pool.get("autoscaling") or {}
    # Exactly one: an empty `locations` is an unknown zone span, as
    # `_pool_ceiling` reads it, not a pool locked to a zone.
    zone_locked = len(locations) == 1 and autoscaling.get("enabled") and not has_nap
    ceiling, basis = _pool_ceiling(autoscaling, locations)
    at_ceiling = bool(ceiling) and current_node_count >= NODEPOOL_CEILING_FRACTION * ceiling
    if not zone_locked and not at_ceiling:
        return None
    excerpts, impacts = [], []
    if zone_locked:
        excerpts.append(
            f"single-zone ({locations}), autoscaling enabled, no NAP and no regional multi-zone configuration"
        )
        impacts.append(_IMPACT_ZONE_LOCKED)
    if at_ceiling:
        # Name the arm and the zone span. This condition has nothing to do with
        # zones -- it fires on a regional pool spanning three of them -- but it
        # is published under a slug called `single-zone-nodepool`, and a bare
        # "9/10 live nodes" left the reader nothing to tell the two arms apart.
        # 3.9 keys its impact and its remediation off this prefix, so a run
        # stops proposing multi-zone node pools to an operator who has them.
        excerpts.append(
            f"at its autoscaling ceiling: {current_node_count}/{ceiling} live nodes "
            f"({current_node_count / ceiling * 100:.0f}% of {basis}), zones {locations}"
        )
        impacts.append(_ceiling_impact(ceiling, current_node_count, has_nap))
    return {
        "object": f"NodePool/{pool.get('name', '')}",
        "excerpt": "; ".join(excerpts),
        "impact": " ".join(impacts),
    }


# --------------------------------------------------------------------------- #
# 3.10 reservation-mismatch-risk
# --------------------------------------------------------------------------- #


def check_reservation(reservation: dict) -> dict | None:
    specific = reservation.get("specificReservation") or {}
    # `inUseCount` defaults to 0 rather than to "unknown": proto3 JSON omits a
    # zero int64, so the reservation nothing is consuming -- the one §3.10(c)
    # most wants -- is exactly the one that arrives without the field.
    count = _gce_int(specific.get("count"))
    in_use = _gce_int(specific.get("inUseCount"), 0)
    if count is None or in_use is None or count == 0:
        return None
    ratio = in_use / count
    if ratio <= RESERVATION_IDLE_RATIO and (count - in_use) >= RESERVATION_IDLE_MIN_INSTANCES:
        return {
            "object": f"Reservation/{reservation.get('name', '')}",
            "excerpt": f"inUseCount={in_use}/{count} ({ratio * 100:.0f}% used, {count - in_use} idle)",
            "severity": "major",
        }
    return None


def check_reservation_affinity(cc: dict) -> dict | None:
    priorities = (cc.get("spec") or {}).get("priorities") or []
    for p in priorities:
        affinity = ((p.get("reservations") or {}).get("affinity") or "")
        if affinity in ("AnyBestEffort", "Automatic"):
            return {
                "object": f"ComputeClass/{cc['metadata']['name']}",
                "excerpt": f"reservations.affinity={affinity!r} bypasses this ComputeClass's priority chain",
                "severity": "critical",
            }
    return None


# --------------------------------------------------------------------------- #
# 3.7 quota-exhaustion-risk
# --------------------------------------------------------------------------- #


def check_quota(quota: dict, region: str) -> dict | None:
    metric = str(quota.get("metric") or "")
    if not _CAPACITY_QUOTA_RE.search(metric) or metric.startswith(_COMMITMENT_QUOTA_PREFIX):
        return None
    limit, usage = quota.get("limit"), quota.get("usage", 0)
    if not isinstance(limit, (int, float)) or not isinstance(usage, (int, float)):
        return None
    if not limit:
        return None
    ratio = usage / limit
    if ratio >= QUOTA_EXHAUSTION_RATIO:
        # The region is in the object because the same metric is a separate
        # quota in every region, and without it two regions' `CPUS` shared one
        # finding id.
        return {"object": f"Quota/{region}:{metric}", "excerpt": f"{region}: {metric}: {usage}/{limit} ({ratio * 100:.0f}%)"}
    return None


# --------------------------------------------------------------------------- #
# §3.8 Spot capacity advice, §3.11 autoscaler visibility
# --------------------------------------------------------------------------- #


def autoscaler_message_ids(entries: object) -> dict[str, dict]:
    """§3.11's stockout message ids out of a `cluster-autoscaler-visibility` read.

    Both schemas, keyed by id, each carrying how many entries named it, the
    window it spanned, and whatever `parameters` the autoscaler attached — for
    `scale.up.error.out.of.resources` that is the instance group it could not
    grow, which is the one thing a reader needs to act on.
    """
    found: dict[str, dict] = {}
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        payload = entry.get("jsonPayload") or {}
        errors = [
            result.get("errorMsg") or {}
            for result in ((payload.get("resultInfo") or {}).get("results") or [])
            if isinstance(result, dict)
        ]
        no_scale_up = (payload.get("noDecisionStatus") or {}).get("noScaleUp") or {}
        for group in no_scale_up.get("unhandledPodGroups") or []:
            if isinstance(group, dict):
                errors += [
                    reason
                    for reason in (group.get("napFailureReasons") or [])
                    if isinstance(reason, dict)
                ]
        stamp = str(entry.get("timestamp") or "")
        for err in errors:
            message_id = err.get("messageId")
            if message_id not in AUTOSCALER_STOCKOUT_MESSAGE_IDS:
                continue
            slot = found.setdefault(
                message_id, {"count": 0, "parameters": [], "first_seen": "", "last_seen": ""}
            )
            slot["count"] += 1
            for param in err.get("parameters") or []:
                if str(param) not in slot["parameters"]:
                    slot["parameters"].append(str(param))
            if stamp:
                slot["first_seen"] = min(slot["first_seen"] or stamp, stamp)
                slot["last_seen"] = max(slot["last_seen"], stamp)
    return found


def check_autoscaler_out_of_resources(message_ids: dict[str, dict]) -> list[dict]:
    """One finding per distinct message id, not per log entry, and the id is
    the object: §3.11's remediation branches on it, and two ids on one cluster
    under one `Cluster/<name>` object shared a finding id.

    A cluster wedged against a regional stockout emits the same id every
    autoscaler tick, and the SOP's remediation branches on the id rather than
    on the occurrence — three hundred findings saying `out.of.resources` are
    one problem written three hundred times.
    """
    hits = []
    for message_id in sorted(message_ids):
        seen = message_ids[message_id]
        where = (
            seen["parameters"][0].rsplit("/", 1)[-1]
            if seen["parameters"]
            else "no instance group named"
        )
        # The window comes from the entries themselves rather than from
        # `AUTOSCALER_FRESHNESS`: this function does not issue the read and
        # cannot know what window it asked for, and a hardcoded "over the last
        # 24h" beside timestamps that say otherwise is worse than no claim.
        window = (
            f", {seen['first_seen'][:19]} .. {seen['last_seen'][:19]}"
            if seen["first_seen"]
            else ""
        )
        hits.append(
            {
                "object": f"ScaleUpError/{message_id}",
                "excerpt": (
                    f"{message_id}, {seen['count']} occurrence"
                    f"{'' if seen['count'] == 1 else 's'} in the autoscaler "
                    f"visibility log{window}; first affected: {where}"
                ),
            }
        )
    return hits


def spot_shapes(compute_classes: list[dict], node_pools: list[dict]) -> dict[str, dict]:
    """The concrete Spot machine types this cluster asks for, and who asks.

    `capacity-history` takes one `--machine-type` and nothing coarser, so a
    priority naming only `machineFamily` has no shape to query — those are
    counted here and reported as unmeasured rather than dropped.

    `families` is what §3.8's "without alternative family fallbacks" tests,
    kept per owner: a single-family node pool is not excused because some
    other ComputeClass asking for the same shape spans two families. A node
    pool has no fallback chain at all, so it carries 1 by construction: when
    its shape runs out, nothing else is tried.

    Owners §2 calls non-production are left out, since §3.8 does not flag
    them.
    """
    shapes: dict[str, dict] = {}

    def add(machine_type: str, owner: str, families: int) -> None:
        slot = shapes.setdefault(machine_type, {"owners": [], "families": {}})
        if owner not in slot["owners"]:
            slot["owners"].append(owner)
        slot["families"][owner] = max(slot["families"].get(owner, 0), families)

    for cc in compute_classes:
        meta = cc.get("metadata") or {}
        if is_non_production(meta.get("name", ""), meta.get("labels")):
            continue
        priorities = (cc.get("spec") or {}).get("priorities") or []
        families = len({_priority_family(p) for p in priorities if _priority_family(p)})
        owner = f"ComputeClass/{meta.get('name', '')}"
        for priority in priorities:
            if _priority_is_spot(priority) and priority.get("machineType"):
                add(str(priority["machineType"]), owner, families)
    for pool in node_pools:
        config = pool.get("config") or {}
        if is_non_production(pool.get("name", ""), {**(config.get("resourceLabels") or {}), **(config.get("labels") or {})}):
            continue
        if config.get("spot") and config.get("machineType"):
            add(str(config["machineType"]), f"NodePool/{pool.get('name', '')}", 1)
    return shapes


def spot_without_a_shape(
    compute_classes: list[dict], node_pools: list[dict], *, brokered: bool
) -> tuple[list[str], list[str], list[str]]:
    """Spot requests `capacity-history` cannot be asked about, split by why.

    `(unqueryable, unpinned, inert)`, and the split is the whole point. A
    priority naming a machine family but no machine type is a real gap: the
    shape exists, the API takes one `--machine-type` and has no way to be asked
    about a family. A priority pinning *neither* has no shape to be scarce,
    because every family is available to it; §3.8's "without alternative family
    fallbacks" cannot be true of it by construction.

    `brokered` is whether GKE places this cluster's capacity — Autopilot, or
    Standard with node auto-provisioning on — and so whether a shape-free Spot
    priority stands behind every scheduling decision or merely sits there
    unselected. It exists because the third bucket was for a long time folded
    into the second, on the belief that a pod-family Spot priority meant
    `autopilot-spot` and that `autopilot-spot` meant an Autopilot cluster. GKE
    pre-installs those classes on Standard clusters too, so on 2026-09-05 ten of
    this fleet's sixteen clusters published "Every Spot request on this cluster
    leaves the machine shape entirely to GKE" — about clusters that make no Spot
    request at all and hold no Spot node pool. The honest sentence, that nothing
    here asks for Spot, was the branch it displaced.

    It is not whether a node could be created: a ComputeClass carrying its own
    `nodePoolAutoCreation.enabled: true` provisions independently of the
    cluster-level flag, and the first attempt at this fix asserted otherwise in
    published prose. See the `inert` branch below for what the fleet did to
    disprove it.

    `unqueryable` belongs in `limitations` and the other two in
    `checks_not_applicable`, saying different things; reporting all three as
    "this cluster does not use Spot" was wrong about each.

    Owners §2 calls non-production are left out, as `spot_shapes` leaves them
    out: §3.8 does not flag them, so a gap in measuring them is no gap.
    """
    unqueryable, unpinned, inert = [], [], []
    for cc in compute_classes:
        meta = cc.get("metadata") or {}
        if is_non_production(meta.get("name", ""), meta.get("labels")):
            continue
        name = meta.get("name", "")
        for priority in (cc.get("spec") or {}).get("priorities") or []:
            if not _priority_is_spot(priority) or priority.get("machineType"):
                continue
            family = _priority_family(priority)
            if family:
                bucket, label = unqueryable, f"{name}:{family}"
            else:
                bucket = unpinned if brokered else inert
                label = f"ComputeClass/{name}"
            if label not in bucket:
                bucket.append(label)
    for pool in node_pools:
        config = pool.get("config") or {}
        if is_non_production(pool.get("name", ""), {**(config.get("resourceLabels") or {}), **(config.get("labels") or {})}):
            continue
        if config.get("spot") and not config.get("machineType"):
            unqueryable.append(f"NodePool/{pool.get('name', '')}")
    return unqueryable, unpinned, inert


def mean_preemption_rate(advice: object) -> tuple[float | None, int]:
    """The mean daily preemption rate and how many intervals it averages.

    The mean rather than the maximum, deliberately. §3.8 is about a shape being
    a bad bet, and one 60% afternoon inside a month of 5% is a zonal incident
    that already resolved — flagging on the peak turns every shape in the fleet
    critical after any bad day.
    """
    history = (advice or {}).get("preemptionHistory") or [] if isinstance(advice, dict) else []
    rates = [
        float(entry["preemptionRate"])
        for entry in history
        if isinstance(entry, dict) and isinstance(entry.get("preemptionRate"), (int, float))
    ]
    if not rates:
        return None, 0
    return sum(rates) / len(rates), len(rates)


def spot_list_price(advice: object) -> str:
    """The most recent list price, as a display string, or `""`.

    `units` is absent below one currency unit and `nanos` is absent above a
    whole one, so both halves are read. Context for the excerpt only — no
    check branches on it.
    """
    history = (advice or {}).get("priceHistory") or [] if isinstance(advice, dict) else []
    prices = [entry for entry in history if isinstance(entry, dict) and entry.get("listPrice")]
    if not prices:
        return ""
    price = prices[-1]["listPrice"]
    amount = float(price.get("units") or 0) + float(price.get("nanos") or 0) / 1e9
    return f"{amount:.4f} {price.get('currencyCode') or ''}".strip()


def check_spot_scarcity(
    machine_type: str, shape: dict, region: str, advice: object
) -> tuple[dict | None, str | None]:
    """§3.8 for one shape: `(finding, limitation)`, at most one of them set.

    The SOP's two halves are both required — a preemption rate above the
    ceiling *and* no alternative family to fall back to. A chain that already
    spans two families survives its worst shape being preempted, which is
    exactly what "Do NOT flag: comprehensive multi-family fallbacks" means.
    """
    rate, intervals = mean_preemption_rate(advice)
    owners = ", ".join(shape["owners"])
    # Sorted, so the finding's object (the first) is the same owner every run.
    exposed = sorted(o for o in shape["owners"] if shape["families"][o] < MIN_FALLBACK_FAMILIES)
    if rate is None:
        return None, (
            f"spot-scarcity-risk could not be measured for {machine_type} in "
            f"{region}: capacity-history returned no preemptionHistory "
            f"(requested by {owners})"
        )
    if intervals < SPOT_MIN_INTERVALS:
        return None, (
            f"spot-scarcity-risk for {machine_type} in {region} rests on "
            f"{intervals} daily interval(s), under the {SPOT_MIN_INTERVALS} this "
            f"check needs to mean anything (requested by {owners})"
        )
    if rate <= SPOT_PREEMPTION_CEILING or not exposed:
        return None, None
    price = spot_list_price(advice)
    families = shape["families"][exposed[0]]
    return {
        "object": exposed[0],
        "excerpt": (
            f"Spot {machine_type} in {region} preempted at a mean "
            f"{rate * 100:.1f}% per day over {intervals} days (ceiling "
            f"{SPOT_PREEMPTION_CEILING * 100:.0f}%), and {', '.join(exposed)} "
            f"name{'s' if len(exposed) == 1 else ''} {families} machine famil"
            f"{'y' if families == 1 else 'ies'} — no alternative to fall "
            f"back to" + (f"; list price {price}/h" if price else "")
        ),
    }, None


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #

IMPACT = {
    "ccc-missing-fallbacks": "Pinned to a single machine family or narrow configuration: any zonal capacity exhaustion causes scale-up to fail and leaves pods unschedulable.",
    "ccc-no-ondemand-floor": "If Spot VM capacity is preempted or exhausted in the region, the workload has no on-demand floor and remains permanently in Pending state.",
    "ccc-large-vm-scarcity": "Very large VM shapes (>32 cores) draw from thin regional capacity pools and are highly prone to sudden stockouts during scale-up.",
    "ccc-priority-starvation": "Excessive priority rules (>10) exceed the autoscaler solver cache limit, triggering backoff loops that starve lower priorities.",
    "ccc-mixed-disk-generations": "Stateful PV workload mixes Gen 2 and Gen 4 machine families, causing volume attachment failures and deadlocks when scaling across nodes.",
    "ccc-hyperdisk-incompatible": "Autoscaler fallback lands on an older machine family that does not support Hyperdisk, causing node provisioning or pod volume attachment to fail.",
    "quota-exhaustion-risk": f"A regional GCP capacity quota is at {QUOTA_EXHAUSTION_RATIO:.0%} or more of its limit; once it is reached, Cluster Autoscaler cannot provision additional nodes in that region even if physical capacity exists.",
    # Both arms of §3.9 set their own `impact` on the hit -- and a pool
    # matching both gets both sentences -- so this entry is the fallback
    # nothing reaches. It says only what every hit has in common: "cannot
    # scale when it needs to" was the previous wording and is false of a
    # zone-locked pool at 1 of 2 nodes, which scales fine until the zone
    # runs out.
    "single-zone-nodepool": "Node pool's scale-up has a single point of failure: it is locked to one zone, at its own configured ceiling, or both.",
    "reservation-mismatch-risk": "ComputeClass fallback priorities are rendered inert by Automatic reservation affinity, or expensive guaranteed reservation capacity sits idle during stockouts.",
    "dangling-compute-class": "Workload cannot be scheduled due to dangling class references, invalid CRD configuration, or missing node tolerations, causing permanent Pending state.",
    "spot-scarcity-risk": "Spot machine shapes have high historical preemption rates and severe obtainability constraints, putting workload uptime at extreme risk.",
    "autoscaler-out-of-resources": "Autoscaler has actively failed scale-up attempts due to physical cloud stockouts, quota exhaustion, or pod subnet IP exhaustion.",
}
SEVERITY = {
    "ccc-missing-fallbacks": "critical",
    "ccc-no-ondemand-floor": "major",  # overridden to critical when referenced by an inference workload
    "ccc-large-vm-scarcity": "major",
    "ccc-priority-starvation": "critical",
    "ccc-mixed-disk-generations": "critical",
    "ccc-hyperdisk-incompatible": "critical",
    "quota-exhaustion-risk": "critical",
    "single-zone-nodepool": "major",
    "reservation-mismatch-risk": "major",  # overridden to critical for a broken/bypassed binding
    "dangling-compute-class": "critical",
    "spot-scarcity-risk": "major",
    "autoscaler-out-of-resources": "critical",
}


def _emit(slug: str, hit: dict) -> dict:
    emitted = {
        "check": slug,
        "namespace": hit.get("namespace", ""),
        "object": hit["object"],
        "severity": hit.get("severity") or SEVERITY[slug],
        "excerpt": hit["excerpt"],
        # A check with two arms can supply the arm's own sentence, the way it
        # already supplies its own `severity`. `IMPACT[slug]` stays the default
        # for the checks that mean one thing.
        "impact": hit.get("impact") or IMPACT[slug],
        "needs_triage": None,
    }
    if hit.get("impact"):
        # Which arm fired is an observation the model cannot infer from the
        # excerpt reliably enough to bet a published sentence on it. See
        # `adopt_arm_impact` in audit_report.py.
        emitted["impact_authoritative"] = True
    if hit.get("command"):
        # The command that produced *this* candidate, for a check that issues
        # one per sub-target rather than one per cluster. The entry's
        # `commands` map holds a single record per slug, so §3.8's per-shape
        # `capacity-history` calls and 3.10's per-region `regions describe`
        # calls overwrite each other there and every finding inherits the last
        # one. `adopt_collector_evidence` prefers this field when it is set.
        emitted["command"] = hit["command"]
    return emitted


def crashed_entry(cluster: dict, exc: BaseException) -> dict:
    """A `clusters[]` entry for a worker that raised something unmodelled.

    `future.result()` re-raises, so one unhandled exception on one cluster
    aborts `collect_fleet` — and the SOP invokes this collector as
    `fleet_stockout.py … > manifest_stockout-prevention.json`, so by then the
    shell has already truncated the file. The run loses the whole fleet to one
    bad object instead of one cluster. `gate-failed` is the shape the document
    already carries for "enumerated, could not be read".
    """
    print(
        f"[fleet_stockout] {cluster.get('project', '?')}/{cluster.get('name', '?')}: "
        f"collector raised {type(exc).__name__}: {exc}",
        file=sys.stderr,
    )
    return {
        "name": target_name(cluster.get("project", ""), cluster.get("location", ""), cluster.get("name", "?")),
        "project": cluster.get("project", "?"),
        "location": cluster.get("location", "?"),
        "autopilot": bool(cluster.get("autopilot")),
        "has_nap": bool(cluster.get("has_nap")),
        "outcome": "gate-failed",
        "error": f"collector raised {type(exc).__name__}: {exc}"[:ERROR_EXCERPT_CHARS],
    }


def crashed_project_error(project: str, exc: BaseException) -> str:
    """`crashed_entry`'s account for a project read, as the error its
    `gate-failed` `project/<p>` target carries: one bad answer there must not
    abort the fleet either."""
    print(f"[fleet_stockout] project {project}: collector raised {type(exc).__name__}: {exc}", file=sys.stderr)
    return f"collector raised {type(exc).__name__}: {exc}"[:ERROR_EXCERPT_CHARS]


def collect_cluster(cluster: dict, *, run: RunFn) -> dict:
    name, project, location = cluster["name"], cluster["project"], cluster["location"]
    # `name` stays bare for the gcloud and logging reads; `target` is what the
    # manifest publishes.
    target = target_name(project, location, name)
    # Both are cluster properties `enumerate_clusters` already resolved, and
    # both ride on every shape below: neither stops being true because this
    # run failed to read inside the cluster.
    mode = {"autopilot": bool(cluster.get("autopilot")), "has_nap": bool(cluster.get("has_nap"))}
    kubeconfig, cred_run = fetch_credentials(project, name, location, run=run)
    if cred_run.rc != 0:
        return {"name": target, "project": project, "location": location, **mode, "outcome": "unreachable", "error": f"get-credentials rc={cred_run.rc}: {cred_run.stderr.strip()[:ERROR_EXCERPT_CHARS]}"}

    env = {**os.environ, "KUBECONFIG": str(kubeconfig)}
    dump_argv = ["kubectl", "get", "computeclasses,deployments,statefulsets,storageclasses,nodes", "-A", "-o", "json"]
    parsed, result = run_and_gate(dump_argv, run=run, env=env)
    if parsed is None:
        return {"name": target, "project": project, "location": location, **mode, "outcome": "gate-failed", "error": f"object dump gate failed (rc={result.rc}): {result.stderr.strip()[:ERROR_EXCERPT_CHARS]}"}
    if not isinstance(parsed, dict) or not isinstance(parsed.get("items"), list):
        # Parsed but not a List: read as `items: []` it would be a cluster with
        # nothing on it, audited clean.
        return {"name": target, "project": project, "location": location, **mode, "outcome": "gate-failed", "error": "object dump gate failed: the answer has no `items` list"}
    dump_record = _record(f"KUBECONFIG={kubeconfig} {shlex.join(dump_argv)}", result)

    items = parsed.get("items", [])
    all_compute_classes = [i for i in items if i.get("kind") == "ComputeClass"]
    # §2's standard exclusions, applied before any check reads an object. An
    # excluded workload also stops counting as a ComputeClass's inference or
    # stateful referrer. `compute_classes_by_name` keeps every class, because
    # whether a referenced class *exists* (3.12(a)) does not depend on it.
    compute_classes = [cc for cc in all_compute_classes if not standard_excluded(cc)]
    deployments = [i for i in items if i.get("kind") == "Deployment" and not standard_excluded(i)]
    statefulsets = [i for i in items if i.get("kind") == "StatefulSet" and not standard_excluded(i)]
    storage_classes = {i["metadata"]["name"]: i for i in items if i.get("kind") == "StorageClass"}
    workloads = deployments + statefulsets
    compute_classes_by_name = {cc["metadata"]["name"]: cc for cc in all_compute_classes}

    # §3.9's ">= 90% of maxNodeCount" test needs the pool's *live* node
    # count, which the GKE `NodePool` resource itself never exposes --
    # `initialNodeCount` is creation-time and the autoscaler never updates
    # it. Count live `Node` objects by the same nodepool label
    # `fleet_waste.py`'s idle-nodepool check already groups by.
    live_node_count_by_pool: dict[str, int] = {}
    for node in (i for i in items if i.get("kind") == "Node"):
        pool = (node.get("metadata", {}).get("labels") or {}).get("cloud.google.com/gke-nodepool", "")
        live_node_count_by_pool[pool] = live_node_count_by_pool.get(pool, 0) + 1

    autopilot = bool(cluster.get("autopilot"))
    # Not attempted on Autopilot: `node-pools list` answers HTTP 400 there
    # ("Autopilot node pools cannot be accessed or modified"), so the record
    # would be a guaranteed failure for a read whose only consumers -- §3.9's
    # single-zone-nodepool and the node pool labels below -- cannot apply to a
    # cluster with no user node pools anyway.
    #
    # `pools_readable` rather than testing `node_pools` for emptiness. A read
    # that failed and a Standard cluster that genuinely holds no pools both
    # produced `[]`, and the check downstream was skipped for either -- so a
    # denied read silently dropped `single-zone-nodepool` from the manifest
    # with nothing to say it had been attempted, which §6 reads as a check
    # nobody ran and cannot explain. They are different facts and they now
    # take different paths.
    node_pools: list[dict] = []
    pools_result = None
    pools_record: dict | None = None
    pools_readable = False
    if not autopilot:
        node_pools_argv = ["gcloud", "container", "node-pools", "list", "--cluster", name, "--location", location, "--project", project, "--format", "json"]
        pools_result = run(node_pools_argv)
        pools_readable = pools_result.rc == 0
        node_pools = json.loads(pools_result.stdout) if pools_readable and pools_result.stdout.strip() else []
        pools_record = _record(shlex.join(node_pools_argv), pools_result)
    has_nap = bool(cluster.get("has_nap"))
    # ComputeClass-managed pools carry the class name as this label, not as
    # their own pool name -- matching against pool names would test the
    # wrong field and never actually find the reference.
    node_pool_labels = (
        {v for p in node_pools for v in [((p.get("config") or {}).get("labels") or {}).get("cloud.google.com/compute-class")] if v}
        if pools_readable
        else None
    )

    candidates: list[dict] = []
    commands: dict[str, dict] = {}

    # A claim that names no class gets the cluster's default, and most charts
    # name none -- reading only an explicit `storageClassName` missed them.
    default_class = next(
        (n for n, sc in storage_classes.items() if ((sc.get("metadata") or {}).get("annotations") or {}).get(DEFAULT_STORAGE_CLASS_ANNOTATION) == "true"),
        None,
    )

    def claim_class(vct: dict) -> str | None:
        sc_name = (vct.get("spec") or {}).get("storageClassName")
        return default_class if sc_name is None else sc_name

    version = GKE_VERSION_RE.match(cluster.get("version") or "")
    dynamic_rwo_applies = bool(version) and tuple(int(g) for g in version.groups()) >= DYNAMIC_RWO_MIN_VERSION
    stateful_names_using_hyperdisk = set()
    for sts in statefulsets:
        for vct in sts.get("spec", {}).get("volumeClaimTemplates", []) or []:
            sc_name = claim_class(vct)
            provisioner = (storage_classes.get(sc_name) or {}).get("provisioner", "")
            params = (storage_classes.get(sc_name) or {}).get("parameters", {}) or {}
            if params.get("type") in HYPERDISK_TYPES or "hyperdisk" in provisioner.lower():
                stateful_names_using_hyperdisk.add((sts["metadata"].get("namespace", ""), sts["metadata"]["name"]))

    cc_referenced_by_stateful = set()
    cc_referenced_by_hyperdisk = set()
    for sts in statefulsets:
        cc_ref = ((sts.get("spec", {}).get("template", {}).get("spec", {}) or {}).get("nodeSelector") or {}).get("cloud.google.com/compute-class")
        if not cc_ref:
            continue
        # §3.5 flags "a stateful workload *using PersistentVolumes*" -- a
        # StatefulSet with no `volumeClaimTemplates` has nothing that can
        # deadlock on a machine-family mismatch, so it does not count here
        # even though it references the ComputeClass.
        # Nor does one whose every claim is on `dynamic-rwo` where the control
        # plane is new enough to honour it: §3.5's Do-NOT-flag.
        vcts = sts.get("spec", {}).get("volumeClaimTemplates") or []
        if vcts and not (dynamic_rwo_applies and all(claim_class(v) == DYNAMIC_RWO_CLASS for v in vcts)):
            cc_referenced_by_stateful.add(cc_ref)
        if (sts["metadata"].get("namespace", ""), sts["metadata"]["name"]) in stateful_names_using_hyperdisk:
            cc_referenced_by_hyperdisk.add(cc_ref)

    # ai-security-audit's inference discriminators, per §3.2: a serving image
    # or an accelerator request. Not `_is_ai_workload` whole -- it also counts
    # an AI-provider credential, which marks an app that *calls* a model, and a
    # web app calling one is not a Spot-preemption SLA breach.
    from collect import _is_inference_workload as _is_inference

    cc_referenced_by_inference = set()
    for workload in workloads:
        spec = workload.get("spec") or {}
        template_spec = ((spec.get("template") or {}).get("spec")) or spec
        cc_ref = (template_spec.get("nodeSelector") or {}).get("cloud.google.com/compute-class")
        if cc_ref and _is_inference(template_spec):
            cc_referenced_by_inference.add(cc_ref)

    # Recorded whether or not the dump held a ComputeClass or a StatefulSet.
    # The dump read both kinds, so "there are none" is this check's answer
    # rather than a check nobody ran -- the reasoning single-zone-nodepool
    # gives below for a cluster with no pools. Recording them only when the
    # inputs existed left every cluster without a ComputeClass partially
    # audited on every run, which keeps its findings from ever resolving.
    for cc_slug in (
        "ccc-missing-fallbacks", "ccc-no-ondemand-floor", "ccc-large-vm-scarcity", "ccc-priority-starvation",
        "ccc-mixed-disk-generations", "ccc-hyperdisk-incompatible", "reservation-mismatch-risk",
    ):
        commands[cc_slug] = dump_record
    for cc in compute_classes:
        cc_meta = cc.get("metadata") or {}
        # §3.2 and §3.10 do not flag non-production.
        non_production = is_non_production(cc_meta.get("name", ""), cc_meta.get("labels"))
        for hit in [check_ccc_missing_fallbacks(cc)]:
            if hit:
                candidates.append(_emit("ccc-missing-fallbacks", hit))
        if not non_production:
            for hit in [check_ccc_no_ondemand_floor(cc, cc_meta["name"] in cc_referenced_by_inference)]:
                if hit:
                    candidates.append(_emit("ccc-no-ondemand-floor", hit))
        candidates += [_emit("ccc-large-vm-scarcity", hit) for hit in check_ccc_large_vm_scarcity(cc)]
        for hit in [check_ccc_priority_starvation(cc)]:
            if hit:
                candidates.append(_emit("ccc-priority-starvation", hit))
        for hit in [check_ccc_mixed_disk_generations(cc, cc_meta["name"] in cc_referenced_by_stateful)]:
            if hit:
                # §3.5's fix differs on either side of DYNAMIC_RWO_MIN_VERSION.
                side = "at or past" if dynamic_rwo_applies else "before"
                plane = f"control plane {cluster['version']}, {side} {DYNAMIC_RWO_MIN_LABEL}" if version else "control plane version unknown"
                hit = {**hit, "excerpt": f"{hit['excerpt']} ({plane})"}
                candidates.append(_emit("ccc-mixed-disk-generations", hit))
        for hit in [check_ccc_hyperdisk_incompatible(cc, cc_meta["name"] in cc_referenced_by_hyperdisk)]:
            if hit:
                candidates.append(_emit("ccc-hyperdisk-incompatible", hit))
        if not non_production:
            for hit in [check_reservation_affinity(cc)]:
                if hit:
                    candidates.append(_emit("reservation-mismatch-risk", hit))

    commands["dangling-compute-class"] = dump_record
    for workload in workloads:
        for hit in [check_dangling_compute_class(workload, compute_classes_by_name, node_pool_labels)]:
            if hit:
                candidates.append(_emit("dangling-compute-class", hit))

    not_applicable: list[dict] = []
    limitations: list[str] = []
    # Checks whose read failed: neither run nor inapplicable. `finish` refuses
    # a document that claims one as either, which is the enforcement a
    # `limitations` sentence alone cannot give.
    unevaluated: dict[str, str] = {}
    if autopilot:
        # Declared by the collector rather than left to the model, for the
        # reason cross_check_manifest's note on `checks_not_applicable` gives:
        # until every collector says which checks it skipped and why, nothing
        # can adjudicate the field, and whether a run tells the truth about it
        # comes down to how well the model happens to know GKE. It is the same
        # disposition on every run, and `autopilot` is a fact already in hand.
        not_applicable.append(
            {
                "check": "single-zone-nodepool",
                "reason": (
                    "GKE Autopilot: Google places the nodes and exposes no user "
                    "node pool whose locations could be a single zone."
                ),
            }
        )
    elif pools_readable:
        # Recorded even when the cluster holds no pools. Zero pools is a real
        # answer to "is any pool single-zone" -- no -- and dropping the check
        # for it makes an empty Standard cluster indistinguishable from one
        # whose pools nobody looked at.
        commands["single-zone-nodepool"] = pools_record
        for pool in node_pools:
            live_count = live_node_count_by_pool.get(pool.get("name", ""), 0)
            for hit in [check_single_zone_nodepool(pool, has_nap, live_count)]:
                if hit:
                    candidates.append(_emit("single-zone-nodepool", hit))
    else:
        pools_failure = (
            f"`gcloud container node-pools list` failed (rc={pools_result.rc}) — "
            f"{pools_result.stderr.strip()[:STDERR_EXCERPT_CHARS] or 'no stderr'}"
        )
        unevaluated["single-zone-nodepool"] = pools_failure
        limitations.append(
            f"single-zone-nodepool could not be measured on this cluster: "
            f"{pools_failure}. The same failure left dangling-compute-class "
            f"without node pool labels, so its nodePoolAutoCreation arm did not "
            f"run either, and spot-scarcity-risk read no Spot node pool"
        )

    # §3.11. One read per cluster, and it is recorded whether or not it found
    # anything: "the autoscaler logged no stockout in 24h" is the answer this
    # check exists to give, and a clean cluster that never records the read is
    # indistinguishable from one nobody looked at.
    logging_argv = [
        "gcloud", "logging", "read",
        f'log_id("{AUTOSCALER_LOG_ID}") AND resource.labels.cluster_name="{name}" '
        f'AND resource.labels.location="{location}" '
        f"AND (jsonPayload.noDecisionStatus.noScaleUp:* OR jsonPayload.resultInfo.results.errorMsg:*)",
        "--project", project, "--freshness", AUTOSCALER_FRESHNESS,
        "--limit", str(AUTOSCALER_LOG_LIMIT), "--format", "json",
    ]
    entries, logging_result = run_and_gate(logging_argv, run=run)
    if logging_result.rc == 0:
        commands["autoscaler-out-of-resources"] = _record(shlex.join(logging_argv), logging_result)
        # `entries` is None for an empty result set as well as for unparseable
        # output, because gcloud prints nothing at all when nothing matched.
        # rc == 0 already told us the read succeeded, so an empty window is a
        # clean cluster rather than a gap.
        for hit in check_autoscaler_out_of_resources(autoscaler_message_ids(entries)):
            candidates.append(_emit("autoscaler-out-of-resources", hit))
        if isinstance(entries, list) and len(entries) >= AUTOSCALER_LOG_LIMIT:
            # gcloud returns newest first, so a full page drops the oldest
            # entries of the window, and a stockout among them is unseen.
            limitations.append(
                f"autoscaler-out-of-resources read the newest {AUTOSCALER_LOG_LIMIT} "
                f"visibility-log entries, the read's limit; older entries in the "
                f"{AUTOSCALER_FRESHNESS} window were not read"
            )
    else:
        logging_failure = (
            f"`gcloud logging read` failed (rc={logging_result.rc}) — "
            f"{logging_result.stderr.strip()[:STDERR_EXCERPT_CHARS] or 'no stderr'}"
        )
        unevaluated["autoscaler-out-of-resources"] = logging_failure
        limitations.append(f"autoscaler-out-of-resources could not be measured on this cluster: {logging_failure}")

    # §3.8. `capacity-history` takes one machine type per call, so the cost is
    # one read per distinct Spot shape rather than one per cluster. Ordered so
    # the ceiling, when it bites, drops the same shapes on every run instead of
    # whichever ones a dict happened to yield first.
    shapes = spot_shapes(compute_classes, node_pools if pools_readable else [])
    region = region_of(location)
    spot_hits: dict[str, dict] = {}
    for machine_type in sorted(shapes)[:SPOT_MAX_SHAPES]:
        advice_argv = [
            "gcloud", "beta", "compute", "advice", "capacity-history",
            "--region", region, "--machine-type", machine_type,
            "--provisioning-model", "SPOT", "--types", "PREEMPTION,PRICE",
            "--project", project, "--format", "json",
        ]
        advice, advice_result = run_and_gate(advice_argv, run=run)
        if advice_result.rc != 0:
            limitations.append(
                f"spot-scarcity-risk could not be measured for {machine_type} in "
                f"{region}: `gcloud beta compute advice capacity-history` failed "
                f"(rc={advice_result.rc}) — {advice_result.stderr.strip()[:STDERR_EXCERPT_CHARS] or 'no stderr'}"
            )
            continue
        commands["spot-scarcity-risk"] = _record(shlex.join(advice_argv), advice_result)
        # A list of one, on every response seen so far. Unwrapped here rather
        # than in the helpers so they take the shape the API documents.
        first = advice[0] if isinstance(advice, list) and advice else advice
        hit, limitation = check_spot_scarcity(machine_type, shapes[machine_type], region, first)
        if hit:
            hit["command"] = shlex.join(advice_argv)
            # One candidate per object: two hot shapes in one ComputeClass
            # would otherwise share a finding id. The first shape's command
            # stays as the evidence; the excerpt names both.
            if hit["object"] in spot_hits:
                spot_hits[hit["object"]]["excerpt"] += f"; {hit['excerpt']}"
            else:
                spot_hits[hit["object"]] = hit
        if limitation:
            limitations.append(limitation)
    candidates += [_emit("spot-scarcity-risk", hit) for hit in spot_hits.values()]
    if shapes and "spot-scarcity-risk" not in commands:
        unevaluated["spot-scarcity-risk"] = "every capacity-history read for this cluster's Spot shapes failed"
    if len(shapes) > SPOT_MAX_SHAPES:
        limitations.append(
            f"spot-scarcity-risk read {SPOT_MAX_SHAPES} of this cluster's "
            f"{len(shapes)} distinct Spot machine shapes; the rest were not "
            f"measured: {', '.join(sorted(shapes)[SPOT_MAX_SHAPES:])}"
        )
    unqueryable, unpinned, inert = spot_without_a_shape(
        compute_classes,
        node_pools if pools_readable else [],
        brokered=autopilot or bool(cluster.get("has_nap")),
    )
    if unqueryable:
        limitations.append(
            f"spot-scarcity-risk could not be measured for Spot requests that "
            f"name no machine type, which `capacity-history` has no way to "
            f"query: {', '.join(unqueryable)}"
        )
    if not shapes and not autopilot and not pools_readable:
        # The node pools were not read, so "nothing here requests Spot" is
        # not something this run knows -- whether or not a ComputeClass also
        # named a family-only Spot request, which says nothing about the pools.
        unevaluated["spot-scarcity-risk"] = (
            "no ComputeClass names a Spot machine type and the node pools could "
            "not be read, so whether a Spot node pool exists is unknown"
        )
    elif not shapes and unqueryable:
        # Spot is requested, by family only, so the check applies and was not
        # measured. Filed like the branch above, because the limitation alone
        # left the slug in no bucket `finish` checks, and a document declaring
        # it not applicable published complete.
        unevaluated["spot-scarcity-risk"] = (
            f"every Spot request on this cluster names a machine family but no "
            f"machine type, which `capacity-history` cannot query: {', '.join(unqueryable)}"
        )
    elif not shapes:
        # No command to record, so §6 would otherwise read the missing record as
        # a check nobody ran. Declared not-applicable for the same reason the
        # Autopilot branch above declares one: it is a fact already in hand.
        if unpinned:
            reason = (
                f"Every Spot request on this cluster leaves the machine shape "
                f"entirely to GKE ({', '.join(unpinned)}), so no shape can be "
                f"scarce for it — every family is available to it, which is "
                f"what this check tests for."
            )
        elif inert:
            # Says nothing about whether the class could provision, because it
            # can: a ComputeClass's own `nodePoolAutoCreation.enabled: true`
            # drives node creation independently of the cluster-level
            # auto-provisioning flag. An earlier version of this string
            # asserted the opposite -- "a Standard cluster with
            # auto-provisioning off, so no node can be created through it" --
            # and the fleet disproves it: `spot-capacity-test` has NAP off,
            # and on 2026-09-05 the GKE service agent still created
            # `nap-e2-standard-2-spot-rbu9q0zw` there to place a pod that
            # selected `autopilot-spot`. The check does not need the claim
            # either way; what makes it inapplicable is that no shape was
            # named, so keep the reason to what was actually read. That rules
            # out "nothing requests Spot" and "GKE's pre-installed" as well:
            # neither the workloads nor the class's origin were read, and a
            # hand-authored pod-family Spot class lands here too.
            reason = (
                f"The only Spot priority on this cluster is in {', '.join(inert)}, "
                f"which names no machine family or machine type, so there is no "
                f"shape to ask `capacity-history` about."
            )
        else:
            reason = (
                "No ComputeClass priority or node pool on this cluster requests "
                "Spot capacity, so there is no Spot machine shape to ask "
                "`capacity-history` about."
            )
        not_applicable.append({"check": "spot-scarcity-risk", "reason": reason})

    entry = {
        "name": target, "project": project, "location": location, **mode,
        "outcome": "collected",
        "commands": [{"check": slug, **record} for slug, record in commands.items()],
        "candidates": candidates,
    }
    if not_applicable:
        entry["checks_not_applicable"] = not_applicable
    if unevaluated:
        entry["checks_unevaluated"] = [
            {"check": slug, "reason": reason} for slug, reason in sorted(unevaluated.items())
        ]
    if limitations:
        entry["limitations"] = "; ".join(limitations)
    return entry


def collect_project(project: str, cluster_regions: set[str], *, run: RunFn) -> dict | None:
    res_argv = ["gcloud", "compute", "reservations", "list", "--project", project, "--format", "json"]
    reservations, res_result = run_and_gate(res_argv, run=run)
    if not cluster_regions and reservations is None and _api_disabled(res_result) and refusal_names_project(project, res_result.stderr, run=run):
        # No Compute Engine and no cluster: no reservation or regional quota
        # can exist here, so there is no target. A row reporting the failed
        # read would make every such project a coverage gap.
        log(f"{project}: Compute Engine API is not enabled; no project-scoped check applies")
        return None

    quota_records: dict[str, dict] = {}
    quota_candidates: list[dict] = []
    failed_regions: list[str] = []
    # Sorted so the recorded command, and the order of any limitation, is the
    # same on every run.
    for region in sorted(cluster_regions):
        q_argv = ["gcloud", "compute", "regions", "describe", region, "--project", project, "--format", "json(quotas)"]
        parsed, result = run_and_gate(q_argv, run=run)
        if not isinstance(parsed, dict):
            failed_regions.append(
                f"{region} (rc={result.rc}: {result.stderr.strip()[:STDERR_EXCERPT_CHARS] or 'no parseable output'})"
            )
            continue
        quota_records[region] = _record(shlex.join(q_argv), result)
        for quota in parsed.get("quotas") or []:
            for hit in [check_quota(quota, region)]:
                if hit:
                    hit["command"] = shlex.join(q_argv)
                    quota_candidates.append(_emit("quota-exhaustion-risk", hit))

    name = f"{PROJECT_TARGET_PREFIX}{project}"
    unevaluated: dict[str, str] = {}
    limitations: list[str] = []
    if reservations is None:
        reservations_failure = (
            f"`gcloud compute reservations list` failed (rc={res_result.rc}) — "
            f"{res_result.stderr.strip()[:STDERR_EXCERPT_CHARS] or 'no parseable output'}"
        )
        unevaluated["reservation-mismatch-risk"] = reservations_failure
        limitations.append(f"reservation-mismatch-risk's idle-capacity form could not be measured: {reservations_failure}")
    if failed_regions and not quota_records:
        unevaluated["quota-exhaustion-risk"] = f"every regional quota read failed: {', '.join(failed_regions)}"
    if failed_regions:
        # A region that failed is a region nobody checked. Recording the check
        # as run on the strength of the regions that answered, with nothing
        # naming the one that did not, published an all-clear for it.
        limitations.append(f"quota-exhaustion-risk could not read these regions: {', '.join(failed_regions)}")

    commands = []
    candidates = []
    if reservations is not None:
        commands.append({"check": "reservation-mismatch-risk", **_record(shlex.join(res_argv), res_result)})
        for reservation in reservations if isinstance(reservations, list) else []:
            # §3.10 does not flag non-production.
            if is_non_production(reservation.get("name", ""), reservation.get("resourceLabels")):
                continue
            for hit in [check_reservation(reservation)]:
                if hit:
                    candidates.append(_emit("reservation-mismatch-risk", hit))
    if quota_records:
        commands.append({"check": "quota-exhaustion-risk", **next(iter(quota_records.values()))})
        candidates.extend(quota_candidates)

    entry = {
        "name": name,
        "project": project,
        "location": "global",
        "outcome": "collected",
        "commands": commands,
        "candidates": candidates,
    }
    if not cluster_regions:
        # The quota reads are per cluster region, so with no cluster there
        # is none to make. Declared, because a check that neither ran nor
        # was declared counts as a coverage gap and pins every run partial.
        entry["checks_not_applicable"] = [
            {"check": "quota-exhaustion-risk", "reason": NO_CLUSTER_QUOTA_REASON.format(project=project)}
        ]
    if unevaluated:
        entry["checks_unevaluated"] = [
            {"check": slug, "reason": reason} for slug, reason in sorted(unevaluated.items())
        ]
    if limitations:
        entry["limitations"] = "; ".join(limitations)
    return entry


def _before(deadline: float) -> bool:
    return time.monotonic() < deadline


def collect_fleet(project: str | None = None, *, run: RunFn = default_run, max_workers: int = MAX_WORKERS, project_budget_s: float = PROJECT_READ_DEADLINE_S) -> dict:
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # The clock starts before discovery, as `fleet_waste.py`'s does: a slow
    # `projects list` spends the same terminal timeout the reads do.
    deadline = time.monotonic() + project_budget_s

    def failed(error: str) -> dict:
        # The manifest contract's top-level `error`: a run that enumerated
        # nothing says so rather than emitting an empty `clusters` array,
        # which reads as an empty fleet. `main` exits non-zero on it.
        return {
            "version": MANIFEST_VERSION,
            "checks_revision": CHECKS_REVISION,
            "audit": "stockout-prevention",
            "started_at": started_at,
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "error": error,
            "clusters": [],
        }

    try:
        projects, partial_discovery = get_target_projects(project, run=run)
    except NoProjectInScope as exc:
        return failed(str(exc))

    # One `clusters list` per project, in the pool: discovery names every
    # project the credential sees, which on an organisation-wide one is
    # hundreds, and one at a time that is minutes before a cluster is read.
    deadline_error = lambda p: PROJECT_DEADLINE_ERROR.format(budget=int(project_budget_s), project=p)

    def enumerate_or_error(p: str) -> tuple[list[dict], list[dict], str | None] | str:
        if not _before(deadline):
            return deadline_error(p)
        try:
            return (*enumerate_clusters(p, run=run), None)
        except IncompleteEnumeration as exc:
            # The clusters that arrived are audited; the project's own reads
            # are not, because a reservation a silent zone's cluster consumes
            # would read as unused.
            return exc.running, exc.not_running, str(exc)[:ERROR_EXCERPT_CHARS]
        except RuntimeError as exc:
            log(f"{p}: cluster enumeration failed, no clusters known from this project: {exc}")
            return str(exc)[:ERROR_EXCERPT_CHARS]
        except Exception as exc:  # noqa: BLE001 — see crashed_project_error
            return crashed_project_error(p, exc)

    enumerated: dict[str, tuple[list[dict], list[dict], str | None] | str] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(len(projects), max_workers))) as pool:
        # Started in a fresh order each run, so a deadline that cuts the list
        # short leaves a different tail unread each week rather than the same
        # projects forever.
        futures = {pool.submit(enumerate_or_error, p): p for p in random.sample(projects, len(projects))}
        for future in as_completed(futures):
            enumerated[futures[future]] = future.result()
    enumeration_failed = {p: r for p, r in enumerated.items() if isinstance(r, str)}
    if len(enumeration_failed) == len(projects):
        # Nothing was read anywhere, which is the same run a single failed
        # project used to be. One project quoted, the rest counted: a
        # credential that expired across two hundred projects would
        # otherwise produce an error nobody can read as a one-line summary.
        first = projects[0]
        return failed(f"{len(projects)} project(s) could not be listed or were not reached; first, {first}: {enumeration_failed[first]}")

    clusters: list[dict] = []
    not_running: list[dict] = []
    for p in projects:
        if p in enumeration_failed:
            continue
        clusters.extend(enumerated[p][0])
        not_running.extend(enumerated[p][1])
        if enumerated[p][2]:
            enumeration_failed[p] = enumerated[p][2]
    readable = [p for p in projects if p not in enumeration_failed]

    # A quota or a reservation belongs to a project rather than to a cluster,
    # and a project with no cluster can still hold a reservation nobody uses
    # (§3.10), so every readable project gets its project-scoped reads. They
    # need nothing the cluster reads produce, so they share the cluster pool
    # and are submitted first, while the deadline still admits them.
    def project_or_skip(p: str) -> tuple[dict | None] | str | None:
        if not _before(deadline):
            return None
        # Both halves: a DEGRADED or PROVISIONING cluster is not audited, but
        # it still sits in a region whose quota it draws on, and a project
        # whose only cluster is not running does not hold "no cluster".
        regions = {region_of(c["location"]) for c in (*enumerated[p][0], *enumerated[p][1]) if c.get("location")}
        try:
            return (collect_project(p, regions, run=run),)
        except Exception as exc:  # noqa: BLE001 — see crashed_project_error
            return crashed_project_error(p, exc)

    cluster_entries = [None] * len(clusters)
    project_reads: dict[str, tuple[dict | None] | str | None] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        project_futures = {pool.submit(project_or_skip, p): p for p in random.sample(readable, len(readable))}
        futures = {pool.submit(collect_cluster, c, run=run): i for i, c in enumerate(clusters)}
        for future in as_completed(project_futures):
            project_reads[project_futures[future]] = future.result()
        for future in as_completed(futures):
            index = futures[future]
            try:
                cluster_entries[index] = future.result()
            except Exception as exc:  # noqa: BLE001 — see crashed_entry
                cluster_entries[index] = crashed_entry(clusters[index], exc)

    project_entries: list[dict] = []
    for p in readable:
        read = project_reads.get(p)
        if read is None:
            enumeration_failed[p] = deadline_error(p)
        elif isinstance(read, str):
            enumeration_failed[p] = read
        elif read[0]:
            if not enumerated[p][0] and not enumerated[p][1]:
                read[0][CLUSTERS_LISTED_KEY] = 0
            project_entries.append(read[0])

    failed_entries = [
        {"name": f"{PROJECT_TARGET_PREFIX}{p}", "project": p, "location": "global", "outcome": "gate-failed", "error": enumeration_failed[p]}
        for p in projects
        if p in enumeration_failed
    ]
    discovery_entries = (
        [{"name": UNENUMERATED_PROJECTS_TARGET, "project": "", "location": "global", "outcome": "gate-failed", "error": partial_discovery[:ERROR_EXCERPT_CHARS]}]
        if partial_discovery
        else []
    )

    read_clusters = [e for e in cluster_entries if e]
    entries = read_clusters + project_entries + failed_entries + not_running + discovery_entries
    if not read_clusters and not project_entries:
        # Every target left is one §2 sends straight to `scope.skipped` -- an
        # unlisted or unreached project, a cluster not running -- and `finish`
        # rejects an empty `scope.clusters`, so this is the top-level `error`
        # rather than a manifest nothing can be built from.
        # The `--project` note is an error only in form: it says what this run
        # did not look at, never why the project it did look at yielded nothing.
        first = next(
            (e for e in entries if e.get("error") and e.get("name") != UNENUMERATED_PROJECTS_TARGET), None
        )
        return failed(NOTHING_COLLECTED_ERROR.format(count=len(projects), first=f"{first['name']}: {first['error']}" if first else "no project yielded any target"))

    return {
        "version": MANIFEST_VERSION,
        "checks_revision": CHECKS_REVISION,
        "audit": "stockout-prevention",
        "started_at": started_at,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "clusters": entries,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", help="audit only this project; omit to discover every project the credential can see")
    args = parser.parse_args(argv)
    manifest = collect_fleet(args.project)
    print(json.dumps(manifest, indent=2))
    return 1 if manifest.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
