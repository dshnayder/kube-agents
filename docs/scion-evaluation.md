# Evaluation: Can Scion replace (or host) kube-agents?

**Status:** Evaluation / recommendation
**Date:** 2026-07-22
**Scope:** Whether [Scion](https://github.com/GoogleCloudPlatform/scion) can host the kube-agents Hermes agents and take over orchestration + communication.
**Method:** Verified against Scion source (`github.com/GoogleCloudPlatform/scion`, cloned at HEAD `b4c9911`) and the kube-agents source in this repo — **not** against Scion's published docs, which were found to be inaccurate on several points.

> ⚠️ Naming note: this "Scion" is **not** the SCION networking project (`scionproto/scion`). It is a Google Cloud AI-agent orchestration platform that happens to share the name.

---

## TL;DR

**Partly true, but the "easily" does not hold for our use case — because Scion's "Hermes" is not our Hermes.**

Scion and kube-agents both build on the same upstream package (`nousresearch/hermes-agent`), but they run it in **opposite modes**:

- **kube-agents** runs `hermes gateway run` — Hermes as a **long-running chat-gateway platform**: profiles, kanban delegation, Google Chat/Slack ingress, cron, and gateway plugin hooks.
- **Scion** runs `hermes chat --yolo` — Hermes as a **coding-agent CLI** that Scion puppeteers by injecting prompts over `tmux send-keys`.

The part of kube-agents that actually does our job — the Hermes **gateway** — is exactly the part Scion's Hermes integration does **not** run. So "deploy the hermes platform agent harness on top of Scion" is **not a lift-and-shift; it is a re-architecture** onto Scion-native primitives, and it regresses several properties that matter for an always-on fleet operator.

**Recommendation:** Do **not** replace the kube-agents platform gateway with Scion. Keep the always-on gateway (chat + monitoring + delegation) on the operator / a plain Kubernetes Deployment. **Do** consider Scion for the piece it is genuinely good at — ephemeral, strongly-isolated **specialist/investigation agents** (our per-cluster Cluster Agents and kanban workers) — where its isolation and security model are a real upgrade. See [Recommended path](#recommended-path-hybrid).

---

## Claim-by-claim verdict

| # | Claim | Verdict | Why |
|---|-------|---------|-----|
| 1 | "Scion might solve a lot of these problems (multi-agent + security)" | ✅ **Largely true** | Strong per-agent isolation (own container, credentials, git worktree), native multi-agent projects, a cron scheduler, and bidirectional chat. This is Scion's real strength. |
| 2 | "It now also supports hermes" | ⚠️ **True but misleading** | There is a built-in `hermes` harness — but it runs Hermes as a **coding CLI**, not as our gateway platform. Not the same capability set. |
| 3 | "Easily deploy the hermes platform agent harness on top of Scion" | ❌ **Not easily** | Requires abandoning gateway mode and re-modelling profiles, kanban delegation, Google Chat ingress, and gateway plugin hooks onto Scion primitives. |
| 4 | "Easily add additional agent/persona types" | ⚠️ **Partly** | Scion templates/projects make adding personas straightforward *if* they are Scion-native agents. Our persona model (SOUL.md + gateway profiles) does not port 1:1 — SOUL.md is downgraded into `AGENTS.md`. |
| 5 | "Let Scion manage orchestration and communication" | ⚠️ **Partly** | Orchestration (spawn/schedule/route) and chat (Slack, Telegram, Discord — **not** Google Chat) exist, but **there is no self-healing**: an evicted agent stays dead until manually resumed. |

---

## Background: what each system is

**kube-agents** — an agentic harness that replaces `kubectl`/`gcloud`/Console with intent-driven agents for GKE fleet operations. Its runtime unit is a single **long-running Hermes gateway pod** (operator-managed Deployment) hosting multiple **profiles**: a Chat Agent front door, a privileged Platform Agent, and dynamically scaffolded per-cluster Cluster Agents. Delegation runs through a **kanban dispatcher** and file-based **handover** channel; chat reaches humans through Hermes' **own** Google Chat/Slack adapters.

**Scion** — a container-based orchestration platform for running many LLM "deep agents" (Claude Code, Gemini CLI, Codex, Hermes, …) concurrently, each isolated in its own container with separate credentials and a git worktree. A Hub + Runtime Broker manage agent lifecycle across Docker/Podman/Kubernetes backends.

---

## The critical finding: two meanings of "Hermes"

Both projects depend on `nousresearch/hermes-agent`, but use it in opposite modes.

| | kube-agents | Scion `hermes` harness |
|---|---|---|
| Package extra | `hermes-agent[google_chat]` | `hermes-agent[vertex]` |
| Launch command | `hermes gateway run` | `hermes chat --yolo` — `harnesses/hermes/config.yaml:42` |
| Operating mode | Long-running **gateway/server** | Interactive **coding-agent CLI** |
| Input path | Hermes' own ingress (Google Chat/Slack via Pub/Sub pull, Slack socket mode) | Scion injects prompts via `tmux send-keys` — `pkg/runtimebroker/handlers.go:1643` |
| Uses | profiles, kanban dispatcher, Hermes cron, gateway plugin hooks | none of the above |
| Scion's own description | — | "Nous Research's AI **coding agent**" — `harnesses/hermes/README.md` |

Scion's Hermes has **no hook dialect** (`pkg/sciontool/hooks/dialects/` contains only `claude.go`, `codex.go`, `gemini.go`), and its provisioner does only auth resolution, `AGENTS.md` projection, and MCP config — **no chat ingress** (`harnesses/hermes/provision.py`). So on Scion, Hermes is a puppeteered CLI; it does not run its own gateway or chat adapters.

**Consequence:** "deploying our Hermes platform agent on Scion" means one of two things, both costly:

- **Path A — adopt Scion's `hermes chat` harness (Scion-native).** You lose gateway mode entirely: no profiles, no kanban dispatcher, no Google Chat ingress, and gateway plugin hooks (`pre_gateway_dispatch`: `session_store`, `session_otel_bridge`, `tool_call_audit`, `chat_message_audit`) do not fire because there is no gateway. SOUL.md is downgraded into `AGENTS.md` (the harness declares `system_prompt: partial`). MCP tools carry over. This is a re-architecture onto Scion's native multi-agent primitives.
- **Path B — run our existing gateway image as an opaque BYO container on Scion.** You keep all gateway features, but Scion adds ~nothing and actively fights you: it forces a `sciontool init` + `tmux` entrypoint (image must ship both), drives via `send-keys` (meaningless to a gateway), gives **no self-healing**, and offers no easy durable RWO volume. You'd be using Scion as a strictly worse Deployment.

---

## Gaps that matter for an always-on fleet operator

All verified against Scion source.

### 1. No self-healing on eviction (the hard blocker)
Scion's Kubernetes runtime creates a **bare `corev1.Pod`** with `RestartPolicy: Never` and **no controller/informer/reconcile** watching it (`pkg/runtime/k8s_runtime.go:1208,1237,356`). If a pod is evicted (node upgrade, autoscaler scale-down, preemption, OOM) or the node dies, the agent stays **dead** until a human runs `scion resume` (`cmd/resume.go:23`). Stalled agents are **suspended, not relaunched** (`pkg/hub/server.go:1991`).

kube-agents today runs the gateway as an operator-managed **Deployment**, which reconciles it back to healthy automatically. For an always-on monitor + chat front door, losing that is a real reliability regression.

### 2. Persistence model mismatch
kube-agents' `/opt/data` is a durable **RWO PVC** holding profiles, fleet handover records, per-cluster kubeconfigs, `kanban.db`, and cron state. Scion's default `/workspace` is **ephemeral EmptyDir** (`pkg/runtime/k8s_runtime.go:1196`). Durable state requires opting into **NFS/Filestore (RWX)**, shared-dir RWX PVCs, or GCS-fuse; there is **no arbitrary RWO-PVC mount and no hostPath** (`k8s_runtime.go:1498-1505`). Our state layout would need rework.

### 3. Google Chat is a non-functional stub
kube-agents depends on Google Chat. In Scion, only a signing-key constant exists (`pkg/config/integration_config.go`); there is **no Google Chat channel implementation** and it is not in the channel registry (`pkg/hub/channels.go`). Working chat channels are **Slack** (outbound), plus **Telegram/Discord** (full bidirectional). Bidirectional human→agent input works via `tmux send-keys`.

### 4. GCP identity is supported but not automated
Scion sets `serviceAccountName` on the pod for Workload Identity **if you name a pre-bound KSA** (`k8s_runtime.go:1517`, `pkg/api/types.go:318`), but it does **not** create the KSA↔GSA binding, and its metadata-interception path does not work on Kubernetes (the pod drops all capabilities — `k8s_runtime.go:1230`). Our provisioning scripts would still do the binding.

### 5. Scion replaces the runtime, not the platform
LiteLLM, the OTel collector, the GitHub token-minter, and cluster RBAC are all separate dependencies that remain our responsibility regardless of which orchestrator runs the agents.

---

## What Scion genuinely gets right (the case *for* it)

Corrected against source — several capabilities are **better** than an initial doc-based read suggested:

- **Long-running agents** — Scion agents are persistent daemons (persistent tmux, `sleep infinity` when idle, no terminal "completed" phase). It is *not* a task-only runner.
- **Cron scheduler** — first-class `robfig/cron/v3` scheduler with a `dispatch_agent` event that spawns agents on a schedule (`pkg/hub/scheduler.go`, `pkg/hub/server.go:2206`) — directly useful for periodic monitoring.
- **Security / isolation** — the strongest part of the original claim: each agent gets its own container, credentials, and git worktree — materially stronger tenant isolation than kube-agents' shared-pod-identity profiles.
- **Bring-your-own image** — templates set image, command/args, env, volumes, resources (`pkg/api/types.go:437`), subject to the image shipping `sciontool` + `tmux`.
- **Bidirectional chat** — Slack, Telegram, Discord, with human input routed into the agent.

These make Scion a strong fit for **ephemeral, isolated specialist agents** — which is precisely how our Cluster Agents and kanban workers already behave.

---

## Recommended path (hybrid)

1. **Keep the always-on gateway on Kubernetes** (operator or a plain Deployment). This is where self-healing, Google Chat ingress, profile-based delegation, and gateway plugin hooks live. Do not move it to Scion.
2. **Evaluate Scion for ephemeral specialist agents.** Our per-cluster Cluster Agents and kanban workers are already one-shot, task-scoped invocations — an ideal match for Scion's isolation model and `dispatch_agent` scheduling. The no-self-healing and ephemeral-state traits do not hurt here.
3. **Seam:** the gateway could call Scion's `dispatch_agent` (or the equivalent API) to spawn an isolated investigation agent per cluster/incident, then collect results — replacing in-pod subprocess scaffolding with hardened, separately-credentialed containers. This captures the **security/isolation** win Gari is pointing at without giving up the reliability of the gateway.

---

## Bottom line

- "It supports Hermes" is **true but not the Hermes we run.** Scion drives Hermes as a coding CLI; we run it as a gateway platform.
- "Easily deploy … and let Scion manage orchestration and communication" is **not accurate for the platform gateway** — it is a re-architecture that drops Google Chat and gateway plugin hooks and regresses self-healing and persistence.
- Scion's real value for us is **security/isolation of ephemeral specialist agents**, not replacing the platform front door. A **hybrid** captures that value without the regressions.

---

## Appendix: source evidence

Scion (`github.com/GoogleCloudPlatform/scion` @ `b4c9911`):

| Finding | File:line |
|---|---|
| Hermes launched as `hermes chat --yolo` (CLI, not gateway) | `harnesses/hermes/config.yaml:42` |
| Hermes = Nous coding agent; installed via pip | `harnesses/hermes/README.md`, `harnesses/hermes/Dockerfile:38` |
| No Hermes hook dialect (only claude/codex/gemini) | `pkg/sciontool/hooks/dialects/` |
| Hermes provisioner does auth/AGENTS.md/MCP only — no ingress | `harnesses/hermes/provision.py` |
| Human input injected via tmux send-keys | `pkg/runtimebroker/handlers.go:1643` |
| K8s object = bare Pod, `RestartPolicy: Never` | `pkg/runtime/k8s_runtime.go:1208,1237,356` |
| No pod reconcile/informer; stalled agents suspended | `pkg/hub/server.go:1991` |
| Manual restart only (`scion resume`) | `cmd/resume.go:23` |
| Default `/workspace` = ephemeral EmptyDir | `pkg/runtime/k8s_runtime.go:1196` |
| Bind/local volumes unsupported on K8s | `pkg/runtime/k8s_runtime.go:1498-1505` |
| BYO image + command/env/volumes/resources | `pkg/api/types.go:437` |
| `serviceAccountName` for Workload Identity (binding not created by Scion) | `pkg/runtime/k8s_runtime.go:1517`, `pkg/api/types.go:318` |
| Pod drops ALL capabilities (metadata interception broken on K8s) | `pkg/runtime/k8s_runtime.go:1230` |
| Cron scheduler + `dispatch_agent` | `pkg/hub/scheduler.go`, `pkg/hub/server.go:2206` |
| Chat channels (Slack/Discord/Email/Webhook; no Google Chat) | `pkg/hub/channels.go` |
| Google Chat is a stub (signing key only) | `pkg/config/integration_config.go` |

kube-agents (this repo):

| Finding | File |
|---|---|
| Base image `nousresearch/hermes-agent`; runs `hermes gateway run` | `deploy/docker/Dockerfile`, `deploy/shared/docker-entrypoint.sh` |
| Google Chat / Slack ingress wired by the operator | `k8s-operator/internal/controller/platformagent_manifests.go` |
| Gateway plugin hooks (`pre_gateway_dispatch`) | `agents/chat/defaults/plugins/`, `agents/platform/plugins/` |
| Durable `/opt/data` RWO PVC (profiles, handover, kanban.db) | `k8s-operator/internal/controller/platformagent_manifests.go` |
