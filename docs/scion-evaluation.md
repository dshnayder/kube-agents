# Evaluation: Can Scion replace (or host) kube-agents?

**Status:** Evaluation / recommendation
**Date:** 2026-07-22
**Scope:** Whether [Scion](https://github.com/GoogleCloudPlatform/scion) can host the kube-agents Hermes agents and take over orchestration + communication — including deployment topology and security posture.
**Method:** Verified against source — Scion (`github.com/GoogleCloudPlatform/scion` @ `b4c9911`), Hermes (`github.com/nousresearch/hermes-agent`), and the kube-agents source in this repo. **Not** taken from published docs, which were found inaccurate on several points.

> ⚠️ Naming note: this "Scion" is **not** the SCION networking project (`scionproto/scion`). It is a Google Cloud AI-agent orchestration platform that happens to share the name.

---

## TL;DR

- **Content portability is easy.** Our Hermes platform-agent content — SOUL.md, skills, config.yaml, MCP tools, plugin tools — ports onto Scion with **minor rework**, confirmed from both the Hermes and Scion source.
- **Scion can run in the management cluster**, Hub included, in a shape close to kube-agents (a Hub Deployment + PVC, a Broker Deployment, on-demand agent pods).
- **The decisive issue is security/RBAC.** Scion's runtime is fundamentally a **mutating** actor: running any agent requires `create pods` + **`pods/exec`** (plus `create/delete secrets` and PVCs). There is **no read-only, GitOps, or exec-free mode**. This is categorically incompatible with kube-agents' hard requirement of a read-only cluster posture where all mutations flow through GitOps.
- **Genuine pros exist**, concentrated in **per-agent isolation, fleet-scale concurrency, and extensibility** — which are strongest for the *ephemeral subagent* tier, not the privileged platform tier.

**Recommendation:** Do not move the read-only platform tier onto Scion. If Scion is adopted, use it in a **hybrid**: kube-agents remains the read-only, GitOps-gated orchestrator and drives Scion to spawn **isolated, read-only subagents in a dedicated agent cluster** (not `kubeagents-system`), behind admission-policy guardrails Scion does not provide. See [Recommendation](#recommendation).

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

---

## Background: what each system is

**kube-agents** — an agentic harness that replaces `kubectl`/`gcloud`/Console with intent-driven agents for GKE fleet operations. Its runtime unit is a single **long-running Hermes gateway pod** (operator-managed Deployment) hosting multiple **profiles**: a Chat Agent front door, a privileged Platform Agent, and dynamically scaffolded per-cluster Cluster Agents. Delegation runs through a **kanban dispatcher** and file-based **handover** channel; chat reaches humans through Hermes' **own** Google Chat/Slack adapters. The platform agent holds **read-only** cluster RBAC and performs all mutations through a **GitOps write path** (human-reviewed PRs).

**Scion** — a container-based orchestration platform for running many LLM "deep agents" (Claude Code, Gemini CLI, Codex, Hermes, …) concurrently, each isolated in its own container with separate credentials and a git worktree. A Hub + Runtime Broker manage agent lifecycle across Docker/Podman/Kubernetes backends.

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

## Recommendation

1. **Keep the read-only platform tier on kube-agents.** Its read-only RBAC + GitOps write path is a hard requirement that Scion's runtime cannot satisfy. Do not move it onto Scion.
2. **If Scion is adopted, use the hybrid:** kube-agents remains the orchestrator and drives the Scion Hub to spawn **ephemeral, read-only subagents** for investigation/specialist work. Fleet mutations still route through kube-agents' GitOps PRs; subagents stay non-mutating investigators.
3. **Confine Scion's mutating footprint.** Run agent pods in a **dedicated agent cluster** (not `kubeagents-system`), keep the broker SA out of the control-plane namespace, and layer the admission/PodSecurity/NetworkPolicy/SA-pinning controls Scion does not provide (see [Security & RBAC](#security--rbac-the-decisive-concern)).
4. **Prove it with a scoped PoC** before committing: validate content portability end-to-end, eviction recovery, the chat channel (Slack vs Google Chat requirement), a durable state backend, and — most importantly — that a prompt-injected subagent cannot reach the broker's create path.

**Bottom line:** adopting Scion is feasible and brings real isolation/scale/extensibility gains for the *ephemeral subagent tier*. It is **not** appropriate for the *read-only platform tier*, because its runtime requires an irreducible, un-scopable `create pods` + `pods/exec` privilege that contradicts the read-only + GitOps requirement. The defensible path is a contained hybrid; a wholesale replacement is not.

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

kube-agents (this repo):

| Finding | File |
|---|---|
| Base image `nousresearch/hermes-agent`; runs `hermes gateway run` | `deploy/docker/Dockerfile`, `deploy/shared/docker-entrypoint.sh` |
| Read-only platform RBAC + GitOps write path | `k8s-operator/internal/controller/platformagent_manifests.go` |
| Google Chat / Slack ingress wired by the operator | `k8s-operator/internal/controller/platformagent_manifests.go` |
| Durable `/opt/data` RWO PVC (profiles, handover, kanban.db) | `k8s-operator/internal/controller/platformagent_manifests.go` |
