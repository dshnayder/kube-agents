# Evaluation: Can Scion replace (or host) kube-agents?

**Status:** Evaluation / recommendation
**Date:** 2026-07-22
**Scope:** Whether [Scion](https://github.com/GoogleCloudPlatform/scion) can host the kube-agents Hermes agents and take over orchestration + communication — including deployment topology, security posture, and fitness (vs. alternatives) as a mechanism for **spawning delegated subagents, each on its own pod with its own KSA**. Options evaluated: Scion, Agent Substrate, AX-on-Substrate, DIY read-only Jobs, extending the existing operator (**recommended**), and SIG Agent Sandbox.
**Method:** Verified against source — Scion (`github.com/GoogleCloudPlatform/scion` @ `b4c9911`), Hermes (`github.com/nousresearch/hermes-agent`), [Agent Substrate](https://github.com/agent-substrate/substrate), [Agent Executor / AX](https://github.com/google/ax), and the kube-agents source in this repo. **Not** taken from published docs, which were found inaccurate on several points.

> ⚠️ Naming note: this "Scion" is **not** the SCION networking project (`scionproto/scion`). It is a Google Cloud AI-agent orchestration platform that happens to share the name.

---

## TL;DR

- **Content portability is easy.** Our Hermes platform-agent content — SOUL.md, skills, config.yaml, MCP tools, plugin tools — ports onto Scion with **minor rework**, confirmed from both the Hermes and Scion source.
- **Scion can run in the management cluster**, Hub included, in a shape close to kube-agents (a Hub Deployment + PVC, a Broker Deployment, on-demand agent pods).
- **The decisive issue is security/RBAC.** Scion's runtime is fundamentally a **mutating** actor: running any agent requires `create pods` + **`pods/exec`** (plus `create/delete secrets` and PVCs). There is **no read-only, GitOps, or exec-free mode**. This is categorically incompatible with kube-agents' hard requirement of a read-only cluster posture where all mutations flow through GitOps.
- **Genuine pros exist**, concentrated in **per-agent isolation, fleet-scale concurrency, and extensibility** — which are strongest for the *ephemeral subagent* tier, not the privileged platform tier.
- **For the specific "platform agent plans → delegates to spawned task-workers" pattern, Scion is the wrong-shaped tool.** Its orchestration surface is unused (planning/delegation live in the platform agent), and its create+exec spawn is the un-offset security con.
- **The decisive requirement — each subagent on its own pod with its own KSA — reorders everything.** A KSA is per-Pod, so this needs pod-per-agent. **Substrate/AX are incompatible** (they multiplex actors onto shared pods). The recommended answer is **not a new platform at all**: extend the **existing kube-agents operator** (which already creates per-agent SAs) to render each subagent as its own read-only Job/Deployment with its own KSA. See [Identity](#identity-per-agent-ksa-the-decisive-requirement) and [Recommended design](#recommended-design-per-agent-ksa-via-the-existing-operator).

**Recommendation:** For "each subagent on its own pod with its own KSA," **extend the existing operator** to spawn per-agent, per-KSA, read-only Jobs/Deployments from an `AgentTask` CRD, running the existing Hermes image via its CLI — no new platform, no `pods/exec`, self-healing, LLM kept out of the spawn path. **Substrate/AX** are incompatible with per-agent KSA; **Scion** can do it but only by importing a Hub/Broker + `create pods` + `pods/exec`. Keep the read-only platform tier + GitOps write path unchanged. See [Recommendation](#recommendation).

**Options covered in this doc:** Scion (host / spawn), Agent Substrate, AX-on-Substrate, DIY read-only Jobs, **extend-the-operator (recommended)**, and SIG Agent Sandbox (future).

---

## Summary of findings

| Dimension | Finding | Verdict |
|---|---|---|
| Run our Hermes content on Scion | `hermes chat` honors SOUL.md/config/skills/MCP/plugin-tools from `$HERMES_HOME`, which Scion populates | ✅ **Minor rework** |
| Add new agent/persona types | each persona = a Scion template (image + prompt + tools) | ✅ **Straightforward** |
| Long-running agents + scheduled monitoring | Scion agents are persistent daemons; `dispatch_agent` cron scheduler exists | ✅ **Supported** |
| Deploy in the management cluster (Hub included) | Hub/Broker are containers; run as Deployments; agent pods on demand | ✅ **Viable** |
| Read-only RBAC / GitOps-only mutations | runtime requires `create pods` + `pods/exec`; no read-only mode | ❌ **Incompatible** (decisive con) |
| Self-healing on eviction | agent pods are bare Pods, `RestartPolicy: Never`, no reconcile | ❌ **No** |
| Google Chat ingress | non-functional stub (Slack/Telegram/Discord work) | ❌ **Missing** |
| Per-agent isolation / tenancy | own container, credentials, worktree, locked SecurityContext | ✅ **Strong (a pro)** |
| **Per-agent KSA (Scion)** | pod-per-agent; `serviceAccountName` per workload | ✅ native (but via create+exec) |
| **Per-agent KSA (Substrate/AX)** | actors multiplex onto shared pods; no `serviceAccountName` field | ❌ **Incompatible** |
| **Per-agent KSA (recommended)** | **extend the existing operator** → per-agent read-only Job/Deployment with its own KSA | ✅ **Best fit, no new platform** |

---

## Background: what each system is

**kube-agents** is the system being served — the *consumer*. **Scion**, **Agent Substrate**, and **AX** are the three candidate *runtimes* that could host or spawn its agents. Note the three are not fully parallel: Scion is a full standalone platform; Substrate is a low-level Kubernetes runtime; AX is a thin execution layer that runs *on top of* Substrate.

**kube-agents** — an agentic harness that replaces `kubectl`/`gcloud`/Console with intent-driven agents for GKE fleet operations. Its runtime unit is a single **long-running Hermes gateway pod** (operator-managed Deployment) hosting multiple **profiles**: a Chat Agent front door, a privileged Platform Agent, and dynamically scaffolded per-cluster Cluster Agents. Delegation runs through a **kanban dispatcher** and file-based **handover** channel; chat reaches humans through Hermes' **own** Google Chat/Slack adapters. The platform agent holds **read-only** cluster RBAC and performs all mutations through a **GitOps write path** (human-reviewed PRs).

**Scion** (`github.com/GoogleCloudPlatform/scion`) — a container-based **orchestration platform** for running many LLM "deep agents" (Claude Code, Gemini CLI, Codex, Hermes, …) concurrently, each isolated in its own container with separate credentials and a git worktree. A **Hub** (control plane + web UI + chat channels + scheduler) and a **Runtime Broker** manage agent lifecycle across Docker/Podman/Kubernetes backends. On Kubernetes it creates a **bare Pod per agent** and drives it via **`pods/exec`** (`tmux send-keys`). It is opinionated and batteries-included: templates/personas, projects, inter-agent messaging, human-in-the-loop attach, Slack/Telegram/Discord. Not the SCION networking project. Most mature of the three; K8s runtime self-described as still rough.

**Agent Substrate** (`github.com/agent-substrate/substrate`) — a **low-level Kubernetes runtime** for "actors" (bursty, mostly-idle agent workloads). Instead of a pod-per-agent, it pre-warms a small pool of **worker Pods** and **multiplexes many actors onto few workers over time** via snapshot **suspend/resume** (demo: ~250 sessions on 8 pods), deliberately keeping the K8s API server and **`pods/exec` out of the hot path**. Its control plane is **pods-read-only**; actor isolation is via **gVisor** (`runsc`) or a Kata micro-VM. It is explicitly *not* an agent SDK and has **no chat, personas, delegation, or task queue** — a pure execution substrate. Pre-production alpha ("VERY early / aspirational").

**AX (Agent Executor)** (`github.com/google/ax`) — a thin **execution/orchestration layer** that runs *on top of* Substrate (or locally). It provides a **single-writer controller + durable append-only event log** for reliable, **resumable** agentic loops, and a bring-your-own **Harness** abstraction (custom image implementing a `HarnessService` gRPC contract, with Skills + MCP support). Its core call is "run a harness against an input → stream output → durable completion." It maps each conversation/execution to a Substrate actor; on Kubernetes it holds **no RBAC and creates no pods** — it deploys via Substrate's CRDs. No chat/persona/hub layer of its own. Earliest-stage of the three (PRs paused, k8s path "experimental").

---

## The three runtimes compared

A head-to-head across the dimensions that matter when choosing a runtime for kube-agents' agents. (Detailed, use-case-specific tables follow in the sections below.)

| Dimension | **Scion** | **Agent Substrate** | **AX (on Substrate)** |
|---|---|---|---|
| Layer / what it is | full orchestration **platform** | low-level K8s **runtime** | **execution layer** over Substrate |
| Execution model | bare Pod per agent | multiplex actors onto a warm worker pool (suspend/resume) | one execution per conversation, delegated to a Substrate actor |
| Needs `create pods` + **`pods/exec`**? | ✅ **yes** (the decisive con) | ❌ no exec; pods-read-only hot path (only CRD controller creates the pool) | ❌ no k8s RBAC at all |
| Isolation | container + locked SecurityContext | **gVisor / micro-VM** | **gVisor / micro-VM** (inherited) |
| Read-only cluster posture | ❌ incompatible | ✅ compatible | ✅ compatible |
| Self-healing / resumption | ❌ none (bare Pod, `RestartPolicy: Never`) | ⚠️ snapshot suspend/resume (lazy) | ✅ durable event-log replay |
| Spawn with per-task input | ✅ at dispatch | ❌ `CreateActor` takes no input | ✅ `Exec{inputs}` |
| Result + completion signal | ⚠️ weak (session/self-report) | ❌ none (traffic to in-actor server only) | ✅ stream + durable `STATE_COMPLETED` |
| Orchestration surface (chat/personas/hub/delegation) | ✅ rich | ❌ none | ⚠️ conversation/harness only, no chat/persona/delegation |
| Chat channels | Slack/Telegram/Discord (no Google Chat) | none | none |
| Run our Hermes content | ✅ minor rework (`hermes chat` honors SOUL/skills/config/MCP) | via a custom actor image (in-actor server) | via a custom `HarnessService` image (Hermes wrapper) |
| Footprint | Hub + Broker + pods | control plane (ateapi/atecontroller/atelet DaemonSet/atenet) + worker pool | AX server + Substrate + worker pool |
| Maturity | most mature (K8s runtime "rough") | alpha ("VERY early") | earliest (PRs paused, k8s "experimental") |

**How to read this:** Scion is the only one that ships the *orchestration surface* (chat, personas, delegation) — but it's also the only one that requires the `create pods` + `pods/exec` privilege incompatible with kube-agents' read-only posture. Substrate and AX are runtime layers that preserve the read-only posture and add stronger (gVisor) isolation, at the cost of no orchestration surface and pre-production maturity. Because AX runs on Substrate, the realistic choices are **Scion**, **AX-on-Substrate**, or **raw Substrate** (not AX vs Substrate as independent options).

---

## The key distinction: two modes of "Hermes"

Both projects depend on `nousresearch/hermes-agent`, but use it in different modes.

| | kube-agents | Scion `hermes` harness |
|---|---|---|
| Package extra | `hermes-agent[google_chat]` | `hermes-agent[vertex]` |
| Launch command | `hermes gateway run` | `hermes chat --yolo` — `harnesses/hermes/config.yaml:42` |
| Operating mode | Long-running **gateway/server** | Interactive **CLI**, driven per-prompt |
| Input path | Hermes' own ingress (Google Chat/Slack via Pub/Sub, socket mode) | Scion injects prompts via `tmux send-keys` — `pkg/runtimebroker/handlers.go:1643` |
| Provides | profiles, kanban dispatcher, Hermes cron, chat adapters, gateway hooks | agent runtime only; Scion supplies orchestration + chat |

This is a division of labour, not a blocker: in `hermes chat` mode the agent's identity and capabilities are intact; what the gateway would have provided (chat ingress, autonomous scheduling, dispatch) becomes Scion's job.

---

## Content portability (verified from Hermes **and** Scion source)

Hermes reads its persona/config/skills from `$HERMES_HOME`, and Scion sets `HERMES_HOME=/home/scion/.hermes` and lays content there — the two line up.

| Artifact | Portability | Evidence |
|---|---|---|
| **SOUL.md** (persona) | ✅ **As-is** — read from `$HERMES_HOME` natively in `chat`; Scion never touches it | Hermes `agent/prompt_builder.py:1888`, `system_prompt.py:188`; Scion `provision.py` |
| **config.yaml** (model, toolsets, plugins) | ✅ **As-is** — honored by `hermes chat`; not clobbered | Hermes `config.py:7286`; Scion `provision.py` |
| **skills/** (SKILL.md) | ✅ **Rename-only** — live at `.hermes/skills`; Scion auto-copies template `skills/` | Hermes `skills_tool.py:143`; Scion `provision.go:822` |
| **Plugin tools** (e.g. `handover`) + **agent-lifecycle hooks** | ✅ **Works** in `chat` | Hermes `model_tools.py:204`, `plugins.py:391` |
| **MCP servers** | ⚠️ **Re-declare** in the Scion template's `mcp_servers`, else a shipped `mcp.json` is deleted | Scion `provision.py:181-195` |
| **AGENTS.md** | ⚠️ **Merge-safe, placement wrinkle** — Scion prepends a managed block; Hermes reads it from CWD while Scion writes to `$HOME`. Put instructions in SOUL.md | Hermes `prompt_builder.py:1932`; Scion `scion_harness.py:822` |

**Gateway-only features** (not in `hermes chat`) and their Scion replacement: chat adapters → Scion channels (Slack ✅, **Google Chat ✗**); `pre_gateway_dispatch` hooks → Scion session store / OTel / audit; autonomous cron + kanban dispatch → Scion cron + `dispatch_agent`.

### Packaging recipe (minor rework)

1. Rebuild the image `FROM scion-base`, then `pip install hermes-agent` and copy content (supplies `sciontool` + `tmux` + `python3` the pod entrypoint requires — `k8s_runtime.go:933`).
2. Place profile content under `~/.hermes/` (SOUL.md, config.yaml) and `.hermes/skills/`.
3. Re-declare MCP servers in the Scion template's `mcp_servers`.
4. Keep operating instructions in SOUL.md to sidestep the AGENTS.md CWD-vs-HOME wrinkle.
5. Add a durable backend for mutable state (kanban/sessions/handover) — see [Reliability gaps](#reliability--operational-gaps).

---

## Deployment topology

kube-agents is **one self-contained pod** in the cluster (control plane and agent are the same pod). Scion is **three tiers**:

```
CONTROL PLANE   ── Hub ──────── long-running HTTP + WebSocket server + web UI/REST
                   state: SQLite (single instance) OR Postgres (HA)
                     ▲
                     │  Broker DIALS Hub OUTBOUND (wss); Hub sends commands back down the tunnel
DATA PLANE      ── Runtime Broker (proxy type for k8s) ──
                   holds the K8s credentials; creates & drives pods via the K8s API (SPDY)
                     │  Kubernetes API server — exec / send-keys / GetLogs
AGENT PODS      ── bare Pods (RestartPolicy=Never, no self-heal) ──
                   sciontool init → tmux → hermes chat
                   SAME cluster (in-cluster/kubeconfig) OR a different cluster (kubeconfig)
```

- **Agent pods** run in a GKE cluster. The client is kubeconfig-first with in-cluster fallback (`pkg/k8s/client.go:87`), so pods can land in the **same** cluster the broker runs in, or a **different dedicated** cluster (external kubeconfig + context). Namespace via `SCION_K8S_NAMESPACE`/`POD_NAMESPACE`/SA-namespace (`k8s_runtime.go:73-87`).
- **The Hub** is a long-running server (`pkg/hub/server.go:2509`) with its own database — **SQLite** (single instance) or **Postgres** (HA). Ships a `Dockerfile.hub` but **no manifest**. Required only for hosted/multi-user orchestration; the CLI can drive the K8s runtime directly in local mode.
- **The Broker** is a **proxy broker** for K8s (`harness_config_handlers.go:42`): it holds the K8s credentials and drives pods entirely through the API server (SPDY exec, `GetLogs`). It **dials the Hub outbound**, so the Hub needs no inbound path to it. One broker manages the whole cluster.
- **No in-repo Helm chart or Hub/Broker manifest** exists at this HEAD — deploying to GKE is bring-your-own-manifest.

---

## Running Scion in the management cluster (Hub included)

This is viable and architecturally close to kube-agents. Into the management cluster you would deploy:

| Component | K8s resource | Notes |
|---|---|---|
| **Hub** | Deployment (`replicas: 1`) + Service (ClusterIP) + **RWO PVC** for SQLite | Analogous to today's gateway pod + `/opt/data` PVC. Postgres only needed for HA. |
| **Broker** (proxy) | Deployment + **ServiceAccount + RBAC** | Dials the Hub over ClusterIP; needs pod create/exec/log RBAC (see security section). |
| **Agent pods** | created on-demand by the broker | Same cluster, or managed clusters via mounted kubeconfigs. |
| *(optional)* chat bots, Postgres | Deployment / StatefulSet | Slack/Telegram/Discord; Postgres for HA. |

**Self-healing nuance:** because *you* author the Hub and Broker as Deployments, they self-heal after eviction. **Agent pods do not** — the broker creates them as bare Pods with `RestartPolicy: Never` and no controller (`k8s_runtime.go:1208,1237`). Mitigate with Scion's cron `dispatch_agent`, an external watchdog, or by keeping truly always-on work on a Deployment.

The footprint is larger than today (2 always-on control components + on-demand pods vs. 1 pod) but self-contained in one cluster. **However, where the broker's RBAC lives is a security decision — see below.**

---

## Security & RBAC (the decisive concern)

kube-agents requires a **read-only cluster posture**: the platform agent has read-only RBAC and all mutations go through GitOps. Scion's Kubernetes runtime is fundamentally incompatible with this.

### Scion cannot run read-only, and exec is unavoidable

Running any agent requires pod **creation** and **`pods/exec`** — both are on the critical path of *starting* (the pod blocks until the broker execs `touch /tmp/.scion-home-ready`, `k8s_runtime.go:933,418`) and *driving* (input is `tmux send-keys` via exec, `pkg/agent/manager.go:231`). There is **no** read-only, dry-run, GitOps, or attach-to-existing-pod mode (`Run()` always `Pods().Create()`s + exec-syncs). The `agents.x-k8s.io` Sandbox CRD is referenced but **unused** — there is no non-exec/network drive path.

### Minimal required RBAC (inherently mutating)

```yaml
kind: Role   # ClusterRole if multi-namespace (ListAllNamespaces) is enabled
rules:
- apiGroups: [""]
  resources: ["pods"]
  verbs: ["get","list","create","delete"]
- apiGroups: [""]
  resources: ["pods/exec"]        # SPDY exec — startup, send-keys, attach
  verbs: ["create"]
- apiGroups: [""]
  resources: ["pods/log"]
  verbs: ["get"]
- apiGroups: [""]
  resources: ["secrets"]          # auth material for agents
  verbs: ["create","delete","list"]
- apiGroups: [""]
  resources: ["persistentvolumeclaims"]
  verbs: ["get","list","create","delete"]
- apiGroups: ["secrets-store.csi.x-k8s.io"]
  resources: ["secretproviderclasses"]
  verbs: ["create","delete","list"]
```
Evidence: `k8s_runtime.go:356` (pods create), `1808/1821` (delete), `1665/1729/2012/2297` (`pods/exec`), `1953` (`pods/log`), `511-711` (secrets), `808-874` (PVCs).

### Why this cannot be narrowed with RBAC

Kubernetes RBAC scopes by resource **type** and **namespace**, never by instance or ownership. So `create pods` + `create pods/exec` apply to **any** pod in the namespace — the broker SA can create arbitrary pods and exec into **any existing pod** in that namespace, not just Scion's own. This directly realizes two concerns: *(a)* it can create pods beyond its own subagents, and *(b)* it can run arbitrary code (exec) in any pod sharing the namespace.

### Privilege-escalation surface

- **create-pod-with-arbitrary-SA + exec = token theft.** Scion sets `serviceAccountName` on created pods from template config (`types.go:318`, `k8s_runtime.go:1517`); combined with exec, a caller can launch a pod bound to a more-privileged SA and read its token.
- **Tokens are automounted into attacker-runnable code.** Scion does **not** set `automountServiceAccountToken: false`; the LLM shell inside each agent can read its own SA token directly. **The subagent's ServiceAccount = the subagent's power.**
- **No guardrails on created pods.** No image allowlist, no SA pinning. If the create path is reached (see below), the requester controls image + command + SA.
- **The one built-in hardening:** the created-pod SecurityContext is locked — non-root, `drop ALL` caps, seccomp, no privileged/hostPath/hostNetwork (`k8s_runtime.go:1149-1159,1227-1232`).

### Escalation through the orchestration layer

The agent's built-in tooling (`sciontool`) has **no** pod-create method (deny-by-default authz). But "agent" is a first-class principal that **can** be granted create/delegation policy; if granted and with network reach to the Hub API, an agent can call `DispatchAgentCreate` with caller-controlled image/command/SA (`pkg/hub/httpdispatcher.go:801`). `message --wake` also re-launches suspended agent pods. So a prompt-injected agent with a create-capable token could spawn arbitrary compute — gated only by Hub authz policy and network segmentation, not by any image/SA guardrail.

### Containment (reduces, does not eliminate)

Scion supports a **dedicated namespace** for agent pods (`k8s_runtime.go:73-87,209-213`) but provides **no** admission webhooks, PodSecurity wiring, image allowlist, or SA pinning — all external. The strongest containment, given extra deployment is acceptable:

1. **Run agent pods in a separate, dedicated cluster** — not `kubeagents-system`. The broker's `create/exec/secrets` then live in a throwaway agent cluster containing nothing sensitive; the management cluster keeps no mutating create/exec principal.
2. **Never co-locate the broker SA with the Hub/Broker/DB** — co-location means a broker (or prompt-injected agent) compromise can exec into control-plane pods and read all Secrets (DB creds, tokens) = full control-plane compromise.
3. **Subagent pod SA = read-only** + `automountServiceAccountToken: false` (or a zero-privilege pinned SA).
4. **Admission policy** (Gatekeeper/Kyverno): pin the SA, allowlist images, block SA-override.
5. **PodSecurity `restricted`** on the agent namespace; **NetworkPolicy** blocking subagent → Hub-API egress unless intended.
6. **Constrain the dispatch path** so LLM input cannot choose image/command/SA.

**Residual, irreducible con:** even fully contained, one mutating principal (the broker) holds namespace-wide `create pods` + `pods/exec` + `secrets` and cannot be made read-only or exec-free. It can be pushed out of the management cluster, but not eliminated.

---

## Reliability & operational gaps

- **No self-healing on eviction** — agent pods are bare Pods, `RestartPolicy: Never`, no reconcile (`k8s_runtime.go:1208,1237`; manual `scion resume`, `cmd/resume.go:23`).
- **Stateful data needs a durable backend** — `kanban.db`, sessions, handover records won't survive the default ephemeral `/workspace` (EmptyDir, `k8s_runtime.go:1196`); requires NFS/Filestore (RWX), shared-dir RWX PVCs, or GCS-fuse. No arbitrary RWO-PVC mount, no hostPath (`k8s_runtime.go:1498-1505`).
- **Google Chat is a non-functional stub** (`pkg/config/integration_config.go`); Slack/Telegram/Discord work (`pkg/hub/channels.go`).
- **GCP identity** — Scion sets `serviceAccountName` for Workload Identity if you name a pre-bound KSA (`k8s_runtime.go:1517`, `types.go:318`) but does not create the binding; metadata interception doesn't work on K8s (pod drops all caps, `k8s_runtime.go:1230`).
- **Scion replaces the runtime, not the platform** — LiteLLM, the OTel collector, the GitHub token-minter, and cluster RBAC remain our responsibility.
- **No in-repo Helm/manifests** for Hub/Broker — bring-your-own.

---

## Pros of adopting Scion

The strongest pro sits at a different layer than the security con.

- **Per-agent isolation (a security *upgrade* at the workload layer).** Today kube-agents runs Chat/Platform/Cluster as Hermes **profiles in one shared pod** with one shared identity — a prompt-injected profile shares that pod's filesystem (`/opt/data`, `kanban.db`, every kubeconfig), network, and Workload Identity. Scion gives each agent **its own container, credentials, and worktree** with a locked SecurityContext. A prompt-injected subagent (with a read-only, token-automount-off SA) reaches only its own scoped creds — materially shrinking the blast radius the shared-pod model cannot.
- **Fleet-scale concurrency + a real scheduler** — many isolated agents across nodes/clusters, with `dispatch_agent` cron; better than today's in-pod subprocess fan-out for Cluster Agents.
- **Easy to add persona types** — each persona is a template (image + prompt + tools), simpler than Hermes profile scaffolding + operator changes.
- **Native orchestration primitives** — projects, templates, inter-agent messaging, scheduler; could replace bespoke kanban dispatcher + scaffolding + handover code.
- **Heterogeneous harnesses** — Claude Code / Gemini / Codex / Hermes side by side (kube-agents is Hermes-only).
- **Human-in-the-loop attach** (tmux), **swarm observability** (OTel + Hub dashboard + phase/activity state model), **bidirectional chat** (Slack/Telegram/Discord).

Two honest caveats: (1) the isolation pro is achievable **without** Scion — running Cluster Agents as separate pods with read-only SAs via our own operator gets the workload-isolation win without importing the broker's create/exec privilege; (2) several pros are discounted by immaturity (no Helm, no self-healing, missing Google Chat, and all security hardening is DIY).

---

## Delegated task-workers: Scion vs Substrate/AX vs DIY Jobs

This section evaluates a narrower, concrete architecture: **the platform agent plans work and delegates tasks to short-lived worker agents that the runtime spawns.** (Current analogue: the kanban dispatcher runs `hermes -p <cluster> chat -q "work kanban task <id>"` per assigned card.)

### The reframe: the runtime is a pure *executor*

Because planning and delegation live in the platform agent, you are **not** using the runtime's orchestration surface (chat, personas, hub, its own delegation). That **discounts Scion's main advantage** — the batteries-included orchestration is exactly the part you won't use. What actually matters:

1. spawn a worker **with per-task input**
2. get a **result + completion signal** back (delegation fan-in)
3. **isolation** between workers
4. **security of the spawn primitive** (the planner is read-only + prompt-injectable)
5. **maturity / footprint**

### The candidates

- **Agent Substrate** (`github.com/agent-substrate/substrate`) — a Kubernetes runtime that multiplexes "actors" onto a warm pool of worker Pods via snapshot suspend/resume, keeping the K8s API server and `pods/exec` **out of the hot path**; isolation via **gVisor** (or micro-VM). Not an agent SDK; no chat/persona/delegation.
- **Agent Executor / AX** (`github.com/google/ax`) — a thin execution layer *on top of* Substrate (or local): a single-writer controller + durable event log giving resumable "run harness against input → stream output → durable completion" executions. Bring-your-own harness via a `HarnessService` gRPC contract.
- **DIY read-only Job** — the platform agent's own operator spawns a Kubernetes **Job** whose command is `hermes chat -q "work task N"` (task passed as **args at creation**), with a read-only SA and optional **GKE Sandbox (gVisor via RuntimeClass)**.

### Scored for this use case

| Axis | **Scion** | **Substrate (raw)** | **AX (on Substrate)** | **DIY read-only Job** |
|---|---|---|---|---|
| Spawn with per-task input | ✅ at dispatch | ❌ `CreateActor` takes no input | ✅ `Exec{inputs}` | ✅ task as command args |
| Result + completion signal | ⚠️ weak (session/self-report) | ❌ none | ✅ stream + durable `STATE_COMPLETED` | ✅ Job status + logs/sink |
| Isolation | container + locked SecCtx | ✅ gVisor / microVM | ✅ gVisor / microVM | ✅ via GKE Sandbox (gVisor) |
| Spawn-primitive security | ❌ `create pods` + **`pods/exec`**; dispatch carries **arbitrary image/command/SA** | ✅ no exec; pods-read-only hot path | ✅ no exec; **fixed harness/template, input-only** | ✅ controller-scoped `jobs:create`; no exec; fixed template |
| Read-only posture preserved | ❌ (decisive con) | ✅ | ✅ | ✅ |
| Self-healing / resumption | ❌ none | ⚠️ snapshot resume (lazy) | ✅ event-log replay | ✅ Job `backoffLimit` |
| No server-wrapper needed | ✅ (drives CLI via exec) | ❌ needs in-actor server | ❌ needs `HarnessService` server | ✅ CLI runs as command |
| Maturity | most mature (K8s runtime "rough") | ❌ "VERY early / aspirational" | ❌ earliest (PRs paused, k8s "experimental") | ✅ stock Kubernetes |

Evidence: Substrate `CreateActorRequest` carries only an `Actor` (no args/env/input) — `pkg/proto/ateapipb/ateapi.proto:212`; input delivered only via routed traffic to an in-actor server (`demos/counter/counter.go:69`), CLI workloads are fire-and-forget to stdout (`demos/claude-code-multiplex/workload/run.sh:43`). AX `Exec{conversation_id, inputs, harness_id}` → stream → `STATE_COMPLETED` in the event log (`proto/ax.proto:118-137`, `internal/controller/controller.go:151-174`); custom harness = your image implementing `HarnessService` (`proto/ax.proto:92-98`, `internal/config/config.go:213-219`). Neither repo contains a kanban board, dispatcher, or task queue (`grep kanban|board|dispatcher` → none).

### The security point that's decisive for delegation

The planner is read-only and prompt-injectable, and you are giving it a tool that **spawns compute**. The spawn primitive's constraint level is everything:

- **Scion** — `DispatchAgentCreate` carries caller-controlled **image + command + serviceAccountName**, and the broker holds `pods/exec`. A prompt-injected planner reaching this can spawn arbitrary compute and exec into pods.
- **AX** — the planner picks a **registered `harness_id`** and passes an **input string**; the worker image/template is fixed by your config, the sandbox is gVisor, and the SA is whatever you pin (read-only). Worst case from injection: "run a fixed read-only harness with an attacker-chosen prompt in an isolated sandbox."
- **DIY Job** — same constrained shape: a fixed Job template, task as input, read-only SA, created by a deterministic controller (not an LLM-driven exec broker).

For a delegation pattern, the **constrained, input-only spawn** (AX or DIY Job) is the security-correct shape; Scion's arbitrary-spawn + exec is not.

### What you build regardless of choice

- The **delegation tool + fan-in** in the platform agent (plan → spawn N → collect → replan). None of the three provide agent-to-agent delegation built in.
- The **result-reporting mechanism** — isolated workers can't share `kanban.db`; use a networked kanban service (worker self-reports) or pointer-in/result-out (AX-native; Job via a result sink).
- For AX/Substrate: a **Hermes-as-server wrapper** (because `hermes chat -q` is a CLI, not a server). For Scion: containment (separate cluster + admission policy). For DIY: the Job template + a result sink.

### Verdict for delegated task-workers

Ranking: **DIY read-only Jobs (simplest, secure, available now) ≈ AX-on-Substrate (best-shaped, most secure spawn, but alpha) > Scion (mature but wrong-shaped, un-offset create+exec con).** Scion's orchestration strengths are wasted in this design and its security con is not offset. Prototype the delegation tool against **AX** for the sophisticated path and **read-only Jobs** as the low-risk baseline; choose based on whether gVisor density + resumption are needed now, given Substrate/AX are pre-production.

---

## Identity: per-agent KSA (the decisive requirement)

The concrete goal is: **each spawned subagent gets its own Kubernetes ServiceAccount (KSA) → its own Workload Identity, scoped GCP access, and scoped RBAC.** Today all agents run as Hermes *profiles in one pod* and share a single identity — the security problem this effort exists to fix. Because a KSA/token is a **per-Pod** construct, this requirement is really "**one pod (workload) per agent**," which reorders the options and is the opposite pull from the no-exec/gVisor argument above.

### Ranking for per-agent KSA

| Option | Per-agent KSA? | Why |
|---|---|---|
| **Extend the operator** (Jobs/Deployments) | ✅ **native, recommended** | operator already creates a per-agent SA; each workload gets its own KSA + WI |
| **DIY read-only Job** | ✅ **native** | one Job = one pod = one KSA |
| **Scion** | ✅ native, but heavy | pod-per-agent; `serviceAccountName` per workload — at the cost of Hub/Broker + create+exec |
| **Raw Substrate** | ❌ **incompatible** | actors multiplex onto *shared* pods; **no `serviceAccountName` field** on any type; worker pods run as namespace `default` |
| **AX on Substrate** | ❌ **worst** | all workers of one harness share one template/pool/KSA; no per-execution identity |

### Why Substrate/AX cannot do per-agent KSA

- A KSA token is bound to a Pod; Substrate's value is running many actors through a **shared, migrating pool** of Pods — so a per-Pod KSA can never be per-actor. Confirmed: no `serviceAccountName` field on `WorkerPoolSpec`/`ActorTemplateSpec` (`pkg/api/v1alpha1/workerpool_types.go:54-87`, `actortemplate_types.go:283-334`), and the controller never sets one — worker pods run as the namespace **`default`** KSA (`workerpool_apply.go`).
- Substrate's own threat model wants the pod KSA to be **zero-privilege** and blocks actor access to the K8s API (`threat-model.md:104`).
- To give personas distinct identities on Substrate you'd need either **one WorkerPool per identity** (defeats multiplexing; the SA field doesn't even exist yet) or Substrate's **SessionIdentity broker** — a per app/user/session **SPIFFE/OIDC** credential (`sessionidentity.go:111,191`) that is deliberately **not a KSA/GSA**, is OIDC-federatable to GCP only in principle (unwired), and whose pod↔session binding checks are **TODOs** (`sessionidentity.go:88-91,155`; `threat-model.md:110`).

### kube-agents already creates per-agent KSAs

The operator you already run creates a ServiceAccount per agent (defaulting to the agent's name), with Workload Identity annotations, automount on: `k8s-operator/internal/controller/platformagent_controller.go:181-195` (`reconcileServiceAccount`, `saName := agent.Name`), `platformagent_manifests.go:535` (`ServiceAccountName: saName`), `common_types.go:161-167` (`ServiceAccountName` + `ServiceAccountAnnotations` for `iam.gke.io/gcp-service-account`). The shared-credentials problem exists only because subagents are *profiles in one pod*, not because the machinery is missing.

---

## Recommended design: per-agent KSA via the existing operator

**Recommendation:** don't adopt a new platform. **Extend the kube-agents operator to spawn each delegated subagent as its own Kubernetes workload — a Job for task-scoped work, a Deployment for long-lived monitors — each with its own KSA + Workload Identity, running the existing Hermes image via its CLI.** This is the smallest, most secure step from where you are.

Why this beats Scion/Substrate/AX for *this* requirement:
- **Per-agent KSA needs pod-per-agent** — which this is, and the operator already renders per-agent SAs.
- **`hermes -p <profile> chat -q "<task>"` as the container command** needs **no `HarnessService` wrapper** (unlike AX/Substrate) and **no `pods/exec`** (unlike Scion). The CLI runs as the entrypoint and exits.
- **Self-healing** comes free (`Job.backoffLimit`; Deployment reconcile), which Scion's bare pods lack.
- **Zero pre-production dependencies** — no Hub/Broker, no alpha Substrate/AX.

### The `AgentTask` CRD (sketch)

Re-point the planning agent's kanban tool to create `AgentTask` CRs (same "create a task" interface, declarative + auditable backend):

```yaml
apiVersion: agents.kube-agents.io/v1alpha1
kind: AgentTask
metadata:
  name: kanban-4213
  namespace: kubeagents-agents
spec:
  assignee: cluster-agent            # persona → fixed KSA + template (operator config)
  target:  { cluster: prod-eu, location: europe-west1 }
  task: "investigate CrashLoopBackOff in namespace payments"
  ttlSecondsAfterFinished: 3600
status:
  phase: Running                      # Pending | Running | Succeeded | Failed
  jobRef: kanban-4213-xyz
  result: ""                          # summary / pointer to result sink
```

### Controller reconcile flow

```
planning agent (kanban-only tool) ── creates ──▶ AgentTask CR
                                                     │  (operator watches)
                                                     ▼
                         operator maps assignee ─▶ (fixed KSA, fixed workload template)
                                                     │  renders:
                                                     ▼
   read-only Job:  serviceAccountName=<persona KSA>            (its own Workload Identity)
                   runtimeClassName=gvisor                      (GKE Sandbox)
                   command=[hermes,-p,<profile>,chat,-q,<task>] (task as args — no exec)
                   securityContext: restricted, non-root
                                                     │
                   worker runs read-only, writes result to sink (as its own KSA)
                                                     │  Job completes
                                                     ▼
             operator sets AgentTask.status.phase=Succeeded + result  ──▶ planner replans / kanban_complete
```

### Rendered worker Job (sketch)

```yaml
apiVersion: batch/v1
kind: Job
metadata: { name: kanban-4213-xyz, namespace: kubeagents-agents }
spec:
  backoffLimit: 2                     # self-heal on failure
  ttlSecondsAfterFinished: 3600
  template:
    spec:
      serviceAccountName: agent-cluster-prod-eu   # per-persona KSA ↔ GSA (read-only)
      runtimeClassName: gvisor                     # optional: GKE Sandbox
      restartPolicy: OnFailure
      securityContext: { runAsNonRoot: true, seccompProfile: { type: RuntimeDefault } }
      containers:
      - name: worker
        image: <kube-agents-hermes-image>
        args: ["-p","cluster","chat","-q","investigate CrashLoopBackOff in namespace payments"]
        # SOUL.md/skills baked in image or mounted from ConfigMap; KUBECONFIG scoped to target cluster
```

### Who holds which privilege

| Component | Privilege |
|---|---|
| Planning agent (LLM) | only creates `AgentTask` CRs — no spawn, no infra tools |
| Operator (deterministic controller) | the *only* mutating principal — `jobs`/`serviceaccounts` create in `kubeagents-agents`, **fixed templates, no exec** |
| Subagent Job pod | its **own read-only KSA**, gVisor sandbox, scoped Workload Identity; cannot touch other agents |
| Fleet mutations | unchanged — still GitOps PRs (human-reviewed) |

The LLM never chooses identity or image (fixed `assignee → KSA/template` map), which contains prompt injection by construction.

### Kanban board with per-agent identity

Two changes make the board work for remote, individually-identified workers:
1. **Kanban becomes a networked service** (or the `AgentTask` CRD is the board) — remote pods can't share the `kanban.db` sqlite file.
2. **Per-KSA authorization** — because each worker has its own identity, the board can enforce that worker-for-card-N reads only card N and writes only its own completion. That per-identity authorization is impossible with today's shared credentials and is the security payoff of the whole exercise. Reporting is either pointer-in/result-out (operator writes `status`) or the worker self-reports over the network authenticated by its KSA.

### What to build

- The `AgentTask` CRD + a reconcile controller in the existing operator (kubebuilder scaffold — same patterns as `PlatformAgent`).
- Per-persona KSA/GSA provisioning (extends `reconcileServiceAccount` + your IAM scripts).
- The result sink (or networked kanban-with-per-KSA-authz for self-reporting workers).

### All options at a glance

| Option | Per-agent KSA | No `pods/exec` | Self-heal | Isolation | Maturity | Fit |
|---|---|---|---|---|---|---|
| **Extend operator (Jobs/Deployments)** | ✅ | ✅ | ✅ | gVisor via RuntimeClass | ✅ stock K8s + your operator | **recommended** |
| DIY read-only Job (no CRD) | ✅ | ✅ | ✅ | gVisor via RuntimeClass | ✅ | good baseline |
| Scion | ✅ | ❌ | ❌ | container | mature (runtime rough) | wrong-shaped, create+exec con |
| Raw Substrate | ❌ | ✅ | ⚠️ | gVisor/microVM | alpha | incompatible w/ per-KSA |
| AX on Substrate | ❌ | ✅ | ✅ | gVisor/microVM | earliest | incompatible w/ per-KSA |
| SIG **Agent Sandbox** (`agents.x-k8s.io`) | ✅ (pod-per-agent) | ✅ | — | pod sandbox | early (upstream) | future substrate to watch |

### Tradeoffs & what to watch

- **Pod-per-task = cold start, no multiplexing density.** Fine for moderate task volume; if you ever need massive, sub-second-idle concurrency, that's the regime where Substrate's multiplexing earns its keep — but you'd give up per-KSA.
- **Kubernetes SIG Agent Sandbox** (`agents.x-k8s.io`) is the emerging upstream primitive for per-agent sandboxed pods — the natural future substrate *under this same operator-driven design*. Early, but the direction to track. (Scion defines its CRD but doesn't use it.)

---

## Recommendation

1. **Primary: spawn subagents as per-agent-KSA workloads via the existing operator.** Extend kube-agents with an `AgentTask` CRD + controller that renders each delegated subagent as its own read-only Job/Deployment with its own KSA + Workload Identity, running the existing Hermes image via its CLI. This directly solves the shared-credentials problem, needs no `pods/exec` and no new platform, self-heals, and keeps the LLM out of the spawn path. See [Recommended design](#recommended-design-per-agent-ksa-via-the-existing-operator).
2. **Keep the read-only platform tier on kube-agents.** Its read-only RBAC + GitOps write path is a hard requirement no external runtime should erode; fleet mutations stay GitOps PRs.
3. **Do not use Substrate/AX for per-agent identity.** Their multiplexing model is incompatible with per-agent KSA; their alternative (SPIFFE SessionIdentity) is not a KSA/GSA and is not securely enforced yet. Revisit only if you later need massive sub-second-idle concurrency and can accept a non-KSA identity. See [Identity](#identity-per-agent-ksa-the-decisive-requirement).
4. **Do not adopt Scion for this.** It can do per-agent KSA (pod-per-agent) but drags in a Hub/Broker + an irreducible `create pods` + `pods/exec` privilege for an orchestration surface you won't use. If Scion were ever adopted for other reasons, confine it to a dedicated agent cluster behind admission policy (see [Security & RBAC](#security--rbac-the-decisive-concern)).
5. **Track SIG Agent Sandbox** (`agents.x-k8s.io`) as the future upstream substrate under the same operator-driven design.
6. **Prove it with a scoped PoC:** an `AgentTask` → read-only Job with a per-persona KSA running `hermes chat -q`, reporting a result, with a prompt-injected planner unable to choose identity/image.

**Bottom line:** the requirement — **each subagent on its own pod with its own KSA** — is best met by **extending the operator you already have** to render per-agent, per-KSA, read-only Jobs/Deployments from an `AgentTask` CRD, running the existing Hermes image via its CLI. **Substrate/AX are incompatible** with per-agent KSA (shared-pool multiplexing); **Scion** can do it but only by importing a Hub/Broker and the `create pods` + `pods/exec` privilege that contradicts the read-only posture. The operator path is the smallest, most secure step and beats all three platforms on the actual requirement; Scion/Substrate/AX remain relevant only for different problems (rich orchestration, or extreme multiplexed density with a non-KSA identity).

---

## Appendix: source evidence

Scion (`github.com/GoogleCloudPlatform/scion` @ `b4c9911`):

| Finding | File:line |
|---|---|
| Hermes launched as `hermes chat --yolo` (CLI, not gateway) | `harnesses/hermes/config.yaml:42` |
| Provisioner writes `.env`/`AGENTS.md`/`mcp.json` only; never touches SOUL.md/config.yaml | `harnesses/hermes/provision.py` |
| Template `home/` copied into agent home; `skills/` → `.hermes/skills`; AGENTS.md merged | `pkg/agent/provision.go:787,822`; `harnesses/scion_harness.py:822` |
| `mcp.json` deleted if template declares no MCP servers | `harnesses/hermes/provision.py:181-195` |
| `HERMES_HOME=/home/scion/.hermes`; CWD=`/workspace`; no `-p` flag | `provision.py:232`, `k8s_runtime.go:1224`; `pkg/harness/declarative_generic.go:93` |
| Image must ship sciontool + tmux + python3 + hermes | `k8s_runtime.go:933`, `harnesses/hermes/config.yaml:27`, `image-build/scion-base/Dockerfile` |
| Human input injected via tmux send-keys (exec) | `pkg/runtimebroker/handlers.go:1643`, `pkg/agent/manager.go:231` |
| K8s object = bare Pod, `RestartPolicy: Never`; no reconcile; manual resume | `k8s_runtime.go:1208,1237,356`; `pkg/hub/server.go:1991`; `cmd/resume.go:23` |
| Startup gated on exec (`touch /tmp/.scion-home-ready`) | `k8s_runtime.go:933,418` |
| `pods/exec` call sites (startup, send-keys, attach, commands) | `k8s_runtime.go:1665,1729,2012,2297` |
| `pods/log` stream | `k8s_runtime.go:1953` |
| secrets create/delete/list; PVC get/list/create/delete | `k8s_runtime.go:511-711`; `808-874` |
| `serviceAccountName` set from config; token automount not disabled | `types.go:318`, `k8s_runtime.go:1517` |
| Locked pod SecurityContext (non-root, drop ALL caps, seccomp) | `k8s_runtime.go:1149-1159,1227-1232` |
| Pod-create reachable via Hub `DispatchAgentCreate`; `message --wake` relaunch | `pkg/hub/httpdispatcher.go:801`; `pkg/hub/handlers_agent_messaging.go:449` |
| Namespace selection + per-agent override | `k8s_runtime.go:73-87,209-213` |
| Default `/workspace` = ephemeral EmptyDir; no hostPath/RWO-PVC | `k8s_runtime.go:1196,1498-1505` |
| Client kubeconfig-first, in-cluster fallback (same or different cluster) | `pkg/k8s/client.go:87` |
| Hub = HTTP+WS server; SQLite or Postgres | `pkg/hub/server.go:2509`; `cmd/server_foreground.go:955-1009` |
| Broker = proxy type for k8s; dials Hub outbound | `pkg/hub/harness_config_handlers.go:42`; `pkg/runtimebroker/controlchannel.go:173` |
| Cron scheduler + `dispatch_agent` | `pkg/hub/scheduler.go`, `pkg/hub/server.go:2206` |
| Chat channels (Slack/Discord/Email/Webhook; Google Chat is a stub) | `pkg/hub/channels.go`, `pkg/config/integration_config.go` |

Hermes (`github.com/nousresearch/hermes-agent`) — what `hermes chat` honors:

| Finding | File:line |
|---|---|
| SOUL.md read from `$HERMES_HOME` as identity, in chat mode | `agent/prompt_builder.py:1888`, `agent/system_prompt.py:188` |
| AGENTS.md read from CWD as project context (additive to SOUL.md) | `agent/prompt_builder.py:1932` |
| Skills loaded from `$HERMES_HOME/skills`; synced on launch | `tools/skills_tool.py:143`, `hermes_cli/main.py:2542` |
| config.yaml honored (model, mcp_servers, toolsets, plugins) | `hermes_cli/config.py:7286`, `model_tools.py:399` |
| MCP servers spawned/connected in chat | `cli.py:1030-1034` |
| Plugin tools register into shared registry (work in chat) | `model_tools.py:204`, `plugins.py:391` |
| `pre_gateway_dispatch` hook / chat adapters / autonomous cron+kanban are gateway-only | `gateway/run.py:10322,9487,23360,8161` |

Agent Substrate (`github.com/agent-substrate/substrate`) and AX (`github.com/google/ax`):

| Finding | File:line |
|---|---|
| Substrate multiplexes actors onto a warm worker-Pool (Deployment), not pod-per-agent | `cmd/atecontroller/internal/controllers/workerpool_apply.go:30,74` |
| Control plane → node daemon (DaemonSet) → in-pod driver over gRPC; **no `pods/exec` anywhere** | `internal/proto/ateletpb/atelet.proto:21`; `internal/proto/ateompb/ateom.proto:34`; (no `SubResource("exec")` in repo) |
| `ateapi` control plane is **pods read-only**; only CRD controller has `pods/deployments: create` | `manifests/ate-install/ate-api-server.yaml:22-24`; `generated/role.yaml:31-52` |
| Actor isolation via gVisor `runsc` (or Kata micro-VM); one actor per worker at a time | `manifests/ate-install/sandboxconfig-gvisor.yaml`; `cmd/ateom-gvisor/runsc.go`; `docs/glossary.md` |
| `CreateActorRequest` carries only an `Actor` — **no args/env/input** per actor | `pkg/proto/ateapipb/ateapi.proto:212`; template command/args on the CRD only (`pkg/api/v1alpha1/actortemplate_types.go:99,113`) |
| Substrate result delivery only via routed traffic to an in-actor server; CLI workloads are fire-and-forget | `demos/counter/counter.go:69`; `demos/claude-code-multiplex/workload/run.sh:43` |
| AX `Exec{conversation_id, inputs, harness_id}` → stream → durable terminal state | `proto/ax.proto:118-137`; `internal/controller/controller.go:151-174` |
| AX custom harness = your image implementing `HarnessService` gRPC server | `proto/ax.proto:92-98`; `internal/config/config.go:213-219`; `internal/harness/substrate/substrate.go:90-131` |
| AX has no k8s client-go dependency; creates no pods / no exec | (no `k8s.io` in `go.mod`; deploys via Substrate CRDs) |
| Neither ships a kanban board, dispatcher, or task queue | (`grep kanban\|board\|dispatcher` → none) |
| Maturity: both pre-production alphas | Substrate `README.md`/`docs/architecture.md:3`; AX `README.md`, `manifests/README.md` |
| **Substrate: no `serviceAccountName` on any type; worker pods run as namespace `default`** | `pkg/api/v1alpha1/workerpool_types.go:54-87`, `actortemplate_types.go:283-334`, `workerpool_apply.go` |
| Substrate wants pod KSA zero-privilege; blocks actor→K8s API | `docs/threat-model.md:104` |
| Substrate per-actor identity = SPIFFE/OIDC SessionIdentity broker (not KSA/GSA; binding checks are TODOs) | `cmd/ateapi/internal/sessionidentity/sessionidentity.go:111,191,88-91,155`; `threat-model.md:110` |
| AX: one harness = one template/pool/KSA; no per-execution identity | `internal/harness/substrate/substrate.go:88-99`; `internal/config/config.go:213-219` |

kube-agents (this repo):

| Finding | File |
|---|---|
| Base image `nousresearch/hermes-agent`; runs `hermes gateway run` | `deploy/docker/Dockerfile`, `deploy/shared/docker-entrypoint.sh` |
| Read-only platform RBAC + GitOps write path | `k8s-operator/internal/controller/platformagent_manifests.go` |
| Google Chat / Slack ingress wired by the operator | `k8s-operator/internal/controller/platformagent_manifests.go` |
| Durable `/opt/data` RWO PVC (profiles, handover, kanban.db) | `k8s-operator/internal/controller/platformagent_manifests.go` |
| **Operator already creates a per-agent ServiceAccount (+ WI annotations, automount on)** | `k8s-operator/internal/controller/platformagent_controller.go:181-195`; `platformagent_manifests.go:535`; `api/v1alpha1/common_types.go:161-167` |
| Current subagents = Hermes profiles in one pod (shared identity) | `agents/{platform,cluster}/`, `scripts/cluster_agent_profile.py` (`hermes -p <name> chat -q "work kanban task <id>"`) |
