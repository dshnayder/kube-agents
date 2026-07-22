# Evaluation: Can Scion replace (or host) kube-agents?

**Status:** Evaluation / recommendation
**Date:** 2026-07-22
**Scope:** Whether [Scion](https://github.com/GoogleCloudPlatform/scion) can host the kube-agents Hermes agents and take over orchestration + communication.
**Method:** Verified against source — Scion (`github.com/GoogleCloudPlatform/scion` @ `b4c9911`), Hermes (`github.com/nousresearch/hermes-agent`), and the kube-agents source in this repo. **Not** taken from published docs, which were found inaccurate on several points.

> ⚠️ Naming note: this "Scion" is **not** the SCION networking project (`scionproto/scion`). It is a Google Cloud AI-agent orchestration platform that happens to share the name.

---

## TL;DR

**The idea is viable.** Porting our Hermes platform-agent **content** — SOUL.md, skills, config.yaml, MCP tools, plugin tools — onto Scion is a **minor-rework** exercise, confirmed by reading both the Hermes and Scion source (they corroborate each other). This is *not* a rewrite.

The nuance: Scion runs Hermes as a **CLI** (`hermes chat`), not as our **gateway** (`hermes gateway run`). That means we give up the Hermes *gateway* layer — chat adapters, autonomous cron/kanban dispatch, gateway plugin hooks. But that layer is precisely the **"orchestration and communication"** we would be handing to Scion anyway, so losing it is the intent, not a regression.

The real decision therefore is **not** "can our agent run on Scion" (it can). It is whether we accept three genuine trade-offs:

1. **No self-healing on eviction** — Scion agent pods are bare Pods with `RestartPolicy: Never`; an evicted always-on agent stays dead until manually resumed.
2. **No Google Chat** — Scion has Slack/Telegram/Discord, but Google Chat is a non-functional stub.
3. **Stateful data needs a durable backend** — kanban/session/handover state won't survive on Scion's default ephemeral `/workspace`.

**Recommendation:** Treat this as a viable path worth a proof-of-concept, scoped to validate those three risks. If self-healing and Google Chat are hard requirements for the always-on front door, prefer a **hybrid**: keep the always-on piece on a K8s Deployment and use Scion for ephemeral, strongly-isolated specialist agents (our Cluster Agents / kanban workers) — where Scion's isolation is a clear security win. See [Recommended path](#recommended-path).

---

## Claim-by-claim verdict

Evaluating the original proposition: *"Scion supports hermes, so we could easily deploy the hermes platform agent harness on Scion, add persona types, and let Scion manage orchestration and communication."*

| # | Claim | Verdict | Why |
|---|-------|---------|-----|
| 1 | "Scion solves multi-agent + security problems" | ✅ **Largely true** | Strong per-agent isolation (own container, credentials, worktree), native multi-agent projects, cron scheduler, bidirectional chat. Its real strength. |
| 2 | "It now also supports hermes" | ✅ **True** (with a caveat) | Built-in `hermes` harness exists — but runs Hermes as a **CLI**, not our gateway. Content ports; the gateway layer does not. |
| 3 | "Easily deploy the hermes platform agent on Scion" | ✅ **Content: minor rework** / ⚠️ **orchestration: moderate rework** | Agent content (SOUL.md, skills, config, MCP, plugin tools) ports with minor changes. Re-modelling profile+kanban delegation onto Scion primitives is moderate design work. |
| 4 | "Easily add additional agent/persona types" | ✅ **True** | Scion templates/projects make new personas straightforward — arguably easier than Hermes profiles. Each persona = a template. |
| 5 | "Let Scion manage orchestration and communication" | ⚠️ **Mostly, with gaps** | Orchestration (spawn/schedule/route/message) ✅; chat ✅ Slack/Telegram/Discord but ✗ Google Chat; and **no self-healing** on eviction. |

---

## Background: what each system is

**kube-agents** — an agentic harness that replaces `kubectl`/`gcloud`/Console with intent-driven agents for GKE fleet operations. Its runtime unit is a single **long-running Hermes gateway pod** (operator-managed Deployment) hosting multiple **profiles**: a Chat Agent front door, a privileged Platform Agent, and dynamically scaffolded per-cluster Cluster Agents. Delegation runs through a **kanban dispatcher** and file-based **handover** channel; chat reaches humans through Hermes' **own** Google Chat/Slack adapters.

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

**This is not a blocker — it is a division of labour.** In `hermes chat` mode, the agent's *identity and capabilities* are fully intact (see next section); what the gateway would have provided (chat ingress, autonomous scheduling, dispatch) is instead Scion's job. For the stated goal — "let Scion manage orchestration and communication" — that is exactly the desired split.

---

## Content portability (verified from Hermes **and** Scion source)

The critical question is whether our customizations survive when Hermes runs as `hermes chat` under Scion. They do. Hermes reads its persona/config/skills from `$HERMES_HOME`, and Scion sets `HERMES_HOME=/home/scion/.hermes` and lays content there — the two line up:

| Artifact | Portability | Evidence |
|---|---|---|
| **SOUL.md** (persona) | ✅ **As-is** — Hermes reads it from `$HERMES_HOME` natively in `chat`; Scion never touches it | Hermes `agent/prompt_builder.py:1888`, `system_prompt.py:188`; Scion `provision.py` writes only `.env`/`AGENTS.md`/`mcp.json` |
| **config.yaml** (model, toolsets, plugins, disabled_toolsets) | ✅ **As-is** — honored by `hermes chat`; provisioner doesn't clobber `~/.hermes/config.yaml` | Hermes `config.py:7286`, `model_tools.py:399`; Scion `provision.py` (no config handling) |
| **skills/** (SKILL.md) | ✅ **Rename-only** — must live at `.hermes/skills`; Scion auto-copies template `skills/` | Hermes `skills_tool.py:143`; Scion `provision.go:822` |
| **Plugin tools** (e.g. `handover` → `write_handover`) | ✅ **Works** — registered into the shared tool registry in `chat` mode | Hermes `model_tools.py:204`, `plugins.py:391` |
| **Plugin agent-lifecycle hooks** (tool/LLM/session) | ✅ **Works** in `chat` | Hermes `agent/turn_*`, `run_agent.py` |
| **MCP servers** (gke, platform_control, dev-knowledge) | ⚠️ **Re-declare** — must be set in the Scion template's `mcp_servers`, else a shipped `mcp.json` is **deleted** | Scion `provision.py:181-195` |
| **AGENTS.md** (operating instructions) | ⚠️ **Merge-safe, placement wrinkle** — Scion prepends a managed block; Hermes reads AGENTS.md from **CWD** (`/workspace`) while Scion writes to `$HOME`. Put instructions in SOUL.md to avoid this | Hermes `prompt_builder.py:1932`; Scion `scion_harness.py:822` |

**Gateway-only features that do NOT run under `hermes chat`** — and their Scion replacement:

| Lost (gateway-only) | Replaced by |
|---|---|
| Chat adapters (Slack / Google Chat) | Scion channels (Slack ✅, Telegram/Discord ✅; **Google Chat ✗**) |
| `pre_gateway_dispatch` hooks (`session_store`, `session_otel_bridge`, `tool_call_audit`, `chat_message_audit`) | Scion's own session store, OTel telemetry, and audit |
| Autonomous cron ticker + kanban auto-dispatch | Scion's cron scheduler + `dispatch_agent` orchestration |
| HTTP API / dashboard | Scion Hub / dashboard (not needed) |

### Packaging recipe (minor rework)

1. **Rebuild the image `FROM scion-base`**, then `pip install hermes-agent` and copy our content. `scion-base` supplies the `sciontool` + `tmux` + `python3` the pod entrypoint requires (`k8s_runtime.go:933`, `harnesses/hermes/config.yaml:27`).
2. **Place profile content under `~/.hermes/`** (SOUL.md, config.yaml) and `.hermes/skills/` — via a Scion template `home/` tree (auto-copied, `provision.go:787`) or baked into the image.
3. **Re-declare MCP servers** in the Scion template's `mcp_servers` (translated into `~/.hermes/mcp.json` by the provisioner).
4. **Keep operating instructions in SOUL.md** to sidestep the AGENTS.md CWD-vs-HOME wrinkle; expect Scion to prepend a managed block to AGENTS.md.
5. **Add a durable backend** for mutable state (see below) if kanban/sessions/handover must persist.

---

## The three genuine trade-offs

These survive scrutiny and are the real decision factors — none is about content portability.

### 1. No self-healing on eviction (the hard one)
Scion's Kubernetes runtime creates a **bare `corev1.Pod`** with `RestartPolicy: Never` and **no controller/informer/reconcile** watching it (`pkg/runtime/k8s_runtime.go:1208,1237,356`). On eviction (node upgrade, autoscaler scale-down, preemption, OOM) or node loss, the agent stays **dead** until a human runs `scion resume` (`cmd/resume.go:23`); stalled agents are **suspended, not relaunched** (`pkg/hub/server.go:1991`). kube-agents today runs the gateway as an operator-managed **Deployment** that reconciles automatically. For an always-on monitor + chat front door this is a real reliability regression — mitigable only by external supervision or by keeping the always-on piece on a Deployment.

### 2. Google Chat is a non-functional stub
kube-agents depends on Google Chat. In Scion, only a signing-key constant exists (`pkg/config/integration_config.go`); there is **no Google Chat channel implementation** and it is not in the channel registry (`pkg/hub/channels.go`). Working channels: **Slack** (outbound), **Telegram/Discord** (bidirectional). If Google Chat is required, this is a gap (build the channel, or switch to Slack).

### 3. Stateful data needs a durable backend
Image-baked content (SOUL.md, skills, config) is fine — it is rebuilt each launch. But **mutable** state — `kanban.db`, sessions, handover records — will not persist on Scion's default `/workspace` (**ephemeral EmptyDir**, `k8s_runtime.go:1196`). Durable state requires **NFS/Filestore (RWX)**, shared-dir RWX PVCs, or GCS-fuse; there is **no arbitrary RWO-PVC mount and no hostPath** (`k8s_runtime.go:1498-1505`). This is rework of our state layout.

### Also note (not blockers)
- **GCP identity**: Scion sets `serviceAccountName` for Workload Identity if you name a pre-bound KSA (`k8s_runtime.go:1517`, `pkg/api/types.go:318`), but does **not** create the KSA↔GSA binding (our provisioning scripts still do), and its metadata-interception path doesn't work on K8s (pod drops all caps, `k8s_runtime.go:1230`).
- **Scion replaces the runtime, not the platform**: LiteLLM, the OTel collector, the GitHub token-minter, and cluster RBAC remain our responsibility regardless.
- **Orchestration re-modelling** (moderate): our Chat→Platform→Cluster profile + kanban delegation maps onto Scion projects + `dispatch_agent` + inter-agent messaging — doable, but design work, not a copy.

---

## What Scion genuinely gets right (the case *for* it)

- **Long-running agents** — persistent daemons (persistent tmux, `sleep infinity` when idle, no terminal "completed" phase). Not a task-only runner.
- **Cron scheduler** — first-class `robfig/cron/v3` with a `dispatch_agent` event that spawns agents on schedule (`pkg/hub/scheduler.go`, `pkg/hub/server.go:2206`) — directly useful for monitoring.
- **Security / isolation** — the strongest part of the original claim: each agent gets its own container, credentials, and git worktree — materially stronger isolation than kube-agents' shared-pod-identity profiles.
- **Bring-your-own image** — templates set image, command/args, env, volumes, resources (`pkg/api/types.go:437`), given the image ships `sciontool` + `tmux`.
- **Bidirectional chat** — Slack, Telegram, Discord, with human input routed into the agent.

---

## Recommended path

The proposition is viable. De-risk it with a scoped proof-of-concept before committing:

1. **PoC the platform agent on Scion.** Rebuild the image per the [recipe](#packaging-recipe-minor-rework), ship our SOUL.md/skills/config, re-declare MCP, and drive it via Scion. This validates content portability end-to-end (low effort, high signal).
2. **Explicitly test the three trade-offs:** kill the pod / drain the node and observe recovery (self-healing); confirm the chat channel (Slack vs Google Chat requirement); wire a durable backend for kanban/session/handover state.
3. **If self-healing or Google Chat are hard requirements, go hybrid:** keep the always-on front door on a K8s Deployment (self-healing + Google Chat + delegation), and use Scion for **ephemeral specialist agents** — our per-cluster Cluster Agents and kanban workers are already one-shot invocations and are an ideal match for Scion's isolation and `dispatch_agent` scheduling. The gateway can call `dispatch_agent` to spawn a hardened, separately-credentialed investigation agent per cluster/incident — capturing the security/isolation win without the reliability regression.

---

## Bottom line

- **"It supports hermes" — true.** Our agent *content* (SOUL.md, skills, config, MCP, plugin tools) ports with **minor rework**; both source trees confirm it.
- **"Let Scion manage orchestration and communication" — mostly yes, by design.** The Hermes gateway features we lose are the ones Scion replaces. Real gaps: **no Google Chat** and **no self-healing on eviction**.
- **The decision is about trade-offs, not feasibility.** If we can live without automatic eviction recovery (or supervise it externally), don't need Google Chat, and rework state onto a durable backend, deploying our Hermes agent(s) on Scion — and adding persona types as templates — is a realistic architecture. Otherwise, a hybrid captures Scion's security/isolation strengths while keeping the always-on front door reliable.

---

## Appendix: source evidence

Scion (`github.com/GoogleCloudPlatform/scion` @ `b4c9911`):

| Finding | File:line |
|---|---|
| Hermes launched as `hermes chat --yolo` (CLI, not gateway) | `harnesses/hermes/config.yaml:42` |
| Hermes provisioner: writes `.env`/`AGENTS.md`/`mcp.json` only; never touches SOUL.md/config.yaml | `harnesses/hermes/provision.py` |
| Template `home/` tree copied into agent home | `pkg/agent/provision.go:787` |
| Template `skills/` copied to `.hermes/skills` | `pkg/agent/provision.go:822` |
| AGENTS.md merged (managed block prepended), not overwritten | `harnesses/scion_harness.py:822` |
| `mcp.json` deleted if template declares no MCP servers | `harnesses/hermes/provision.py:181-195` |
| `HERMES_HOME=/home/scion/.hermes`; CWD=`/workspace`; no `-p` flag | `harnesses/hermes/provision.py:232`, `k8s_runtime.go:1224`; `pkg/harness/declarative_generic.go:93` |
| Image must ship sciontool + tmux + python3 + hermes | `k8s_runtime.go:933`, `harnesses/hermes/config.yaml:27`, `image-build/scion-base/Dockerfile` |
| Human input injected via tmux send-keys | `pkg/runtimebroker/handlers.go:1643` |
| K8s object = bare Pod, `RestartPolicy: Never` | `pkg/runtime/k8s_runtime.go:1208,1237,356` |
| No pod reconcile/informer; stalled agents suspended; manual `scion resume` | `pkg/hub/server.go:1991`, `cmd/resume.go:23` |
| Default `/workspace` = ephemeral EmptyDir; no hostPath/RWO-PVC | `pkg/runtime/k8s_runtime.go:1196,1498-1505` |
| BYO image + command/env/volumes/resources | `pkg/api/types.go:437` |
| `serviceAccountName` for Workload Identity (binding not created by Scion) | `pkg/runtime/k8s_runtime.go:1517`, `pkg/api/types.go:318` |
| Pod drops ALL capabilities (metadata interception broken on K8s) | `pkg/runtime/k8s_runtime.go:1230` |
| Cron scheduler + `dispatch_agent` | `pkg/hub/scheduler.go`, `pkg/hub/server.go:2206` |
| Chat channels (Slack/Discord/Email/Webhook; Google Chat is a stub) | `pkg/hub/channels.go`, `pkg/config/integration_config.go` |

Hermes (`github.com/nousresearch/hermes-agent`) — what `hermes chat` honors:

| Finding | File:line |
|---|---|
| SOUL.md read from `$HERMES_HOME` as identity, in chat mode | `agent/prompt_builder.py:1888`, `agent/system_prompt.py:188` |
| AGENTS.md read from **CWD** as project context (additive to SOUL.md) | `agent/prompt_builder.py:1932` |
| Profiles (`-p`) work in chat mode | `hermes_cli/main.py:476-630` |
| Skills loaded from `$HERMES_HOME/skills`; synced on launch | `tools/skills_tool.py:143`, `hermes_cli/main.py:2542` |
| config.yaml honored (model, mcp_servers, toolsets, plugins, disabled_toolsets) | `hermes_cli/config.py:7286`, `model_tools.py:399` |
| MCP servers spawned/connected in chat | `cli.py:1030-1034` |
| Plugin tools register into shared registry (work in chat) | `model_tools.py:204`, `plugins.py:391` |
| `pre_gateway_dispatch` hook is gateway-only | `gateway/run.py:10322-10359` |
| Chat adapters / HTTP API / autonomous cron+kanban are gateway-only | `gateway/run.py` (adapters `9487`, cron `23360`, kanban `8161`) |

kube-agents (this repo):

| Finding | File |
|---|---|
| Base image `nousresearch/hermes-agent`; runs `hermes gateway run` | `deploy/docker/Dockerfile`, `deploy/shared/docker-entrypoint.sh` |
| Google Chat / Slack ingress wired by the operator | `k8s-operator/internal/controller/platformagent_manifests.go` |
| Gateway plugin hooks (`pre_gateway_dispatch`) | `agents/chat/defaults/plugins/`, `agents/platform/plugins/` |
| Durable `/opt/data` RWO PVC (profiles, handover, kanban.db) | `k8s-operator/internal/controller/platformagent_manifests.go` |
