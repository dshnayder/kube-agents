#!/usr/bin/env python3
"""Generate the Meridian Financial fleet knowledge base for the memory scale test.

The scenario
------------
Meridian Financial Services runs 500 GKE clusters across 12 regions for ~40
product teams, under PCI-DSS and SOC2. The platform team is 15 people. What
they know after two years of operating that fleet is *not* 500 cluster facts.
It is a knowledge base with a very particular shape, and that shape is the
whole reason this corpus is not just "one memory per cluster":

  * ~450 clusters are boring. They follow the golden path and the knowledge
    about them is one blueprint document, not 450 documents.
  * ~55 clusters are exceptions, and each of those has a story.
  * The bulk of what actually matters belongs to no single cluster at all:
    incidents, runbooks, policies, ownership, deprecations, cost.

Nested rungs
------------
The ladder is 50 / 150 / 400 / 800 / 1400 documents and each rung is a strict
superset of the one below. Every document the query set targets (a "gold" doc)
is present at rung 50 and never moves. Only the haystack grows.

That is the load-bearing design decision here. If gold documents arrived with
later rungs, recall would fall simply because the answer was not in the corpus
yet, and the experiment would measure nothing. Holding signal constant while
growing noise is what makes the per-rung numbers comparable — the only thing
that changes across the ladder is how much there is to be wrong about.

Deliberate defects
------------------
Three things are wrong with this corpus on purpose, because they are where the
two architectures diverge rather than merely differ in degree:

  * Supersession chains. Six policies are stated, revised, then replaced, with
    dates and explicit "supersedes" language. Neither provider has a retraction
    primitive, so both hold all three versions. Which one they answer with is
    the finding.
  * Cross-category overlap. An incident is described in its postmortem, cited
    in a runbook, and used as the rationale in an ADR. Consolidation either
    correctly merges these or destroys the distinctions.
  * Near-duplicate clusters. Pairs differing in exactly one attribute, so a
    plausible-looking wrong answer is available for every right one.

Outputs
-------
  corpus/<category>.md   documents with scope/id directives the seeder parses
  queries.json           the answer key: probes, gold docs, required substrings
  manifest.json          per-document rung assignment, sizes, category counts
"""

import argparse
import json
import random
import re
from pathlib import Path

SEED = 20260731
# The first rung must hold every probe target, and the probe set needs 68 gold
# documents to cover eight query classes honestly. 100 is the next round number
# above that, and the rest of the ladder roughly doubles from there.
RUNGS = [100, 200, 400, 800, 1400]

# --------------------------------------------------------------------------
# Fleet vocabulary
# --------------------------------------------------------------------------

REGIONS = [
    ("us-east4", "use4"), ("us-central1", "usc1"), ("us-west2", "usw2"),
    ("europe-west1", "euw1"), ("europe-west4", "euw4"), ("europe-west3", "euw3"),
    ("asia-southeast1", "ase1"), ("asia-northeast1", "ane1"), ("asia-south1", "asi1"),
    ("australia-southeast1", "aus1"), ("southamerica-east1", "sae1"), ("northamerica-northeast1", "nane1"),
]

TEAMS = [
    "payments-core", "payments-rails", "card-issuing", "card-auth", "ledger",
    "settlement", "fraud-detection", "risk-scoring", "kyc-onboarding", "identity",
    "retail-web", "retail-mobile", "business-banking", "treasury", "lending",
    "mortgage", "collections", "statements", "notifications", "customer-support",
    "data-platform", "streaming", "reporting", "regulatory-reporting", "ml-platform",
    "feature-store", "search", "personalisation", "pricing", "rewards",
    "partner-api", "open-banking", "developer-portal", "internal-tools", "sre-core",
    "platform-networking", "platform-security", "platform-observability", "release-engineering", "disaster-recovery",
]

SERVICES = [
    "payments-api", "payment-router", "card-authoriser", "ledger-writer", "ledger-reader",
    "settlement-batch", "fraud-scorer", "risk-engine", "kyc-verifier", "identity-broker",
    "session-store", "web-bff", "mobile-bff", "account-service", "transfer-service",
    "loan-originator", "mortgage-calculator", "dunning-worker", "statement-renderer",
    "notification-dispatcher", "email-gateway", "sms-gateway", "push-relay",
    "event-bus", "cdc-connector", "stream-aggregator", "report-builder", "regbatch-runner",
    "model-server", "feature-materialiser", "vector-index", "search-api", "recommender",
    "price-oracle", "rewards-ledger", "partner-gateway", "openbanking-adapter",
    "consent-manager", "audit-sink", "config-service",
]

ENVS = ["prod", "stg", "dev", "sbx"]

USERS = [f"user{n:02d}" for n in range(1, 51)]

CITIES = [
    "Dublin", "London", "Frankfurt", "Warsaw", "Bengaluru", "Singapore",
    "Sydney", "Toronto", "New York", "Austin", "São Paulo", "Tokyo",
]

# --------------------------------------------------------------------------

rng = random.Random(SEED)
DOCS = []
QUERIES = []
_seen_ids = set()


def add(doc_id, category, text, scope="shared", gold=False, title=""):
    """Register one document. Gold documents are pinned into the first rung."""
    if doc_id in _seen_ids:
        raise ValueError(f"duplicate document id {doc_id}")
    _seen_ids.add(doc_id)
    DOCS.append({
        "id": doc_id,
        "category": category,
        "scope": scope,
        "title": title,
        # Always collapse. These bodies are triple-quoted source literals, so
        # without this every document ships the generator's own indentation into
        # the extractor's input.
        "text": " ".join(text.split()),
        "gold": gold,
    })
    return doc_id


def ask(qid, qclass, query, gold_docs, must_contain, must_not_contain=(), note="",
        as_user=None, scored_at="context"):
    """Register one probe.

    `scored_at` declares which layer can honestly evaluate it:

    "context" — decidable from what the provider puts in front of the model. A
        superseded value either is or is not present; no judgement needed.
    "answer"  — only decidable from what the model actually said. The negative
        probes are the whole of this set: "did it invent a cluster?" is a
        property of the reply, not of the retrieved text. Scoring them
        lexically against a whole-corpus context reports the base rate of
        common English ("running", "Decision:") and calls it hallucination.
    """
    QUERIES.append({
        "id": qid,
        "class": qclass,
        "query": query,
        "gold_docs": list(gold_docs),
        "must_contain": list(must_contain),
        "must_not_contain": list(must_not_contain),
        "as_user": as_user,
        "scored_at": scored_at,
        "note": note,
    })


def cname(env, region_short, n):
    return f"mfs-{env}-{region_short}-{n:02d}"


# ==========================================================================
# 1. Fleet conventions — the blueprint that covers the boring 450
# ==========================================================================

def gen_conventions():
    add("CONV-001", "convention", """
        Fleet naming convention. Every Meridian cluster provisioned after the Atlas
        migration is named mfs-<env>-<region-short>-<nn>, where env is one of prod,
        stg, dev or sbx, region-short is the compressed GCP region code (us-east4 is
        use4, europe-west1 is euw1), and nn is a two-digit ordinal within that
        env/region pair. Clusters that predate the convention keep their legacy names
        and are listed individually in the exceptions register; do not attempt to
        derive a legacy cluster's region from its name, because for several of them
        the name is actively misleading.
        """, gold=True, title="Fleet naming convention")

    add("CONV-002", "convention", """
        Golden path cluster blueprint. A standard Meridian cluster is a regional GKE
        cluster on the stable release channel, private nodes, no public endpoint,
        Workload Identity Federation enabled, Dataplane V2, three node pools
        (system-pool n2-standard-4 with three nodes, general-pool n2-standard-8
        autoscaling 3-40, and burst-pool n2d-standard-8 spot autoscaling 0-100),
        Backup for GKE with a daily plan, Config Sync pointed at the fleet-config
        repository, and Managed Prometheus. Roughly 450 of the 500 clusters match
        this blueprint exactly. If you need to know how a cluster is configured and
        it is not in the exceptions register, this is the answer.
        """, gold=True, title="Golden path blueprint")

    add("CONV-003", "convention", """
        Namespace convention. Each tenant team gets one namespace per service per
        cluster, named <team>-<service>, with a ResourceQuota and a default
        LimitRange applied by Config Sync. The only namespaces exempt from the quota
        are kube-system, gmp-system, config-management-system and the platform team's
        own platform-ops namespace. Requests to raise a quota go through the capacity
        register, not through a direct kubectl edit, because Config Sync will revert
        a manual edit within four minutes and the revert looks like an unrelated
        outage to the team that made it.
        """, gold=True, title="Namespace convention")

    add("CONV-004", "convention", """
        Environment promotion. Changes flow sbx to dev to stg to prod, and each hop
        requires the previous environment to have run the change for at least the
        soak period: two hours for sbx to dev, twenty-four hours for dev to stg, and
        seventy-two hours for stg to prod. The soak period is waived only for a
        Sev-1 mitigation with an incident commander's approval recorded in the
        incident channel.
        """, title="Environment promotion")

    add("CONV-005", "convention", """
        Release channels by environment. Production clusters run the GKE stable
        channel. Staging runs regular. Dev and sandbox run rapid, deliberately, so
        that the platform team sees breaking Kubernetes changes about ten weeks
        before they can reach production. Six production clusters are pinned to the
        extended channel instead of stable; every one of them is an exception with a
        documented reason and an expiry date.
        """, gold=True, title="Release channels")

    add("CONV-006", "convention", """
        Ownership model. Every namespace has exactly one owning team recorded in the
        fleet-config repository, and every team has exactly one escalation rota in
        PagerDuty. The platform team owns the cluster, the node pools, the CNI, the
        service mesh and the ingress layer. The tenant team owns everything inside
        its namespace. There is no shared-ownership category, deliberately: when the
        Atlas migration allowed one, forty percent of incidents spent their first
        twenty minutes deciding whose problem it was.
        """, gold=True, title="Ownership model")

    add("CONV-007", "convention", """
        Change freeze calendar. Meridian freezes production changes from the 27th of
        each month to the 2nd of the following month for month-end settlement, for
        the full week around each quarterly regulatory filing, and from 15 December
        to 4 January. Emergency changes during a freeze require a director-level
        approval and are reviewed at the next change advisory board.
        """, gold=True, title="Change freeze calendar")

    add("CONV-008", "convention", """
        Image provenance. Every image running in a Meridian production cluster must
        be built by the central pipeline, signed with cosign, and admitted by Binary
        Authorization against the meridian-prod attestor. Images pulled directly from
        Docker Hub or ghcr.io are rejected at admission in prod and stg, allowed with
        a warning in dev, and unrestricted in sbx.
        """, title="Image provenance")

    rest = [
        ("Label taxonomy", """Every workload carries the labels meridian.io/team,
         meridian.io/service, meridian.io/env, meridian.io/cost-centre and
         meridian.io/data-class. Cost allocation, network policy targeting and the
         quarterly access review all key off these five labels, so a workload
         missing any of them is rejected by the Gatekeeper policy in prod and stg."""),
        ("Network policy default", """Every tenant namespace is default-deny for
         ingress and egress. Traffic is opened by named NetworkPolicy resources
         committed to fleet-config. Egress to the internet goes through the shared
         egress gateway, never directly, so that the outbound IP set stays small
         enough for partners to allowlist."""),
        ("Secret management", """Secrets come from Secret Manager through the CSI
         driver, mounted as files. Kubernetes Secret objects are permitted only for
         image pull credentials and for the small number of controllers that cannot
         read a mounted file. Secrets are never committed to fleet-config, and the
         pre-commit hook scans for them."""),
        ("Ingress topology", """External traffic enters through a global external
         Application Load Balancer, terminates TLS at the edge, and is routed to
         regional backends by the Gateway API. Internal service-to-service traffic
         uses Cloud Service Mesh with mTLS in STRICT mode. There is no path by which
         a tenant workload can be exposed publicly without a Gateway resource
         reviewed by platform-networking."""),
        ("Autoscaling defaults", """Horizontal Pod Autoscaler targets 65% CPU with a
         three-minute stabilisation window on scale-down and none on scale-up.
         Cluster autoscaler uses the balanced profile. Vertical Pod Autoscaler runs
         in recommendation mode only; nothing in the fleet applies VPA
         recommendations automatically, because doing so once caused a rolling
         restart of every pod in a namespace during a settlement window."""),
        ("PodDisruptionBudget policy", """Every production Deployment must declare a
         PodDisruptionBudget with maxUnavailable no greater than 25%. Node pool
         upgrades respect PDBs and will stall rather than violate one, so a PDB of
         maxUnavailable: 0 blocks fleet maintenance indefinitely and is rejected at
         admission."""),
        ("Backup policy", """Every production cluster has a daily Backup for GKE plan
         retaining thirty days, with all namespaces and volume data included.
         Clusters holding cardholder data additionally replicate backups to a second
         region. Restore drills run quarterly against a scratch cluster; a plan that
         has never been restored from is treated as a plan that does not work."""),
        ("Cost allocation", """Cluster cost is allocated to teams monthly by the
         meridian.io/cost-centre label, using GKE cost allocation with idle capacity
         charged back to the platform team rather than to tenants. This is deliberate:
         charging tenants for idle headroom made them size their requests to the
         floor, which cost more in incidents than it saved in compute."""),
        ("Access model", """Human access to production clusters is read-only by
         default through a group binding. Write access is granted just-in-time for
         four hours through the access broker, requires a ticket reference, and is
         logged to the audit sink. Break-glass access exists, notifies the security
         channel immediately, and is reviewed within one business day."""),
        ("Observability baseline", """Every cluster ships metrics to Managed
         Prometheus, logs to the regional log bucket, and traces to Cloud Trace at a
         1% sample rate rising to 100% for any request that returns 5xx. Tenant teams
         own their own dashboards and alerts; the platform team owns cluster-level
         alerting and the four golden-signal dashboards per cluster."""),
        ("Multi-tenancy isolation", """Tenants are isolated by namespace, network
         policy, ResourceQuota and a per-team Workload Identity service account.
         There is no node-level isolation between tenants in the general pool. Three
         workloads require it for compliance reasons and run on dedicated node pools
         with taints; they are listed in the exceptions register."""),
        ("Config Sync topology", """Every cluster syncs from the fleet-config
         repository, from a directory selected by cluster name, with a root sync for
         platform-owned resources and one repo sync per tenant namespace. Drift is
         reverted within four minutes. Config Sync is the only supported way to place
         a resource in a cluster permanently; anything applied by hand is temporary
         by construction."""),
        ("Node pool upgrade policy", """Node pools are surge-upgraded with maxSurge 1
         and maxUnavailable 0, one pool at a time, never during a change freeze and
         never on a Friday. A full fleet upgrade takes eleven days. The order is
         sandbox, dev, staging, then production in ascending blast-radius order,
         which puts the payments clusters last."""),
        ("Certificate management", """TLS certificates for external endpoints are
         Google-managed. Internal mTLS certificates are issued by Cloud Service Mesh
         with a 24-hour lifetime and automatic rotation. The three legacy services
         that cannot do mTLS use a manually rotated certificate from the internal CA,
         with a 90-day lifetime and a calendar reminder at 60 days."""),
        ("Data residency", """EU customer data may only be processed in
         europe-west1, europe-west3 and europe-west4. Australian customer data may
         only be processed in australia-southeast1. This is enforced by an
         organisation policy on resource locations and by a Gatekeeper constraint
         that rejects a workload carrying meridian.io/data-class=eu-personal in a
         non-EU cluster."""),
        ("Quota headroom target", """Every production cluster is sized to absorb the
         loss of one zone plus 30% growth without hitting a quota. The capacity
         register tracks the headroom figure per cluster and the platform team
         reviews anything below 15% at the weekly operations meeting."""),
        ("Incident severity definitions", """Sev-1 is customer money movement stopped
         or customer data exposed. Sev-2 is a degraded customer-facing path with a
         workaround. Sev-3 is internal impact only. Sev-1 pages the incident
         commander rota and the on-call director; Sev-2 pages the owning team;
         Sev-3 is a ticket. Declaring a Sev-1 is never wrong in hindsight and the
         review explicitly does not second-guess the call."""),
        ("Runbook standard", """Every alert that pages a human must link to a runbook
         with, in order: how to confirm the alert is real, the immediate mitigation,
         the diagnostic steps, and the escalation path. An alert without a runbook is
         disabled at the next review, because an unactionable page is worse than no
         page."""),
        ("Terraform boundary", """Terraform provisions the cluster, node pools,
         networking, IAM and the Backup for GKE plan. Config Sync manages everything
         inside the cluster. The boundary is deliberate and absolute: nothing inside
         a cluster is created by Terraform, so a Terraform apply can never restart a
         workload."""),
        ("Deprecation process", """A platform capability being withdrawn gets a
         deprecation notice with a removal date at least two quarters out, an
         automated inventory of who is still using it, a monthly nag to those owners,
         and a hard removal on the date. Removal dates have been moved exactly twice
         in three years and both moves are documented in the change log."""),
        ("On-call handover", """The platform on-call rota rotates Wednesday at 10:00
         UTC. Handover is a fifteen-minute call covering open incidents, anything
         degraded but not paging, in-flight changes, and anything expected to fire in
         the next week. A handover that takes less than five minutes usually means
         something was not said."""),
        ("Capacity request path", """A tenant needing more quota files a capacity
         request with the peak figure, the date it is needed, and the business
         reason. The platform team either raises the ResourceQuota, raises the GCP
         quota, or adds nodes. Turnaround is three business days, or same-day for an
         incident."""),
        ("Sandbox lifecycle", """Sandbox clusters are created on demand and deleted
         automatically after fourteen days unless the owner extends them. There is no
         backup, no support, and no expectation of availability. Roughly forty
         sandbox clusters exist at any moment and the count is deliberately not
         controlled."""),
        ("Chaos programme", """The platform team runs a monthly game day against
         staging: a zone is drained, a node pool loses capacity, or the egress
         gateway is failed. Findings become runbook entries. Production chaos was
         attempted once, produced a real Sev-2, and has not been repeated pending a
         better blast-radius control."""),
        ("Third-party controllers", """Any controller not from Google or the platform
         team requires a security review, a resource limit, and a named owner before
         it may run in a production cluster. Seven such controllers are approved and
         are listed in the exceptions register. An unreviewed controller is removed
         on discovery, without notice, because a controller has cluster-wide reach by
         definition."""),
        ("Log retention tiers", """Application logs go to a regional bucket. Audit
         logs go to a separate write-once bucket in a different project that the
         platform team cannot delete from. Retention is set by policy and has changed
         twice; consult the current policy rather than assuming, because the earlier
         figures are still written down in older runbooks."""),
        ("Service mesh onboarding", """Onboarding a service to the mesh needs a
         sidecar injection label on the namespace, a rollout, a PeerAuthentication in
         PERMISSIVE mode for one week, then a flip to STRICT. Skipping the permissive
         week is the single most common cause of a self-inflicted Sev-2 during
         onboarding."""),
        ("DNS convention", """Internal service DNS is
         <service>.<team>-<service>.svc.cluster.local within a cluster and
         <service>.<team>.mfs.internal across clusters through the mesh. External
         names are <service>.api.meridianfs.com. There is no split-horizon DNS in the
         fleet and there will not be."""),
        ("Storage classes", """Three storage classes are available: standard-rwo for
         general use, premium-rwo for databases, and a regional-pd class for the four
         workloads that need synchronous cross-zone replication. Local SSD is
         available on request for two ML workloads and is understood to be ephemeral;
         a node repair loses the data."""),
        ("Workload identity mapping", """Each tenant service account in a namespace
         maps to exactly one Google service account, named
         <team>-<service>@<project>.iam.gserviceaccount.com. One-to-many mappings are
         rejected in review because they make an access audit unanswerable."""),
        ("Cluster decommission", """Decommissioning a cluster requires: traffic
         drained and verified at zero for 48 hours, a final backup taken and restore-
         tested, the fleet-config directory removed, the Terraform workspace
         destroyed, and the entry moved to the decommissioned register. Nine clusters
         have been through this process and two of them had to be rebuilt because
         step one was signed off from a dashboard that was itself cached."""),
        ("Alert routing", """Cluster-level alerts route to the platform rota.
         Namespace-level alerts route to the owning team's rota by the
         meridian.io/team label. An alert that cannot be attributed to a team routes
         to the platform rota and generates a weekly report of unattributable alerts,
         which is how missing labels get found."""),
        ("Kubernetes version policy", """The fleet stays within two minor versions of
         the newest GKE stable release and never runs a version past its GKE end of
         support. Version skew across the fleet is tracked weekly; the target is no
         more than two distinct minor versions in production at any time, and the
         record is five, during the Atlas migration."""),
        ("Documentation home", """Cluster facts live in the fleet inventory. Policies
         live in ADRs. Procedures live in runbooks. Incidents live in postmortems.
         A fact written in two of these places will diverge, so each has exactly one
         home and the others link to it."""),
    ]
    for i, (title, body) in enumerate(rest, start=9):
        add(f"CONV-{i:03d}", "convention", body, title=title)


# ==========================================================================
# 2. Architecture decisions — including the six supersession chains
# ==========================================================================

SUPERSESSION_CHAINS = [
    {
        "topic": "service account keys",
        "docs": [
            ("ADR-2024-014", "2024-03-11", "active-at-the-time", """
             Decision: service account keys are permitted for workloads that cannot use
             Workload Identity, provided the key is stored in Secret Manager and rotated
             every 90 days by the key-rotation job. Context: at the time of writing, the
             mesh sidecar could not obtain a Workload Identity token during init, so
             thirty-one workloads had no alternative. Consequences: the key inventory must
             be reviewed quarterly and any key older than 90 days raises a Sev-3."""),
            ("ADR-2025-031", "2025-06-02", "revised", """
             Decision: Workload Identity is mandatory for all new workloads. Existing
             service account keys are grandfathered until their owning team's next major
             release, with a backstop of 2026-01-31. Supersedes ADR-2024-014. Context: the
             init-container limitation was fixed in GKE 1.29 and the thirty-one exceptions
             fell to four. Consequences: no new key may be issued; the rotation job stays
             running for the grandfathered set only."""),
            ("ADR-2026-052", "2026-04-20", "current", """
             Decision: service account keys are banned outright across the Meridian fleet.
             All workloads use Workload Identity Federation. Key creation is blocked by an
             organisation policy constraint, and any existing key is deleted on discovery
             without notice. Supersedes ADR-2025-031 and ADR-2024-014. Context: the
             grandfathering backstop passed on 2026-01-31 with four workloads still
             holding keys, and the January partner-gateway incident was caused by one of
             them being committed to a repository. Consequences: the key-rotation job is
             decommissioned. There is no longer any approved path to a service account
             key; a workload that genuinely cannot federate must be redesigned."""),
        ],
        "probe": ("What is our current policy on service account keys?",
                  ["banned", "Workload Identity Federation"],
                  ["grandfathered", "rotated every 90 days", "permitted for workloads"]),
    },
    {
        "topic": "ingress",
        "docs": [
            ("ADR-2024-008", "2024-01-22", "active-at-the-time", """
             Decision: ingress-nginx is the standard ingress controller for the Meridian
             fleet, deployed per cluster by Config Sync, fronted by a regional load
             balancer. Context: the team knows nginx well and the Gateway API was alpha.
             Consequences: each cluster runs its own nginx deployment and each team writes
             Ingress resources with nginx-specific annotations."""),
            ("ADR-2025-019", "2025-03-17", "revised", """
             Decision: new services use the GKE Gateway API with the global external
             Application Load Balancer. Existing nginx Ingress resources are migrated
             opportunistically, with no forced deadline. Supersedes ADR-2024-008. Context:
             Gateway API went GA and multi-cluster routing through nginx required a layer
             of DNS trickery nobody wanted to own. Consequences: two ingress paths exist
             simultaneously, which is accepted for the duration of the migration."""),
            ("ADR-2026-047", "2026-02-09", "current", """
             Decision: the Gateway API is the only supported ingress path. ingress-nginx is
             removed from the fleet; the remaining eleven Ingress resources must migrate by
             2026-09-30, after which the nginx deployments are deleted whether or not the
             migration is complete. Supersedes ADR-2025-019 and ADR-2024-008. Context: the
             opportunistic migration stalled at eleven resources for nine months, and
             running two ingress paths doubled the surface of every networking incident.
             Consequences: nginx-specific annotations stop working on the removal date."""),
        ],
        "probe": ("Which ingress controller should a new service use?",
                  ["Gateway API"],
                  ["ingress-nginx is the standard", "opportunistically"]),
    },
    {
        "topic": "log retention",
        "docs": [
            ("ADR-2024-021", "2024-05-30", "active-at-the-time", """
             Decision: application logs are retained for 30 days and audit logs for 90
             days. Context: cost. At the time the fleet produced 4TB of logs a day and
             longer retention was not justifiable. Consequences: an investigation older
             than 30 days has no application logs to work from."""),
            ("ADR-2025-036", "2025-08-14", "revised", """
             Decision: application logs are retained for 90 days and audit logs for 400
             days, in a write-once bucket in a separate project. Supersedes ADR-2024-021.
             Context: the SOC2 Type II audit found the 90-day audit retention
             insufficient, and two incident reviews were unable to establish a timeline.
             Consequences: log spend roughly triples; tenants are charged for their own
             application log volume from Q4."""),
            ("ADR-2026-044", "2026-01-28", "current", """
             Decision: application logs are retained for 90 days hot and a further 275 days
             in archive storage; audit logs are retained for seven years in the write-once
             bucket. Supersedes ADR-2025-036 and ADR-2024-021. Context: the PCI-DSS
             assessment requires one year of retrievable application logs and the
             regulatory reporting obligation for audit trails is seven years.
             Consequences: an investigation reaching past 90 days requires an archive
             restore, which takes up to twelve hours and must be requested through the
             platform team."""),
        ],
        "probe": ("How long do we retain audit logs?",
                  ["seven years"],
                  ["90 days for audit", "400 days"]),
    },
    {
        "topic": "container base image",
        "docs": [
            ("ADR-2024-030", "2024-07-08", "active-at-the-time", """
             Decision: all Meridian services build on the shared debian-slim base image
             maintained by release-engineering, patched monthly. Context: teams were using
             sixteen different bases and CVE response required sixteen conversations.
             Consequences: one base to patch, and a monthly rebuild of every service."""),
            ("ADR-2025-027", "2025-05-06", "revised", """
             Decision: new services build on distroless. Existing services move to
             distroless at their next major version. Supersedes ADR-2024-030. Context: the
             debian-slim base carried 340 packages, of which services used perhaps twenty,
             and the monthly CVE triage had become a full-time job. Consequences: no shell
             in the container, so debugging moves to ephemeral debug containers, which
             several teams had not used before."""),
            ("ADR-2026-051", "2026-03-30", "current", """
             Decision: all images build on the Chainguard hardened base distributed through
             the internal registry mirror. Supersedes ADR-2025-027 and ADR-2024-030.
             Context: distroless solved package count but not provenance, and the PCI
             assessor asked for an SBOM per image that the distroless pipeline could not
             produce. Consequences: every image now ships an SBOM and a signed attestation;
             build times rise by roughly ninety seconds; the debian-slim base image is
             withdrawn on 2026-10-01."""),
        ],
        "probe": ("What base image should a new service use?",
                  ["Chainguard"],
                  ["debian-slim base image maintained", "new services build on distroless"]),
    },
    {
        "topic": "backup tooling",
        "docs": [
            ("ADR-2024-017", "2024-04-15", "active-at-the-time", """
             Decision: cluster backups are taken with Velero to a GCS nearline bucket,
             nightly, retained 14 days. Context: Backup for GKE was not available in three
             of the regions Meridian operates in. Consequences: Velero runs as a
             cluster-admin controller in every cluster, which is a standing privilege the
             security team has flagged."""),
            ("ADR-2025-024", "2025-04-21", "revised", """
             Decision: clusters in supported regions move to Backup for GKE. Velero remains
             in the three unsupported regions. Supersedes ADR-2024-017. Context: Backup for
             GKE reached the remaining regions except asia-south1, australia-southeast1 and
             southamerica-east1. Consequences: two backup mechanisms, two restore runbooks,
             and a restore drill that has to cover both."""),
            ("ADR-2026-049", "2026-03-02", "current", """
             Decision: Backup for GKE is the only backup mechanism in the fleet. Velero is
             removed. Cardholder-data clusters additionally replicate to a second region.
             Supersedes ADR-2025-024 and ADR-2024-017. Context: Backup for GKE reached all
             twelve Meridian regions in January, and the standing cluster-admin privilege
             Velero required was the last open finding from the internal audit.
             Consequences: the Velero restore runbook is retired; anyone reaching for it is
             following a document that describes software no longer installed."""),
        ],
        "probe": ("How do we back up a production cluster?",
                  ["Backup for GKE"],
                  ["Velero to a GCS nearline", "Velero remains in the three"]),
    },
    {
        "topic": "node operating system",
        "docs": [
            ("ADR-2024-025", "2024-06-19", "active-at-the-time", """
             Decision: node pools run Container-Optimized OS with the docker runtime.
             Context: two workloads mount the docker socket for image builds.
             Consequences: the fleet stays on a runtime that GKE has announced it will
             remove."""),
            ("ADR-2025-022", "2025-04-02", "revised", """
             Decision: all node pools move to Container-Optimized OS with containerd. The
             two socket-mounting build workloads move to Kaniko. Supersedes ADR-2024-025.
             Context: GKE removed the docker runtime option. Consequences: forced, not
             chosen; the migration was completed in six weeks."""),
            ("ADR-2026-046", "2026-02-24", "current", """
             Decision: batch and ML inference node pools move to Arm (t2a and c4a) where
             the workload has a multi-arch image. General and system pools stay on x86.
             Supersedes the pool-shape guidance in ADR-2025-022. Context: a 34% price-
             performance improvement on the batch fleet at current volumes. Consequences:
             any image scheduled to a batch pool must be a multi-arch manifest, and a
             single-arch image fails with an ImagePullBackOff whose error text does not
             mention architecture at all."""),
        ],
        # Deliberately asks about the batch pools rather than the runtime: the
        # runtime answer (containerd) is settled in the *middle* document, and
        # only the pool-shape guidance is superseded by the last one. A probe
        # whose answer is not in the current document measures the corpus, not
        # the provider.
        "probe": ("What node pool shape should a batch or ML inference workload use?",
                  ["Arm", "multi-arch"],
                  ["docker runtime"]),
    },
]


def gen_adrs():
    for chain in SUPERSESSION_CHAINS:
        for adr_id, date, status, body in chain["docs"]:
            add(adr_id, "adr", f"{adr_id} ({date}, {status}). {body}",
                gold=True, title=f"{adr_id} — {chain['topic']}")

    # Non-chain ADRs. Filler, but plausible filler: these are the decisions a real
    # platform team writes down and never revisits.
    others = [
        ("ADR-2024-002", "2024-01-08", "Regional clusters, not zonal", """Decision: every
         Meridian cluster is regional. Zonal clusters are not permitted, including in
         sandbox. Context: a zonal control plane outage in a sandbox cluster consumed a
         day of platform time explaining that it was expected behaviour."""),
        ("ADR-2024-005", "2024-01-19", "One project per environment", """Decision: prod,
         stg, dev and sbx each get one GCP project per region. Context: quota is a
         per-project resource, and a sandbox workload exhausting the regional CPU quota
         must not be able to stop a production scale-up."""),
        ("ADR-2024-011", "2024-02-27", "No cluster-admin for humans", """Decision: no human
         holds cluster-admin on a production cluster, including the platform team.
         Elevation goes through the access broker. Context: the SOC2 auditor asked who
         could delete a namespace and the honest answer was forty-one people."""),
        ("ADR-2024-019", "2024-05-02", "Config Sync over Argo", """Decision: Config Sync,
         not Argo CD, is the fleet's GitOps engine. Context: fleet-level policy
         distribution and the Config Controller integration mattered more than Argo's
         UI, and running both was rejected."""),
        ("ADR-2024-023", "2024-06-04", "Managed Prometheus over self-hosted", """Decision:
         Managed Service for Prometheus replaces the self-hosted Prometheus pairs.
         Context: the self-hosted pairs were the single largest source of platform toil
         and had themselves caused two Sev-2s by OOMing during incidents, which is
         precisely when they were needed."""),
        ("ADR-2024-028", "2024-06-27", "Spot nodes for batch only", """Decision: spot nodes
         are permitted for batch, CI and ML training, and forbidden for any request-
         serving workload. Context: a fifteen-minute preemption cascade during a card
         authorisation peak."""),
        ("ADR-2025-004", "2025-01-14", "Cloud Service Mesh over self-managed Istio",
         """Decision: adopt Cloud Service Mesh. Context: the self-managed Istio control
         plane needed a version upgrade every ten weeks and each one was a change
         advisory board item."""),
        ("ADR-2025-009", "2025-02-05", "Gatekeeper for policy", """Decision: Policy
         Controller (Gatekeeper) enforces admission policy fleet-wide, distributed by
         Config Sync. Context: policy previously lived in six mutating webhooks written
         by four teams, and nobody could enumerate what was enforced."""),
        ("ADR-2025-014", "2025-02-26", "No service mesh in sandbox", """Decision: sandbox
         clusters do not run the mesh. Context: mesh onboarding was the most common
         reason a sandbox cluster did not work, and sandbox is meant to be disposable."""),
        ("ADR-2025-040", "2025-09-23", "Dataplane V2 fleet-wide", """Decision: all clusters
         run Dataplane V2. Clusters that cannot be migrated in place are rebuilt.
         Context: network policy logging and the eBPF dataplane's observability were
         needed for the PCI segmentation evidence."""),
        ("ADR-2025-045", "2025-11-11", "Multi-cluster services for failover", """Decision:
         cross-region failover uses multi-cluster Services and multi-cluster Gateway,
         not DNS failover. Context: DNS failover took eleven minutes to converge in the
         October game day, against a four-minute objective."""),
        ("ADR-2026-003", "2026-01-13", "Ephemeral debug containers only", """Decision:
         debugging a running pod uses kubectl debug with an ephemeral container. Shell
         access into an application container is removed with the distroless and
         Chainguard bases and will not be restored."""),
        ("ADR-2026-011", "2026-01-27", "Fleet-wide cost anomaly detection", """Decision: a
         daily cost anomaly job flags any namespace whose spend moves more than 40%
         week-over-week and opens a ticket against the owning team. Context: a
         misconfigured ML training job cost 84,000 dollars over a long weekend before
         anyone noticed."""),
        ("ADR-2026-019", "2026-02-03", "No in-cluster databases for tier-1", """Decision:
         tier-1 services use Cloud SQL or Spanner, never a StatefulSet database.
         Existing in-cluster databases in tier-1 paths are migrated. Context: three of
         the five longest Meridian outages were in-cluster database recoveries."""),
        ("ADR-2026-027", "2026-02-17", "Binary Authorization in staging too", """Decision:
         Binary Authorization is enforced in staging as well as production. Context: an
         unsigned image reached production through a staging promotion that skipped the
         pipeline, and the control that should have caught it was production-only."""),
        ("ADR-2026-033", "2026-03-10", "Per-team egress gateways for partners",
         """Decision: teams with partner integrations that require IP allowlisting get a
         dedicated egress NAT IP rather than sharing the fleet gateway. Context: a
         partner allowlisting the shared gateway IP inadvertently granted fleet-wide
         egress to their API."""),
        ("ADR-2026-038", "2026-03-24", "Kubernetes API audit to a separate sink",
         """Decision: Kubernetes API audit logs go to a dedicated project the platform
         team has no delete permission on. Context: an auditor observed that the team
         being audited controlled the audit trail's lifecycle."""),
        ("ADR-2026-055", "2026-05-12", "Freeze exceptions require director approval",
         """Decision: change-freeze exceptions escalate to director level. Context:
         thirty-one freeze exceptions were granted in the previous year, which means the
         freeze was advisory in practice."""),
        ("ADR-2026-058", "2026-06-02", "Retire the Atlas naming exceptions", """Decision:
         the remaining legacy-named clusters are renamed by rebuild during their next
         upgrade window, targeting zero legacy names by 2027-Q2. Context: every incident
         involving a legacy cluster spends its first minutes establishing which region it
         is actually in."""),
        ("ADR-2026-061", "2026-06-23", "Standardise on one secrets CSI driver",
         """Decision: the Secret Manager CSI driver is the only supported secrets
         mechanism. The External Secrets Operator installed by two teams is removed.
         Context: two mechanisms meant two rotation behaviours and one of them failed
         silently."""),
    ]
    for adr_id, date, title, body in others:
        add(adr_id, "adr", f"{adr_id} ({date}). {title}. {body}", title=title)

    # Filler ADRs to reach volume.
    adr_topics = [
        "HPA metric selection", "Pod topology spread", "Init container standards",
        "Sidecar resource sizing", "Job retry semantics", "CronJob timezone handling",
        "Readiness probe standards", "Graceful shutdown timing", "Image tag immutability",
        "Registry mirror policy", "Helm chart ownership", "CRD version support",
        "Admission webhook timeouts", "Node taint taxonomy", "Priority class tiers",
        "Ephemeral storage limits", "Sysctl allowlist", "Kernel version floor",
        "GPU node pool access", "Preemption tolerance", "Cluster upgrade cadence",
        "Fleet-config repository layout", "Namespace deletion safety", "Finalizer policy",
        "Custom metric adapters", "Log sampling rates", "Trace propagation headers",
        "Service account naming", "RBAC aggregation", "Audit policy scope",
    ]
    for i, topic in enumerate(adr_topics):
        num = 62 + i
        team = rng.choice(TEAMS)
        add(f"ADR-2026-{num:03d}", "adr",
            f"ADR-2026-{num:03d} (2026-{rng.randint(1,6):02d}-{rng.randint(1,28):02d}). {topic}. "
            f"Decision recorded by platform-{rng.choice(['networking','security','observability'])} "
            f"after review with {team}. The standard is documented in the fleet-config repository "
            f"under policy/{topic.lower().replace(' ', '-')}.yaml and enforced by Policy Controller "
            f"in prod and stg, with a warning-only constraint in dev. Deviations require an entry "
            f"in the exceptions register with a named owner and a review date.",
            title=topic)


# ==========================================================================
# 3. Incident postmortems
# ==========================================================================

GOLD_POSTMORTEMS = [
    ("PM-2026-014", "2026-04-03", "Sev-1", """
     Incident PM-2026-014, 2026-04-03, Sev-1, duration 3h47m. Customer impact: card
     authorisation declined for approximately 11% of transactions in europe-west1 for
     two hours and nine minutes, roughly 340,000 declined authorisations. Symptom: the
     card-authoriser pods in mfs-prod-euw1-03 began failing readiness checks with no
     change deployed, and the pods that remained ready saw p99 latency rise from 40ms
     to 9 seconds. Nothing had been released for six days. Root cause: the etcd
     database of the regional control plane had grown past its compaction threshold
     because a controller in the fraud-detection namespace was writing a ConfigMap
     update every 200 milliseconds in a hot loop, a bug introduced three weeks earlier
     and harmless until the object count crossed the point where compaction could not
     keep up. API server latency rose, which made readiness probes time out, which
     caused the kubelet to restart pods, which generated more API traffic.
     Contributing factor: the etcd size alert existed but was routed to a dashboard
     rather than a pager. Mitigation: the offending controller was scaled to zero,
     which stopped the write amplification within ninety seconds; the control plane
     recovered on its own over the following eleven minutes. Action items: page on
     etcd database size above 4GB rather than dashboard it; add a per-namespace API
     write rate limit; the fraud-detection controller's reconcile loop now has a
     minimum backoff. Lesson: the incident had no deploy to correlate against, and the
     first ninety minutes were spent looking for one.
     """),
    ("PM-2026-009", "2026-02-11", "Sev-2", """
     Incident PM-2026-009, 2026-02-11, Sev-2, duration 5h12m. Customer impact: new
     customer onboarding was unavailable for five hours; existing customers unaffected.
     Symptom: every pod in the kyc-onboarding namespace across four clusters was stuck
     in ContainerCreating, with events showing a timeout mounting a volume. Root cause:
     the Secret Manager CSI driver's node pod had been evicted by a node pressure
     condition and its DaemonSet had a PodDisruptionBudget of maxUnavailable 0, so the
     scheduler could not place a replacement while the node was cordoned during an
     upgrade. Every workload mounting a secret on that node hung indefinitely rather
     than failing, because the CSI mount has no timeout by default. Mitigation:
     uncordon the node, delete the stuck pods, then fix the PDB. Action items: the CSI
     driver DaemonSet PDB is now maxUnavailable 1; a mount timeout of 120 seconds is
     set fleet-wide so that a failed mount produces a crashloop, which pages, instead
     of a hang, which does not. Lesson: a workload that hangs is worse than a workload
     that crashes, because only one of them is alerted on.
     """),
    ("PM-2025-041", "2025-11-19", "Sev-1", """
     Incident PM-2025-041, 2025-11-19, Sev-1, duration 1h58m. Customer impact: all
     outbound payment rails stopped for one hour and fifty-eight minutes; approximately
     19 million dollars of payments were delayed, none lost. Symptom: payment-router in
     mfs-prod-use4-01 and mfs-prod-use4-02 could not reach the partner gateway; every
     request failed with a TLS handshake error. Root cause: the shared egress NAT
     gateway's IP was rotated as part of a routine subnet change, and three payment
     partners allowlist that IP. The subnet change was reviewed by platform-networking,
     who did not know about the allowlists, and by the change advisory board, who did
     not either, because the allowlist arrangement was recorded in a partner contract
     and nowhere in the fleet documentation. Mitigation: the previous IP was reclaimed
     and reassigned, which took 71 minutes of which 44 were spent locating who could
     approve it. Action items: partner IP allowlists are now recorded in the fleet
     inventory against the egress gateway; ADR-2026-033 gives partner-integrated teams
     dedicated egress IPs; any change to a NAT IP now requires an explicit allowlist
     check. Lesson: the dependency that broke was documented, but not anywhere the
     people making the change would look.
     """),
    ("PM-2026-021", "2026-05-28", "Sev-2", """
     Incident PM-2026-021, 2026-05-28, Sev-2, duration 8h03m. Customer impact:
     statements for approximately 60,000 customers were generated with the previous
     month's data. Symptom: no alert fired at all; the problem was reported by customer
     support after 14 customer complaints. Root cause: the statement-renderer reads a
     materialised view refreshed by a CronJob. The CronJob had a schedule expressed in
     a timezone, and the cluster it ran on was rebuilt in March with a different
     default timezone, so the job ran at 02:00 local rather than 02:00 UTC, four hours
     after the statement batch read the view. The CronJob completed successfully every
     night, so every monitor was green. Mitigation: rerun the refresh, regenerate the
     affected statements, notify the customers. Action items: all CronJob schedules are
     now expressed in UTC explicitly and the timezone field is set rather than
     inherited; the statement batch now asserts the freshness of the view it reads
     before running. Lesson: a job that succeeds at the wrong time is invisible to
     every success-based monitor, and data freshness must be asserted by the consumer,
     not assumed from the producer's exit code.
     """),
    ("PM-2025-033", "2025-09-08", "Sev-1", """
     Incident PM-2025-033, 2025-09-08, Sev-1, duration 6h22m. Customer impact: the
     retail mobile application was unavailable for six hours and twenty-two minutes.
     Symptom: mobile-bff returned 503 in every region simultaneously, which immediately
     ruled out a regional cause. Root cause: a Policy Controller constraint was updated
     in fleet-config to require the meridian.io/data-class label on all workloads. The
     constraint was authored with enforcementAction deny and committed directly to the
     main branch, and Config Sync distributed it to all 500 clusters in under four
     minutes. Every workload without that label — which was most of them, since the
     label was being introduced by the same change — was blocked from rescheduling.
     Running pods were unaffected, so nothing broke until the first node pool
     autoscaled down and its pods could not be rescheduled. The failure propagated over
     forty minutes as normal autoscaling churn touched more workloads. Mitigation:
     revert the constraint. The revert took eleven minutes to author and four minutes
     to propagate; most of the six hours was spent understanding a failure mode where
     nothing had visibly changed at the time symptoms began. Action items: policy
     changes now land as enforcementAction dryrun first, for a minimum of one week,
     with a report of what would have been denied; fleet-config main is now protected
     and requires review from platform-security for anything under policy/. Lesson:
     the blast radius of a GitOps engine is the entire fleet, and its propagation speed
     is a feature until it is not.
     """),
    ("PM-2026-006", "2026-01-16", "Sev-1", """
     Incident PM-2026-006, 2026-01-16, Sev-1, duration 2h31m, security. Customer
     impact: none confirmed; treated as a potential data exposure. Symptom: the secret
     scanner flagged a service account key committed to the partner-api team's
     repository, in a commit from eleven months earlier. Root cause: a grandfathered
     service account key under ADR-2025-031, with permissions on the partner-gateway
     project, was committed as part of a local test fixture and the repository was
     later made internal-visible. The key had not been rotated because the rotation job
     only covered keys registered in the inventory and this one had been created
     manually before the inventory existed. Mitigation: key revoked within nine
     minutes of discovery; access logs reviewed for the eleven-month window, showing no
     use from an unexpected source; the repository history was rewritten. Action items:
     this incident is the direct cause of ADR-2026-052, which bans service account keys
     outright. Lesson: an exception process that grandfathers existing instances needs
     an inventory that is complete, and this one was built from the rotation job's
     own list, which is circular.
     """),
    ("PM-2025-028", "2025-07-24", "Sev-2", """
     Incident PM-2025-028, 2025-07-24, Sev-2, duration 4h44m. Customer impact: fraud
     scoring degraded to a rules-only fallback for four hours; approximately 2,100
     transactions were manually reviewed that would normally have been auto-approved.
     Symptom: model-server pods in the ml-platform namespace on mfs-prod-usc1-04 entered
     ImagePullBackOff after a routine node pool scale-up. The pods already running were
     fine. Root cause: the batch node pool had been migrated to Arm under ADR-2026-046's
     predecessor pilot, and the model-server image was built for amd64 only. Existing
     pods were on the old x86 nodes; the scale-up added Arm nodes and the scheduler,
     seeing no architecture constraint on the pod, placed pods there. The
     ImagePullBackOff error text reports "no matching manifest" without mentioning
     architecture, and the team spent two hours checking registry permissions.
     Mitigation: cordon the Arm nodes, then add a nodeSelector. Action items: all images
     destined for batch pools are built multi-arch by the pipeline; a Gatekeeper
     constraint now requires an explicit kubernetes.io/arch nodeSelector on any workload
     scheduled to a pool with mixed architecture. Lesson: the error message for an
     architecture mismatch does not contain the word architecture.
     """),
    ("PM-2026-017", "2026-04-21", "Sev-3", """
     Incident PM-2026-017, 2026-04-21, Sev-3, duration 11h. Customer impact: none.
     Internal impact: the regulatory reporting batch missed its internal deadline by
     eleven hours, with four hours of margin remaining before the external filing
     deadline. Symptom: regbatch-runner Jobs were created but never scheduled, sitting
     Pending with no events. Root cause: the regulatory-reporting namespace's
     ResourceQuota had been reached, and a Job whose pod cannot be admitted due to
     quota produces an event on the ReplicaSet, not on the Job, and nothing was
     watching there. The quota had been reached because a previous run's pods were
     retained by a ttlSecondsAfterFinished that was never set. Mitigation: delete
     completed pods, raise the quota. Action items: ttlSecondsAfterFinished is now set
     by a mutating policy on every Job in the fleet; quota utilisation above 85% now
     alerts the owning team. Lesson: quota exhaustion is silent in exactly the place
     people look first.
     """),
    ("PM-2025-019", "2025-05-13", "Sev-2", """
     Incident PM-2025-019, 2025-05-13, Sev-2, duration 3h09m. Customer impact: internal
     transfers between Meridian accounts failed for approximately 40 minutes for
     customers whose request landed in asia-southeast1. Symptom: transfer-service in
     mfs-prod-ase1-01 returned 503s intermittently, roughly one request in six, with no
     pattern by customer or amount. Root cause: mTLS was flipped from PERMISSIVE to
     STRICT on the transfer-service namespace as part of mesh onboarding. One of the
     five pods had been running since before sidecar injection was enabled on the
     namespace and had no sidecar, so it could not participate in mTLS. Because it was
     one pod of five and load balancing is round-robin, exactly one request in five to
     six failed, which reads as flakiness rather than as a hard failure. Mitigation:
     restart the deployment so every pod gets a sidecar. Action items: the mesh
     onboarding runbook now includes an explicit "verify every pod has a sidecar before
     flipping to STRICT" step with the exact kubectl command; a constraint now blocks
     the STRICT flip if any pod in the namespace lacks the sidecar container. Lesson: a
     partial failure that scales with replica count presents as intermittency.
     """),
    ("PM-2026-024", "2026-06-14", "Sev-2", """
     Incident PM-2026-024, 2026-06-14, Sev-2, duration 2h18m. Customer impact: business
     banking customers in europe-west3 could not log in for two hours. Symptom:
     identity-broker in mfs-prod-euw3-02 was healthy, serving traffic, and rejecting
     every authentication with an internal error. Root cause: the internal CA
     certificate used by the three legacy services that cannot do mesh mTLS expired.
     It has a 90-day lifetime and a calendar reminder at 60 days; the reminder was on
     the calendar of an engineer who had left the company in April, and the calendar
     was deleted with the account. Mitigation: reissue the certificate. Action items:
     the three manual certificates are now tracked in the fleet inventory with an
     expiry field and alert at 30 days to the platform rota, not to an individual;
     offboarding now includes a check for calendar-based operational reminders.
     Lesson: an operational dependency on a named individual's calendar is an
     undocumented single point of failure that offboarding will not catch.
     """),
]


def gen_postmortems():
    for pm_id, date, sev, body in GOLD_POSTMORTEMS:
        add(pm_id, "postmortem", body, gold=True, title=f"{pm_id} {sev} {date}")

    # Filler postmortems. Realistic archetypes with varied detail; their job is to be
    # a plausible haystack that a symptom-shaped query could plausibly match.
    archetypes = [
        ("OOMKill cascade in {ns} on {cluster}", "memory limit was raised for a new feature but the node pool was not resized, so the scheduler packed pods that then OOMKilled under load"),
        ("Certificate expiry on {svc}", "a manually managed certificate expired outside the automated rotation set"),
        ("Node pool upgrade stall on {cluster}", "a PodDisruptionBudget with maxUnavailable 0 blocked the drain and the upgrade sat for nine hours"),
        ("DNS resolution failures in {ns}", "NodeLocal DNSCache pods were evicted and conntrack entries went stale"),
        ("Quota exhaustion for {team}", "a runaway CronJob created Jobs faster than they completed"),
        ("Slow rollout of {svc}", "a readiness probe with a 30-second initial delay multiplied across 60 replicas"),
        ("Config Sync drift loop on {cluster}", "a mutating webhook modified a resource that Config Sync then reverted, in a loop"),
        ("Egress saturation from {ns}", "a retry storm against a partner API with no backoff"),
        ("Spot preemption cascade in {ns}", "a batch workload's PDB prevented rescheduling faster than preemptions arrived"),
        ("Stale endpoints for {svc}", "a slow graceful shutdown left the pod in Endpoints after it stopped serving"),
        ("Disk pressure eviction on {cluster}", "ephemeral storage from unrotated application logs written to the container filesystem"),
        ("Cross-region latency spike for {svc}", "a multi-cluster Service failed over to a distant region and did not fail back"),
        ("Webhook timeout blocking admission in {ns}", "a validating webhook's backend was itself in the namespace being admitted"),
        ("Metric cardinality explosion from {svc}", "a label containing a request id"),
        ("Backup failure on {cluster}", "a PersistentVolume in a zone the backup plan did not cover"),
        ("HPA thrashing on {svc}", "a scale-down stabilisation window shorter than the workload's warm-up time"),
        ("Image pull rate limit for {ns}", "a workload pulling from an upstream registry rather than the mirror"),
        ("Mesh sidecar OOM in {ns}", "sidecar memory sized for the median service applied to one with 4,000 upstream endpoints"),
        ("Audit log gap on {cluster}", "the log sink's service account lost a permission during an IAM cleanup"),
        ("Zone drain during scale-up for {team}", "a maintenance event coinciding with peak, with insufficient headroom in remaining zones"),
    ]
    sevs = ["Sev-2", "Sev-2", "Sev-3", "Sev-3", "Sev-1"]
    for i in range(70):
        arch, cause = archetypes[i % len(archetypes)]
        team = TEAMS[(i * 7) % len(TEAMS)]
        svc = SERVICES[(i * 11) % len(SERVICES)]
        region, short = REGIONS[(i * 5) % len(REGIONS)]
        cluster = cname("prod" if i % 3 else "stg", short, (i % 6) + 1)
        ns = f"{team}-{svc}"
        year = 2025 if i % 2 else 2026
        num = 100 + i
        title = arch.format(ns=ns, cluster=cluster, svc=svc, team=team)
        sev = sevs[i % len(sevs)]
        add(f"PM-{year}-{num}", "postmortem", f"""
            Incident PM-{year}-{num}, {year}-{(i % 12) + 1:02d}-{(i % 27) + 1:02d}, {sev},
            duration {(i % 6) + 1}h{(i * 7) % 60:02d}m. {title}. Affected {ns} on {cluster}
            in {region}. Symptom: the owning team ({team}) saw elevated error rates and
            the platform rota was paged by the cluster-level alert. Root cause: {cause}.
            Mitigation: the immediate workaround was applied within {(i % 40) + 10} minutes
            and the durable fix landed in the following change window. Action items were
            assigned to {team} and to platform-{rng.choice(['networking', 'security', 'observability'])},
            and are tracked to closure in the incident register. This incident is
            referenced by the runbook for {svc} and contributed to the current guidance
            in the fleet conventions.
            """, title=title)


# ==========================================================================
# 4. Runbooks — ordered procedures, the procedural-fidelity probe
# ==========================================================================

GOLD_RUNBOOKS = [
    ("RB-011", "Control plane etcd size alert", """
     Runbook RB-011: responding to the etcd database size alert. This alert fires when
     the regional control plane's etcd database exceeds 4GB. Follow these steps in
     order; do not skip step 2, because steps 3 and 4 are destructive to the wrong
     namespace if the attribution in step 2 is wrong.
     Step 1: confirm the alert is real by reading the apiserver_storage_db_total_size_in_bytes
     metric for the affected cluster over the last six hours. A sawtooth pattern is
     normal compaction; a monotonic rise is the incident.
     Step 2: identify the writer. Run the API request rate query grouped by namespace
     and verb, restricted to write verbs, over the last thirty minutes. One namespace
     will be one to two orders of magnitude above the rest. Record it.
     Step 3: confirm it is a hot loop rather than legitimate load by checking whether
     the same object names repeat. If the resourceVersion of a single object is
     advancing several times per second, it is a hot loop.
     Step 4: scale the offending controller to zero. Do not delete it; scaling to zero
     is reversible and preserves the state needed for the postmortem.
     Step 5: wait eleven minutes. Compaction is automatic and the control plane
     recovers on its own. Do not request a manual compaction from support during this
     window; doing so during PM-2026-014 extended the incident because the manual
     compaction and the automatic one contended.
     Step 6: once API latency is back under 100ms at p99, notify the owning team and
     open a Sev-3 against them for the reconcile loop.
     Escalation: if the database is still growing after step 4, the writer was
     misidentified; return to step 2. If it exceeds 6GB, page Google Cloud support at
     P1, because the control plane becomes read-only at 8GB.
     """),
    ("RB-004", "Restore a production cluster from backup", """
     Runbook RB-004: restoring a production cluster from backup using Backup for GKE.
     Prerequisite: you must have an incident commander's approval recorded before
     starting. A restore is not reversible and it overwrites live state.
     Step 1: identify the restore point. List backups for the cluster's backup plan and
     choose the most recent one that predates the corruption. Record the backup name in
     the incident channel.
     Step 2: verify the backup is complete, not partial. A backup in state SUCCEEDED
     with a non-zero volume count is usable; state PARTIALLY_SUCCEEDED means at least
     one volume is missing and you must establish which before proceeding.
     Step 3: create the restore plan against a scratch cluster first, never directly
     against production, unless production is already fully down. The scratch restore
     takes twenty to forty minutes and is the only way to know the backup is good.
     Step 4: validate the scratch restore: check that the expected namespaces exist,
     that PersistentVolumeClaims are Bound, and that at least one stateful workload
     starts and passes its readiness probe.
     Step 5: scale the production workloads in the target namespaces to zero. Restoring
     over running workloads produces split-brain on any volume that is still mounted.
     Step 6: execute the restore against production with the same restore plan
     configuration validated in step 3.
     Step 7: scale workloads back up in dependency order: databases and caches first,
     then the services that read them, then the services that serve traffic. The
     dependency order per team is in the service catalogue.
     Step 8: verify at the edge, not at the pod. A pod passing readiness is not the
     same as a customer being served.
     Escalation: if the restore fails partway, do not retry it. Stop, and page the
     disaster-recovery team, because a half-applied restore is a state neither the
     backup nor the cluster's own reconciliation can resolve.
     """),
    ("RB-019", "Rotate a compromised service account credential", """
     Runbook RB-019: responding to a leaked or compromised credential. Time matters
     here; the first three steps should take under ten minutes.
     Step 1: revoke first, investigate second. Disable the key or the identity
     immediately. Do not wait to establish blast radius; a credential in a public
     place is being used.
     Step 2: notify the security channel with the identity name and the location the
     credential was found. Do not paste the credential itself anywhere, including into
     the incident channel.
     Step 3: identify what the identity could reach, from its IAM bindings, not from
     what you believe it was for.
     Step 4: pull the access logs for the identity over the full period the credential
     was exposed, not just the recent window. In PM-2026-006 the exposure window was
     eleven months and the initial review covered thirty days.
     Step 5: establish whether any access came from an unexpected source: an unfamiliar
     IP, a time outside the workload's pattern, or an API the workload does not call.
     Step 6: if the credential is in version control, rewrite history and force-push,
     then confirm the object is unreachable, then request that any forks be deleted.
     History rewriting alone does not remove the object from the remote's object store.
     Step 7: replace the credential with Workload Identity Federation. Under
     ADR-2026-052 there is no approved path to issuing a replacement key, so a
     workload that cannot federate must be redesigned rather than reissued.
     Step 8: write the postmortem within five business days regardless of whether
     exposure is confirmed.
     """),
    ("RB-027", "Mesh onboarding: PERMISSIVE to STRICT", """
     Runbook RB-027: flipping a namespace from PERMISSIVE to STRICT mTLS. This is the
     step that most commonly self-inflicts a Sev-2; the verification in step 3 is what
     prevents it.
     Step 1: confirm the namespace has carried the istio-injection label for at least
     seven days and that the PeerAuthentication is currently PERMISSIVE.
     Step 2: confirm there is no traffic to the namespace from outside the mesh, by
     checking the mesh telemetry for plaintext connections over the last seven days.
     Any non-zero plaintext count is a client that will break.
     Step 3: verify that every pod in the namespace has a sidecar. Count the pods and
     count the pods with two or more containers; the numbers must match. A pod that
     predates injection has no sidecar, will fail under STRICT, and because load
     balancing is round-robin it will present as intermittent failure rather than as an
     outage — this is exactly what happened in PM-2025-019 and it took three hours to
     find because one pod in five is indistinguishable from flakiness.
     Step 4: if any pod lacks a sidecar, restart the deployment and return to step 3.
     Step 5: apply the STRICT PeerAuthentication.
     Step 6: watch the namespace's error rate for fifteen minutes. Roll back by
     reapplying PERMISSIVE at the first sign of elevated 503s; the rollback is
     instant and costs nothing.
     """),
    ("RB-008", "Drain a zone for a maintenance event", """
     Runbook RB-008: draining a zone ahead of a planned maintenance event.
     Step 1: confirm the remaining zones have headroom for the full load plus 30%.
     Read the current utilisation, not the configured request totals. If headroom is
     insufficient, scale up the remaining zones and wait for the nodes to be Ready
     before continuing.
     Step 2: check for workloads with a topology spread constraint of DoNotSchedule
     that would prevent rescheduling into the remaining zones. These must be relaxed
     to ScheduleAnyway for the duration or they will block the drain.
     Step 3: check for PodDisruptionBudgets that would block eviction. A PDB with
     maxUnavailable 0 stalls the drain indefinitely and is the most common reason a
     drain does not complete.
     Step 4: cordon the nodes in the target zone, all of them, before draining any.
     Cordoning progressively lets the scheduler place evicted pods back into the zone
     you are draining.
     Step 5: drain the nodes one at a time with a grace period of at least the longest
     terminationGracePeriodSeconds in the cluster.
     Step 6: verify no pods remain in the zone and that error rates are unchanged.
     Step 7: after the maintenance, uncordon and let the cluster autoscaler rebalance.
     Do not manually rebalance; the autoscaler will do it within an hour and manual
     rebalancing during that window fights it.
     """),
]


def gen_runbooks():
    for rb_id, title, body in GOLD_RUNBOOKS:
        add(rb_id, "runbook", body, gold=True, title=title)

    rb_topics = [
        "Investigate elevated 5xx on a tenant service", "Respond to a node NotReady alert",
        "Handle a Config Sync reconcile failure", "Recover a stuck namespace deletion",
        "Respond to a quota exhaustion alert", "Diagnose an ImagePullBackOff",
        "Respond to a certificate expiry warning", "Handle a failed node pool upgrade",
        "Investigate DNS resolution failures", "Respond to a disk pressure eviction",
        "Roll back a bad deployment", "Handle a stuck PersistentVolumeClaim",
        "Investigate a metric cardinality alert", "Respond to a Binary Authorization denial",
        "Diagnose intermittent mesh 503s", "Handle a webhook admission timeout",
        "Respond to a cost anomaly ticket", "Investigate a slow API server",
        "Handle a spot preemption cascade", "Respond to a backup failure alert",
        "Grant just-in-time production access", "Execute a break-glass elevation",
        "Failover a service to another region", "Verify a restore drill",
        "Onboard a new tenant namespace", "Decommission a cluster",
        "Rotate the egress NAT IP safely", "Investigate a Gatekeeper constraint violation",
        "Handle an OOMKilled workload", "Respond to an audit log gap",
        "Diagnose a CrashLoopBackOff", "Investigate a pending pod",
        "Handle a full log bucket", "Respond to a fleet version skew alert",
        "Recover from a Config Sync drift loop", "Investigate a partner API failure",
        "Handle an expired Workload Identity binding", "Respond to a PDB blocking maintenance",
        "Diagnose a readiness probe failure",
    ]
    for i, topic in enumerate(rb_topics):
        rb_id = f"RB-{100 + i:03d}"
        svc = SERVICES[(i * 3) % len(SERVICES)]
        add(rb_id, "runbook", f"""
            Runbook {rb_id}: {topic}. Prerequisites: read access to the affected cluster and
            the owning team's on-call contact. Step 1: confirm the alert is real by checking
            the corresponding dashboard for the last six hours, and establish whether the
            condition is rising, flat or already recovering. Step 2: establish the blast
            radius — how many namespaces, how many clusters, and whether customer traffic is
            affected, using the golden-signal dashboard rather than pod-level metrics.
            Step 3: apply the immediate mitigation, which for this condition is to isolate
            the affected workload and restore capacity before diagnosing further. Step 4:
            collect diagnostics before anything is restarted: pod descriptions, recent
            events, the last 500 log lines, and the relevant metrics window. A restart
            destroys most of what the postmortem needs. Step 5: diagnose using the collected
            evidence, checking recent changes in fleet-config first, then the owning team's
            deploy history, then infrastructure events. Step 6: apply the durable fix through
            the normal change path, not by hand, so that Config Sync does not revert it.
            Step 7: if customer impact occurred, declare the incident retrospectively and
            write the postmortem. Escalation: if unresolved after 30 minutes, page the
            platform rota; if customer money movement is affected, declare Sev-1 immediately
            rather than continuing to diagnose. Related services commonly involved: {svc}.
            """, title=topic)


# ==========================================================================
# 5. Cluster exceptions — the ~55 clusters with stories
# ==========================================================================

GOLD_EXCEPTIONS = [
    ("EXC-001", "mfs-prod-use4-01", """
     mfs-prod-use4-01 is the primary card authorisation cluster and the highest-tier
     cluster in the Meridian fleet. It deviates from the golden path in four ways: it
     is pinned to the extended release channel rather than stable, it has a dedicated
     node pool with tenant isolation for the card-authoriser workload under PCI
     segmentation requirements, it holds cardholder data and therefore replicates
     backups to us-central1, and it has a dedicated egress NAT IP that three payment
     partners allowlist. It is upgraded last in every fleet upgrade, never during a
     change freeze, and any change to it requires two platform approvers. The egress IP
     is the subject of PM-2025-041 and must not be rotated without a partner allowlist
     check."""),
    ("EXC-002", "atlas-3", """
     atlas-3 is a legacy-named cluster that predates the naming convention. Despite the
     name it is located in europe-west4, not in any US region, and this has misled
     responders in at least two incidents. It runs the ledger-writer service for the
     ledger team on an in-cluster PostgreSQL StatefulSet, which is a tier-1 in-cluster
     database and therefore in direct violation of ADR-2026-019; the migration to
     Spanner is scheduled for 2026-Q4 and is the largest single item on the platform
     roadmap. It cannot be upgraded in place to Dataplane V2 and is scheduled for
     rebuild under ADR-2026-058."""),
    ("EXC-003", "mfs-prod-euw1-03", """
     mfs-prod-euw1-03 hosts card-authoriser for the European card scheme and is subject
     to EU data residency: no workload carrying meridian.io/data-class=eu-personal may
     leave europe-west1, europe-west3 or europe-west4, and this cluster is the primary
     for that traffic. It is the cluster involved in PM-2026-014, the etcd compaction
     Sev-1, and as a result it is the only cluster in the fleet with a per-namespace API
     write rate limit configured. It runs a larger control plane tier than the fleet
     default."""),
    ("EXC-004", "legacy-payments-01", """
     legacy-payments-01 runs the last remaining nginx Ingress resources in the fleet —
     eleven of them — and is the reason ADR-2026-047 sets a hard removal date of
     2026-09-30 rather than removing nginx immediately. It is in us-central1. Three of
     the eleven resources belong to teams that no longer exist, and establishing an
     owner for them is a blocker on the ingress migration. It is also one of the three
     clusters still running a manually rotated internal CA certificate."""),
    ("EXC-005", "mfs-prod-asi1-01", """
     mfs-prod-asi1-01 in asia-south1 is the only cluster in the fleet that still ran
     Velero after ADR-2025-024, because asia-south1 was the last region to receive
     Backup for GKE. It was migrated in January 2026 under ADR-2026-049 and the Velero
     installation was removed, but the Velero restore runbook is still linked from two
     team wikis. It runs kyc-verifier for the Indian market with a data residency
     constraint that is contractual rather than regulatory."""),
    ("EXC-006", "mfs-prod-sae1-02", """
     mfs-prod-sae1-02 in southamerica-east1 runs exclusively on Arm nodes (t2a), the
     first cluster fully migrated under ADR-2026-046. Any image deployed to it must be
     a multi-arch manifest; a single-arch amd64 image produces an ImagePullBackOff whose
     error text says "no matching manifest for linux/arm64" and does not otherwise
     indicate an architecture problem. This is the same failure as PM-2025-028. It runs
     the batch settlement workload for the Brazilian market."""),
    ("EXC-007", "mfs-stg-usc1-01", """
     mfs-stg-usc1-01 is the fleet's designated restore-drill target. It is rebuilt from
     backup quarterly as part of the disaster recovery programme and is therefore the
     only cluster where a full destructive restore is a routine operation rather than
     an incident. It carries no production traffic and no customer data. Do not use it
     as a general staging cluster; a restore drill will delete whatever is on it
     without notice."""),
    ("EXC-008", "mfs-prod-aus1-01", """
     mfs-prod-aus1-01 in australia-southeast1 is subject to Australian data residency:
     Australian customer data may not be processed outside australia-southeast1, and
     because Meridian operates only one Australian region, this cluster has no failover
     target. It is the single point of failure the disaster recovery programme cannot
     currently solve, and the accepted mitigation is a longer RTO for Australian
     customers, signed off at director level and reviewed annually."""),
]


def gen_exceptions():
    for exc_id, cluster, body in GOLD_EXCEPTIONS:
        add(exc_id, "exception", body, gold=True, title=f"{cluster} exception")

    reasons = [
        ("pinned to the extended release channel", "a vendor controller that is not yet certified on the stable channel version, with an expiry of {year}-Q{q}"),
        ("running a dedicated node pool with a taint", "a compliance requirement for node-level tenant isolation"),
        ("exempt from the default-deny egress policy", "a legacy partner integration that cannot route through the shared gateway"),
        ("running a third-party controller", "an approved vendor operator with a named owner in {team}"),
        ("holding a larger control plane tier", "sustained API request volume above the fleet default"),
        ("configured with local SSD node pools", "an ML workload that requires it and accepts the ephemerality"),
        ("running a regional-pd storage class", "a workload requiring synchronous cross-zone replication"),
        ("excluded from the default backup plan", "the workload is stateless and rebuilt from source in under ten minutes"),
        ("carrying a dedicated egress NAT IP", "a partner that allowlists it, recorded against this cluster in the inventory"),
        ("on a non-standard node pool shape", "a memory-bound workload that is uneconomic on the standard shape"),
    ]
    legacy_names = [
        "atlas-1", "atlas-2", "atlas-4", "atlas-5", "orion-prod", "orion-stg",
        "vega-01", "vega-02", "helios", "meridian-legacy-a", "meridian-legacy-b",
        "payments-old", "cards-old", "ledger-legacy", "kyc-pilot", "risk-pilot",
    ]
    idx = 9
    for i, legacy in enumerate(legacy_names):
        region, short = REGIONS[(i * 3) % len(REGIONS)]
        reason, why = reasons[i % len(reasons)]
        team = TEAMS[(i * 5) % len(TEAMS)]
        add(f"EXC-{idx:03d}", "exception", f"""
            {legacy} is a legacy-named cluster predating the naming convention, located in
            {region}. Its name does not encode its region or environment, so do not infer
            either from it. It is {reason}, because of {why.format(team=team, year=2026, q=(i % 4) + 1)}.
            It is owned by {team} and scheduled for rebuild under ADR-2026-058 during its
            next upgrade window.
            """, title=f"{legacy} exception")
        idx += 1

    for i in range(31):
        env = "prod" if i % 4 else "stg"
        region, short = REGIONS[(i * 7) % len(REGIONS)]
        c = cname(env, short, (i % 8) + 1)
        reason, why = reasons[i % len(reasons)]
        team = TEAMS[(i * 11) % len(TEAMS)]
        add(f"EXC-{idx:03d}", "exception", f"""
            {c} deviates from the golden path blueprint: it is {reason}, because of
            {why.format(team=team, year=2026, q=(i % 4) + 1)}. The exception is owned by
            {team}, was approved by the change advisory board, and is reviewed at the next
            annual exceptions review. Everything else about this cluster matches the
            standard blueprint.
            """, title=f"{c} exception")
        idx += 1


# ==========================================================================
# 6. Ownership and escalation — short facts, needle-in-haystack
# ==========================================================================

GOLD_OWNERSHIP = [
    ("OWN-001", "The payments-core team owns the payments-api and payment-router services. Its escalation rota is pd-payments-core and its engineering manager is the approver for any change to the payment rails. Out of hours, payments-core escalates to the platform incident commander rota, never directly to an individual."),
    ("OWN-002", "The disaster-recovery team owns the restore process, the quarterly restore drills, and the RTO and RPO commitments per service tier. They are the only team authorised to approve a production restore, and RB-004 requires their sign-off before step 1."),
    ("OWN-003", "platform-security owns Binary Authorization, the Policy Controller constraint library, the access broker, and the secret scanning pipeline. Any change under policy/ in fleet-config requires review from platform-security, a control introduced after PM-2025-033."),
    ("OWN-004", "platform-networking owns the egress gateways, the NAT IPs, the Gateway API configuration, Cloud Service Mesh and Dataplane V2. They are the approvers for any subnet or NAT change, and since PM-2025-041 a NAT IP change additionally requires a partner allowlist check."),
    ("OWN-005", "The ledger team owns the ledger-writer and ledger-reader services and the in-cluster PostgreSQL on atlas-3. They own the Spanner migration scheduled for 2026-Q4. Their rota is pd-ledger and they are a two-person team, which is itself a documented risk in the annual review."),
]


def gen_ownership():
    for own_id, body in GOLD_OWNERSHIP:
        add(own_id, "ownership", body, gold=True, title=own_id)

    idx = 6
    for team in TEAMS:
        owned = [SERVICES[(TEAMS.index(team) * 3 + k) % len(SERVICES)] for k in range(rng.randint(1, 3))]
        city = CITIES[TEAMS.index(team) % len(CITIES)]
        add(f"OWN-{idx:03d}", "ownership",
            f"The {team} team owns {', '.join(owned)}. Its escalation rota is pd-{team}, "
            f"its cost centre is CC-{4000 + TEAMS.index(team)}, and its engineering "
            f"manager is based in {city}. Namespaces owned by this team carry "
            f"meridian.io/team={team}.", title=f"{team} ownership")
        idx += 1

    # Per-namespace ownership rows — the fine-grained needles.
    for i in range(135):
        team = TEAMS[i % len(TEAMS)]
        svc = SERVICES[(i * 13) % len(SERVICES)]
        region, short = REGIONS[(i * 5) % len(REGIONS)]
        env = ENVS[i % 4]
        add(f"OWN-{idx:03d}", "ownership",
            f"Namespace {team}-{svc} in {cname(env, short, (i % 9) + 1)} is owned by "
            f"{team}, escalates to pd-{team}, and is billed to CC-{4000 + (i % 40)}. "
            f"Data class is {rng.choice(['internal', 'confidential', 'eu-personal', 'cardholder'])}.",
            title=f"{team}-{svc} ownership")
        idx += 1


# ==========================================================================
# 7. Routine inventory — the boring 450
# ==========================================================================

def gen_inventory():
    n = 0
    for env in ["prod", "stg", "dev", "sbx"]:
        per_env = {"prod": 160, "stg": 110, "dev": 100, "sbx": 80}[env]
        for i in range(per_env):
            region, short = REGIONS[(i * 7 + len(env)) % len(REGIONS)]
            c = cname(env, short, (i % 12) + 1)
            team = TEAMS[(i * 3) % len(TEAMS)]
            svc = SERVICES[(i * 5) % len(SERVICES)]
            n += 1
            add(f"INV-{n:04d}", "inventory",
                f"{c} is a golden-path cluster in {region} running {svc} for {team}. "
                f"Release channel {'stable' if env == 'prod' else 'regular' if env == 'stg' else 'rapid'}, "
                f"Dataplane V2, {rng.randint(3, 40)} nodes, no exceptions on record.",
                title=c)


# ==========================================================================
# 8. Known issues and gotchas — symptom-keyed tribal knowledge
# ==========================================================================

GOLD_GOTCHAS = [
    ("GOT-001", """If a pod is stuck in ContainerCreating with an event mentioning a
     volume mount timeout and the namespace uses the Secret Manager CSI driver, check
     whether the CSI node pod is running on that node before anything else. A missing
     CSI node pod causes the mount to hang indefinitely rather than fail, so there is no
     error to find — the pod just never starts. This is PM-2026-009."""),
    ("GOT-002", """An ImagePullBackOff whose message is "no matching manifest" is an
     architecture mismatch, not a permissions problem. The error text does not contain
     the word architecture and does not mention arm64 unless you read the full manifest
     list error. Check the node's kubernetes.io/arch label against the image's manifest
     before checking registry permissions; two incidents have been spent the other way
     round."""),
    ("GOT-003", """A CronJob that completes successfully every night can still be running
     at the wrong time. If the schedule has no explicit timeZone field it inherits the
     cluster default, which changed for clusters rebuilt after March 2026. Every
     success-based monitor stays green. Assert freshness at the consumer, per
     PM-2026-021."""),
    ("GOT-004", """Intermittent 503s at roughly one request in N, where N is the replica
     count, almost always means one pod out of N is different from the others — usually
     missing a sidecar after a mesh STRICT flip. It reads as flakiness. Count the pods
     with two containers against the total before investigating anything else."""),
    ("GOT-005", """A Job whose pods cannot be admitted because of a ResourceQuota
     produces its event on the ReplicaSet, not on the Job. kubectl describe job shows
     nothing useful. This is why quota exhaustion looks like a scheduler problem and
     costs an hour every time."""),
]


def gen_gotchas():
    for got_id, body in GOLD_GOTCHAS:
        add(got_id, "gotcha", body, gold=True, title=got_id)

    symptoms = [
        ("Pods evicted with no obvious memory pressure", "check ephemeral storage; application logs written to the container filesystem fill the node disk and the eviction message names memory only on some kubelet versions"),
        ("Config Sync reports success but the resource is missing", "a mutating webhook is stripping a field, so Config Sync sees its own applied state as correct while the API server stores something else"),
        ("A Deployment rollout that never completes", "the new ReplicaSet cannot schedule and the old one is held by a PDB; check both, not just the new one"),
        ("Service endpoints containing terminating pods", "terminationGracePeriodSeconds is longer than the endpoint controller's removal, so drain the endpoint in a preStop hook"),
        ("Sudden metric gaps during an incident", "the metrics pipeline is often the second casualty; check whether the collector was itself evicted before concluding the workload stopped"),
        ("NetworkPolicy that appears not to apply", "Dataplane V2 evaluates policy differently from the legacy dataplane for hostNetwork pods; confirm the pod is not hostNetwork"),
        ("A PVC stuck Pending in a regional cluster", "the storage class is zonal and the pod is constrained to a zone with no capacity; check the volumeBindingMode"),
        ("HPA reporting unknown metrics", "the custom metrics adapter lost its Workload Identity binding during an IAM cleanup; the HPA reports unknown rather than erroring"),
        ("Admission webhook timeouts under load", "the webhook backend is in the cluster it admits for, so a cluster-wide problem makes admission fail closed"),
        ("kubectl exec failing on a healthy pod", "distroless and Chainguard images have no shell; use kubectl debug with an ephemeral container"),
        ("A node that will not drain", "a pod with no controller cannot be evicted safely and blocks the drain; look for bare pods before PDBs"),
        ("Cluster autoscaler not scaling up", "a pod with a nodeSelector no pool satisfies is unschedulable rather than a scale-up trigger; check the events on the pod"),
        ("Backup plan reporting PARTIALLY_SUCCEEDED", "one volume is in a zone or storage class the plan does not cover; a partial backup is not a usable restore point"),
        ("Sidecar consuming more memory than the application", "endpoint count drives sidecar memory; a service with thousands of upstreams needs a scoped Sidecar resource"),
        ("Binary Authorization denying a known-good image", "the attestation is on the digest, not the tag; a retagged image has no attestation"),
        ("A CronJob that fires twice", "two clusters running the same Config Sync directory; the job is not the problem, the fleet-config selector is"),
        ("Slow DNS in one namespace only", "NodeLocal DNSCache is per-node, so a single unhealthy cache affects only the pods on that node, which correlates to a namespace if it is not spread"),
        ("Requests failing only for large payloads", "a proxy body size limit differing between the nginx path and the Gateway API path during the ingress migration"),
        ("Workload Identity token errors after a namespace recreate", "the Kubernetes service account UID changed and the IAM binding references the old one"),
        ("Persistent 429s from the registry", "the workload is pulling from upstream rather than the mirror; check the image reference, not the quota"),
    ]
    idx = 6
    for i in range(195):
        sym, fix = symptoms[i % len(symptoms)]
        svc = SERVICES[(i * 7) % len(SERVICES)]
        add(f"GOT-{idx:03d}", "gotcha",
            f"Symptom: {sym.lower()}, seen most often with {svc}. Cause: {fix}. "
            f"This has come up {rng.randint(2, 9)} times and is worth checking before "
            f"opening a support case.", title=sym)
        idx += 1


# ==========================================================================
# 9. Capacity, quota and cost
# ==========================================================================

GOLD_CAPACITY = [
    ("CAP-001", """The fleet's total committed use discount covers 12,000 vCPU and 48TB of
     memory across all regions, renewing 2027-01-31. Current utilisation against the
     commitment is 87%. Anything that would take the fleet below 80% utilisation of the
     commitment is a cost regression even if it reduces absolute spend, because the
     commitment is paid regardless."""),
    ("CAP-002", """mfs-prod-use4-01 has 11% quota headroom against its regional CPU quota,
     which is below the 15% review threshold. A quota increase request for an additional
     2,000 vCPU in us-east4 was filed on 2026-06-18 and is still pending with Google
     Cloud support. This is the single largest capacity risk in the fleet and it is
     tracked weekly."""),
    ("CAP-003", """The fleet spends approximately 2.1 million dollars a month on GKE
     compute, of which 31% is production, 22% staging, 14% dev, 4% sandbox, and 29% is
     idle headroom charged to the platform team under the cost allocation policy. The
     idle figure has been flat for three quarters despite two efficiency programmes."""),
]


def gen_capacity():
    for cap_id, body in GOLD_CAPACITY:
        add(cap_id, "capacity", body, gold=True, title=cap_id)

    idx = 4
    for i in range(107):
        region, short = REGIONS[i % len(REGIONS)]
        env = ENVS[i % 4]
        c = cname(env, short, (i % 10) + 1)
        team = TEAMS[(i * 3) % len(TEAMS)]
        add(f"CAP-{idx:03d}", "capacity",
            f"{c} in {region}: {rng.randint(16, 92)}% CPU quota utilisation, "
            f"{rng.randint(12, 88)}% memory quota utilisation, "
            f"{rng.randint(15, 60)}% headroom against the zone-loss target. "
            f"Largest tenant is {team} at {rng.randint(20, 70)}% of the cluster. "
            f"Monthly cost approximately {rng.randint(4, 90)},{rng.randint(100, 999)} dollars. "
            f"Reviewed 2026-{rng.randint(1, 7):02d}-{rng.randint(1, 28):02d}.",
            title=f"{c} capacity")
        idx += 1


# ==========================================================================
# 10. Migrations and change log — temporal
# ==========================================================================

GOLD_MIGRATIONS = [
    ("MIG-001", """The Atlas migration ran from 2024-02 to 2025-04 and moved Meridian from
     41 hand-built clusters to the current fleet-config managed estate. It is the origin
     of the naming convention, the legacy-named exceptions that remain, and the
     shared-ownership category that ADR-2024-006 later removed. Sixteen legacy-named
     clusters survive it and are scheduled for rebuild under ADR-2026-058."""),
    ("MIG-002", """The Dataplane V2 migration completed on 2026-01-19 for all clusters
     except atlas-3, which cannot be migrated in place and is waiting on the Spanner
     migration before it can be rebuilt. Until atlas-3 is rebuilt, the fleet-wide claim
     that all clusters run Dataplane V2 is false by exactly one cluster, and the PCI
     segmentation evidence carries a documented exception for it."""),
    ("MIG-003", """The ingress migration from nginx to Gateway API began 2025-03-17 under
     ADR-2025-019 and stalled at eleven remaining Ingress resources, all on
     legacy-payments-01, for nine months. ADR-2026-047 set a hard removal date of
     2026-09-30. Three of the eleven belong to teams that no longer exist and finding an
     owner is the current blocker."""),
]


def gen_migrations():
    for mig_id, body in GOLD_MIGRATIONS:
        add(mig_id, "migration", body, gold=True, title=mig_id)

    changes = [
        "raised the default HPA stabilisation window", "moved the batch pool to Arm",
        "enabled Managed Prometheus", "removed the last Velero installation",
        "enforced Binary Authorization in staging", "rotated the internal CA",
        "raised log retention to the current policy", "migrated to the Secret Manager CSI driver",
        "removed the External Secrets Operator", "enabled cost anomaly detection",
        "upgraded to the current minor version", "added a dedicated egress IP",
        "split the general node pool", "enabled Backup for GKE",
        "moved to the Chainguard base image", "enabled mesh STRICT mTLS",
        "applied the new ResourceQuota baseline", "migrated to multi-cluster Services",
        "enabled Config Sync repo sync", "removed a deprecated CRD version",
    ]
    idx = 4
    for i in range(127):
        change = changes[i % len(changes)]
        env = ENVS[i % 4]
        region, short = REGIONS[(i * 5) % len(REGIONS)]
        c = cname(env, short, (i % 11) + 1)
        team = TEAMS[(i * 7) % len(TEAMS)]
        month = (i % 12) + 1
        year = 2025 if i % 3 == 0 else 2026
        add(f"MIG-{idx:03d}", "migration",
            f"Change log {year}-{month:02d}-{(i % 27) + 1:02d}: {c} {change}. "
            f"Requested by {team}, executed by the platform team in the "
            f"{'Tuesday' if i % 2 else 'Thursday'} change window, no customer impact. "
            f"Rollback plan was tested in staging beforehand and not needed.",
            title=f"{c} {change}")
        idx += 1


# ==========================================================================
# 11. Version and deprecation landscape
# ==========================================================================

GOLD_DEPRECATIONS = [
    ("DEP-001", """ingress-nginx is withdrawn from the Meridian fleet on 2026-09-30 under
     ADR-2026-047. Eleven Ingress resources remain, all on legacy-payments-01. On the
     removal date the nginx deployments are deleted whether or not the migration is
     complete, and any remaining Ingress resource stops serving. This is the nearest
     hard deadline in the deprecation register."""),
    ("DEP-002", """The debian-slim shared base image is withdrawn on 2026-10-01 under
     ADR-2026-051. Fourteen services still build on it. After that date the image is
     removed from the registry mirror and their builds fail rather than producing a
     stale image, which is deliberate."""),
    ("DEP-003", """The key-rotation job was decommissioned on 2026-04-20 with ADR-2026-052.
     Any runbook or alert still referencing it is stale. Two team wikis still document
     the rotation procedure, and following it will fail because there is no longer any
     approved path to issuing a service account key."""),
]


def gen_deprecations():
    for dep_id, body in GOLD_DEPRECATIONS:
        add(dep_id, "deprecation", body, gold=True, title=dep_id)

    items = [
        "the legacy metrics adapter", "the v1beta1 CRD versions", "the self-managed Istio charts",
        "the shared egress gateway for partner traffic", "the manual certificate rotation procedure",
        "the zonal storage class", "the old cost allocation report", "the pre-Gateway ingress annotations",
        "the docker runtime node images", "the External Secrets Operator",
        "the Velero restore runbook", "the shared-ownership namespace category",
        "the legacy cluster naming convention", "the dashboard-only etcd size monitor",
        "the per-cluster Prometheus pairs", "the manual quota request spreadsheet",
        "the v1 PodSecurityPolicy shims", "the old fleet-config directory layout",
        "the unsigned image allowance in staging", "the calendar-based expiry reminders",
    ]
    idx = 4
    for i in range(52):
        item = items[i % len(items)]
        add(f"DEP-{idx:03d}", "deprecation",
            f"{item.capitalize()} is deprecated with a removal date of "
            f"2026-{rng.randint(8, 12):02d}-{rng.randint(1, 28):02d}. "
            f"{rng.randint(1, 26)} workloads across {rng.randint(1, 14)} clusters still "
            f"depend on it. Owners are notified monthly and the inventory is regenerated "
            f"weekly from live cluster state rather than from a static list, because a "
            f"static list was wrong the last three times.", title=item)
        idx += 1


# ==========================================================================
# 12. Per-user personal facts — the isolation probe under load
# ==========================================================================

def gen_users():
    roles = ["SRE", "platform engineer", "tenant lead", "security engineer",
             "release engineer", "data platform engineer", "network engineer"]
    styles = [
        "prefer a written summary before any live debugging session",
        "want raw kubectl output rather than a prose summary",
        "ask for the blast radius before any change is proposed",
        "always want a dry-run diff first",
        "read YAML faster than tables and prefer manifests inline",
        "want cost implications stated up front on any scaling suggestion",
        "prefer three options with trade-offs rather than one recommendation",
        "want links to the runbook rather than the steps pasted in",
    ]
    hours = [
        "work 07:00-15:00 local and am offline after that",
        "am on the European on-call rotation every third week",
        "block Wednesday afternoons for deep work and do not want to be paged then",
        "start late and am usually available until 19:00 local",
        "work a compressed week and am off on Fridays",
        "am on secondment to the reliability team until the end of the quarter",
    ]
    for i, u in enumerate(USERS):
        city = CITIES[i % len(CITIES)]
        team = TEAMS[(i * 3) % len(TEAMS)]
        region, short = REGIONS[i % len(REGIONS)]
        c = cname("prod" if i % 2 else "stg", short, (i % 9) + 1)
        ns = f"{team}-{SERVICES[(i * 5) % len(SERVICES)]}"
        add(f"USR-{i+1:03d}-a", "user", f"I am based in {city} and I work as a "
            f"{roles[i % len(roles)]} on the {team} team.", scope=f"user:{u}")
        add(f"USR-{i+1:03d}-b", "user", f"I {styles[i % len(styles)]}.", scope=f"user:{u}")
        add(f"USR-{i+1:03d}-c", "user", f"I {hours[i % len(hours)]}.", scope=f"user:{u}")
        add(f"USR-{i+1:03d}-d", "user", f"My day-to-day work is on {c} and my default "
            f"namespace there is {ns}.", scope=f"user:{u}",
            gold=(u in ("user07", "user23")))
        add(f"USR-{i+1:03d}-e", "user", f"When I say 'my cluster' I mean {c}. "
            f"When I ask about costs I mean cost centre CC-{4000 + (i % 40)}.",
            scope=f"user:{u}")


# ==========================================================================
# Query set — the answer key
# ==========================================================================

def gen_queries():
    # --- Class 1: supersession / staleness -------------------------------
    for chain in SUPERSESSION_CHAINS:
        q, must, must_not = chain["probe"]
        ids = [d[0] for d in chain["docs"]]
        ask(f"Q-SUP-{ids[-1]}", "supersession", q, [ids[-1]], must, must_not,
            note=f"Three versions of the {chain['topic']} policy are in the corpus. "
                 f"{ids[-1]} is current and supersedes {ids[0]} and {ids[1]}. Answering "
                 f"with a superseded value is the failure, and it is a confident failure.")

    # --- Class 2: procedural fidelity ------------------------------------
    ask("Q-PROC-RB011", "procedural",
        "Walk me through responding to the etcd database size alert, in order.",
        ["RB-011"],
        ["identify the writer", "scale the offending controller to zero", "eleven minutes"],
        ["delete it"],
        note="Six ordered steps with an explicit do-not-skip. Tests whether the step "
             "sequence and the 'scale to zero, do not delete' distinction survive.")
    ask("Q-PROC-RB004", "procedural",
        "What is the procedure for restoring a production cluster from backup?",
        ["RB-004"],
        ["scratch cluster", "scale the production workloads in the target namespaces to zero",
         "incident commander"],
        [],
        note="Eight steps where step 5 prevents split-brain. Losing step 5 turns a "
             "documented procedure into a data-corruption event.")
    ask("Q-PROC-RB027", "procedural",
        "How do I safely flip a namespace from PERMISSIVE to STRICT mTLS?",
        ["RB-027"],
        ["every pod in the namespace has a sidecar", "seven days"],
        [],
        note="The verification step is the entire value of the runbook.")
    ask("Q-PROC-RB019", "procedural",
        "A credential has leaked. What do I do?",
        ["RB-019"],
        ["Revoke first", "do not paste the credential"],
        [],
        note="Order matters more here than content: revoke-before-investigate.")

    # --- Class 3: synthesis across documents -----------------------------
    ask("Q-SYN-001", "synthesis",
        "We are about to rotate the egress NAT IP on the payments cluster. What should I know?",
        ["PM-2025-041", "EXC-001", "ADR-2026-033", "OWN-004"],
        ["allowlist", "partner"],
        [],
        note="The answer lives in four documents in four categories and in none of them "
             "alone. This is the case Hindsight should win.")
    ask("Q-SYN-002", "synthesis",
        "What do I need to check before deploying a new image to a batch node pool?",
        ["ADR-2026-046", "PM-2025-028", "GOT-002", "EXC-006"],
        ["multi-arch"],
        [],
        note="A policy, an incident, a gotcha and a cluster exception all bear on this.")
    ask("Q-SYN-003", "synthesis",
        "Why is atlas-3 a problem and what is being done about it?",
        ["EXC-002", "MIG-002", "ADR-2026-019", "ADR-2026-058"],
        ["europe-west4", "Spanner"],
        [],
        note="Requires joining a cluster exception, a migration status, and two policies. "
             "Also tests the misleading-name trap: atlas-3 is not in a US region.")
    ask("Q-SYN-004", "synthesis",
        "A policy change is about to go into fleet-config. What is the safe way to do it?",
        ["PM-2025-033", "OWN-003", "CONV-011"],
        ["dryrun"],
        [],
        note="The dryrun-first rule exists only as an action item inside a postmortem.")

    # --- Class 4: needle in haystack -------------------------------------
    ask("Q-NDL-001", "needle",
        "Is there a pending quota increase request anywhere in the fleet?",
        ["CAP-002"],
        ["us-east4", "2,000"],
        [],
        note="One document out of 1,400 mentions a pending request.")
    ask("Q-NDL-002", "needle",
        "Which cluster is the restore-drill target?",
        ["EXC-007"],
        ["mfs-stg-usc1-01"],
        [],
        note="Single-document fact with an operational consequence if wrong.")
    ask("Q-NDL-003", "needle",
        "Which cluster has no failover target, and why?",
        ["EXC-008"],
        ["australia-southeast1"],
        [],
        note="Single document; the reason matters as much as the identity.")
    ask("Q-NDL-004", "needle",
        "What is the nearest hard deprecation deadline?",
        ["DEP-001"],
        ["2026-09-30"],
        [],
        note="Requires comparing dates across the deprecation register.")

    # --- Class 5: aggregation --------------------------------------------
    ask("Q-AGG-001", "aggregation",
        "How many clusters deviate from the golden path, and what are the main reasons?",
        ["CONV-002"],
        [],
        [],
        note="Graded qualitatively. Both providers are expected to struggle; the "
             "question is whether the failure is a wrong number stated confidently.")
    ask("Q-AGG-002", "aggregation",
        "Which teams own services that hold cardholder data?",
        [],
        [],
        [],
        note="Requires scanning ownership rows for a data class. Graded qualitatively.")

    # --- Class 6: temporal / recency -------------------------------------
    ask("Q-TMP-001", "temporal",
        "What changed in the fleet in the last six months that I should know about?",
        ["ADR-2026-052", "ADR-2026-049", "MIG-002"],
        [],
        [],
        note="Recency ordering. Neither store has a first-class notion of 'recent'.")
    ask("Q-TMP-002", "temporal",
        "Has the Dataplane V2 migration finished?",
        ["MIG-002"],
        ["atlas-3"],
        [],
        note="The honest answer is 'yes except one cluster'. A flat yes is wrong and is "
             "the answer a summary is most likely to give.")

    # --- Class 7: isolation under load -----------------------------------
    #
    # The canaries are per-user staging clusters and namespaces, verified absent
    # from every shared document. The obvious strings are not usable: "Dublin"
    # and "user07" both appear in shared ownership records ("its engineering
    # manager is based in Dublin"), so a probe built on them fires on correct
    # behaviour and reports it as a leak.
    ask("Q-ISO-001", "isolation",
        "What do you know about my preferences?",
        [], [], ["mfs-stg-ase1-07", "mfs-stg-sae1-05", "notifications-vector-index"],
        note="Run as an unrelated identity. Any other user's fact appearing is a leak, "
             "not a measurement. Canaries are user-unique cluster/namespace names.",
        as_user="probe-operator")
    ask("Q-ISO-002", "isolation",
        "Who works on mfs-prod-euw1-03?",
        [], [], ["mfs-stg-ase1-07", "mfs-stg-sae1-05", "search-vector-index"],
        note="Sharpest leak probe: a personal fact about a shared object, phrased so a "
             "leak looks like a helpful answer.", as_user="probe-operator")

    # --- Class 8: knowing it does not know -------------------------------
    #
    # scored_at="answer": hallucination is a property of the reply. Both of
    # these ask about an identifier that does not exist, and the only honest
    # judge is the model's own output in #105.
    ask("Q-NEG-001", "negative",
        "What is mfs-prod-euw1-99 used for?",
        [], [], ["mfs-prod-euw1-99 is", "mfs-prod-euw1-99 runs", "mfs-prod-euw1-99 hosts"],
        note="No such cluster. A confident invented answer reframes every result above.",
        scored_at="answer")
    ask("Q-NEG-002", "negative",
        "What did we decide in ADR-2026-099?",
        [], [], ["ADR-2026-099 decided", "ADR-2026-099 states", "ADR-2026-099 supersedes"],
        note="No such ADR, but the corpus is full of documents that look like one.",
        scored_at="answer")


# ==========================================================================
# Rung assignment and emission
# ==========================================================================

def assign_rungs():
    """Gold documents first, then filler interleaved across categories.

    Interleaving matters: taking filler in generation order would make rung 150
    almost entirely conventions and ADRs, so the small rungs would measure a
    different corpus rather than a smaller one.
    """
    # Anything a query names as a gold document is gold, whether or not it was
    # flagged at creation. Marking by hand in two places drifts; the query set is
    # the single source of truth for what has to be in the first rung.
    targeted = {g for q in QUERIES for g in q["gold_docs"]}
    for d in DOCS:
        if d["id"] in targeted:
            d["gold"] = True

    # The ladder is over *shared* documents. All 250 per-user facts sit at rung 0
    # and are present at every rung, for two reasons: the corpus being scaled is
    # the fleet's shared knowledge, not its user count; and the isolation probe
    # needs the same 50 users to leak from at rung 100 as at the top, or a clean
    # result at a small rung would just mean there was nobody to leak.
    for d in DOCS:
        if d["scope"] != "shared":
            d["rung"] = 0

    shared = [d for d in DOCS if d["scope"] == "shared"]
    RUNGS[-1] = len(shared)

    gold = [d for d in shared if d["gold"]]
    filler = [d for d in shared if not d["gold"]]

    by_cat = {}
    for d in filler:
        by_cat.setdefault(d["category"], []).append(d)
    for v in by_cat.values():
        rng.shuffle(v)

    order = list(gold)
    cats = sorted(by_cat)
    cursor = {c: 0 for c in cats}
    while len(order) < len(shared):
        placed = False
        for c in cats:
            if cursor[c] < len(by_cat[c]):
                order.append(by_cat[c][cursor[c]])
                cursor[c] += 1
                placed = True
                if len(order) >= len(shared):
                    break
        if not placed:
            break

    if len(gold) > RUNGS[0]:
        raise SystemExit(f"{len(gold)} gold docs exceed the smallest rung {RUNGS[0]}; "
                         "the first rung must contain every probe target")

    for i, d in enumerate(order):
        d["rung"] = next((r for r in RUNGS if i < r), RUNGS[-1])
        d["order"] = i
    for i, d in enumerate(d for d in DOCS if d["scope"] != "shared"):
        d["order"] = -1 - i
    return order + [d for d in DOCS if d["scope"] != "shared"]


def emit(outdir, order):
    corpus_dir = outdir / "corpus"
    corpus_dir.mkdir(parents=True, exist_ok=True)
    for f in corpus_dir.glob("*.md"):
        f.unlink()

    by_cat = {}
    for d in order:
        by_cat.setdefault(d["category"], []).append(d)

    for cat, docs in sorted(by_cat.items()):
        lines = [f"# {cat}", ""]
        for d in docs:
            lines.append(f"<!-- id: {d['id']} -->")
            lines.append(f"<!-- scope: {d['scope']} -->")
            lines.append(f"<!-- rung: {d['rung']} -->")
            lines.append(f"<!-- order: {d['order']} -->")
            if d["title"]:
                lines.append(f"<!-- title: {d['title']} -->")
            lines.append("")
            lines.append(d["text"])
            lines.append("")
        (corpus_dir / f"{cat}.md").write_text("\n".join(lines), encoding="utf-8")

    (outdir / "queries.json").write_text(
        json.dumps({"queries": QUERIES}, indent=2, ensure_ascii=False), encoding="utf-8")

    total_chars = sum(len(d["text"]) for d in order)
    manifest = {
        "seed": SEED,
        "rungs": RUNGS,
        "total_documents": len(order),
        "gold_documents": sum(1 for d in order if d["gold"]),
        "total_chars": total_chars,
        "approx_tokens": total_chars // 4,
        "by_category": {c: len(v) for c, v in sorted(by_cat.items())},
        # by_rung counts shared documents only — that is what the ladder scales.
        # total_by_rung adds the 250 always-present user facts, and is the number
        # that actually reaches a prompt.
        "by_rung": {str(r): sum(1 for d in order if 0 < d["rung"] <= r) for r in RUNGS},
        "total_by_rung": {str(r): sum(1 for d in order if d["rung"] <= r) for r in RUNGS},
        "chars_by_rung": {
            str(r): sum(len(d["text"]) for d in order if d["rung"] <= r) for r in RUNGS},
        "tokens_by_rung": {
            str(r): sum(len(d["text"]) for d in order if d["rung"] <= r) // 4 for r in RUNGS},
        "shared_documents": sum(1 for d in order if d["scope"] == "shared"),
        "user_documents": sum(1 for d in order if d["scope"].startswith("user:")),
        "queries": len(QUERIES),
        "query_classes": {},
        "documents": [{"id": d["id"], "category": d["category"], "scope": d["scope"],
                       "rung": d["rung"], "order": d["order"], "gold": d["gold"],
                       "chars": len(d["text"])} for d in order],
    }
    for q in QUERIES:
        manifest["query_classes"][q["class"]] = manifest["query_classes"].get(q["class"], 0) + 1
    (outdir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def validate(order):
    """Fail loudly rather than measuring a broken corpus."""
    problems = []
    ids = {d["id"] for d in order}

    for q in QUERIES:
        for g in q["gold_docs"]:
            if g not in ids:
                problems.append(f"{q['id']}: gold doc {g} does not exist")
                continue
            doc = next(d for d in order if d["id"] == g)
            if doc["rung"] not in (0, RUNGS[0]):
                problems.append(f"{q['id']}: gold doc {g} is at rung {doc['rung']}, "
                                f"must be {RUNGS[0]} (or 0 for an always-present user fact)")

    # Every must_contain string has to actually appear in a gold document,
    # otherwise the probe is unanswerable and scores zero for reasons that have
    # nothing to do with the provider.
    for q in QUERIES:
        if not q["gold_docs"]:
            continue
        blob = " ".join(d["text"] for d in order if d["id"] in q["gold_docs"]).lower()
        for m in q["must_contain"]:
            if m.lower() not in blob:
                problems.append(f"{q['id']}: must_contain {m!r} absent from its gold docs")

    for pat in ("I wants", "I works", "I prefer a written summary before any live debugging session."):
        bad = [d["id"] for d in order if pat in d["text"] and pat.startswith("I w")]
        if bad:
            problems.append(f"grammar: {pat!r} in {bad[:3]}")

    if problems:
        for p in problems:
            print(f"  FAIL {p}")
        raise SystemExit(f"{len(problems)} corpus validation failures")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="/tmp/scaletest/v2")
    a = ap.parse_args()

    gen_conventions()
    gen_adrs()
    gen_postmortems()
    gen_runbooks()
    gen_exceptions()
    gen_ownership()
    gen_inventory()
    gen_gotchas()
    gen_capacity()
    gen_migrations()
    gen_deprecations()
    gen_users()
    gen_queries()

    order = assign_rungs()
    validate(order)
    m = emit(Path(a.out), order)

    print(f"documents      {m['total_documents']}  ({m['gold_documents']} gold)")
    print(f"size           {m['total_chars']:,} chars  (~{m['approx_tokens']:,} tokens)")
    print(f"queries        {m['queries']}  {m['query_classes']}")
    print("by category   ", m["by_category"])
    print(f"shared/user    {m['shared_documents']} shared, {m['user_documents']} user (always present)")
    print("shared by rung ", m["by_rung"])
    print("total by rung  ", m["total_by_rung"])
    print("tokens by rung ", {k: f"{v:,}" for k, v in m["tokens_by_rung"].items()})


if __name__ == "__main__":
    main()
