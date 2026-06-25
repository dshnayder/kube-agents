# KubeAgents YOLO Report: Divergence from Upstream Baseline

This document compares the current `yolo-01` branch configuration against the upstream baseline (`gke-labs/main`). It highlights the key architectural and feature differences item-by-item, along with the engineering rationales behind them.

---

## Architectural & Feature Breakdown

### 1. Swarm Thought Streaming & Webhook Grouping
*   **Upstream Baseline (`gke-labs/main`)**: 
    Remote worker/operator agents stream intermediate tool call logs ("thoughts") by firing raw webhook calls directly to the chat interface. Each thought creates a new, separate message bubble in Google Chat, causing excessive screen noise during long-running tasks.
*   **Current State (`yolo-01`)**:
    *   The `session_resolver` plugin is updated to forward the active `session_id` inside the thought webhook payload.
    *   A custom `WebhookAdapter` override (`deploy/shared/overrides/webhook.py`) intercepts `swarm-thought-stream` webhooks.
    *   It caches message IDs by `(session_id, worker_id)` and uses the Google Chat API's `edit_message` capability to append new thoughts to the same message bubble with a 5-minute inactivity TTL.
*   **Rationale/Reason**: Reduces chat interface noise. Consecutive tool execution logs from a single agent now display as a clean, single expanding text block rather than flooding the channel with dozens of individual messages.

### 2. Asynchronous Workload Delegation
*   **Upstream Baseline (`gke-labs/main`)**:
    Platform Agent delegates tasks to specialized subagents synchronously, blocking the main thread and risking webhook/connection timeouts (e.g. Gateway API gateway timeouts) when executing long operations.
*   **Current State (`yolo-01`)**:
    *   Introduces the `delegate_workload` plugin (`agents/platform/plugins/delegate_workload/`) which starts tasks asynchronously via target agent `/v1/runs` endpoints.
    *   Establishes Server-Sent Events (SSE) stream ingestion (`/v1/runs/{id}/events`) to process token outputs and task execution deltas asynchronously.
*   **Rationale/Reason**: Ensures robust multi-agent orchestration. The Platform Agent no longer blocks while waiting for complex, multi-minute GKE operator deployments to complete, preventing HTTP gateway timeouts.

### 3. Multi-Tenant Identity Isolation (Workload Identity)
*   **Upstream Baseline (`gke-labs/main`)**:
    Operator and DevTeam agents run on target clusters sharing generic credentials or local service accounts, leading to privilege escalation risks and security boundary leaks.
*   **Current State (`yolo-01`)**:
    *   Enforces unique, dedicated GCP Google Service Accounts (GSA) per provisioned agent replica.
    *   Automates the GSA creation, Workload Identity User binding, and project-level IAM role assignment during agent registration.
    *   Injects matching KSA-to-GSA annotations (`iam.gke.io/gcp-service-account`) into the dynamic instance templates.
*   **Rationale/Reason**: Satisfies least-privilege security mandates. Isolates the API boundaries of different operator instances so they cannot inspect or modify resources outside their designated application namespaces or clusters.

### 4. Stateful Session Store (SQLite KV Server Sidecar)
*   **Upstream Baseline (`gke-labs/main`)**:
    Sessions are transient and stateless. Context variables (like K8s service host, target namespaces) are not persisted across platform agent restarts or shared with subprocesses.
*   **Current State (`yolo-01`)**:
    *   Added the `session_store` plugin and a session KV server sidecar script (`session_kv_server.py`) running on port `8699` backed by SQLite (`session_kv.db`).
    *   Exposes a list sessions API (`GET /v1/sessions`) and enables persistent, shared thread isolation settings.
    *   Integrates the `session_resolver` plugin to copy and restore context variables across agent boundaries.
*   **Rationale/Reason**: Maintains context continuity during complex, multi-step debugging workflows where agent pods might restart or hand off workloads.

### 5. Kubernetes Operator Controller Redesign
*   **Upstream Baseline (`gke-labs/main`)**:
    The Operator manager controllers contain hardcoded Go-native templates for deploying the platform, operator, and devteam deployments.
*   **Current State (`yolo-01`)**:
    *   Decouples the controller code from deployment manifests. Manifest templates are moved to the Platform Agent defaults directory (`defaults/templates/`).
    *   Introduced a Goldens testing framework to validate generated manifests against expected schema definitions.
    *   Allows KCC `ContainerCluster` CRDs to be applied unconditionally in YOLO mode.
*   **Rationale/Reason**: Improves maintainability. Allows operators and developer templates to be edited directly in YAML files without needing to recompile the Go controller binary, simplifying the deployment cycle.

### 6. YOLO Mode Direct Cluster Mutations
*   **Upstream Baseline (`gke-labs/main`)**:
    Strict GitOps compliance is mandated. All cluster changes must go through pull request approvals and Git branch mutations.
*   **Current State (`yolo-01`)**:
    *   Updated the SOUL instructions for all agent types to recognize a "YOLO mode" when the target git repository URL is set to a placeholder (e.g. `placeholder`).
    *   Under YOLO mode, agents are authorized to execute direct live mutations on target clusters using `kubectl` instead of blocking on Git commits.
*   **Rationale/Reason**: Facilitates fast debugging and prototyping. Allows the agents to perform immediate diagnostics and trial fixes on staging environments without being blocked by Git approvals.

### 7. Compliance & Network Security Policies
*   **Upstream Baseline (`gke-labs/main`)**:
    No strict egress control or network isolation policies are defined for agent runtime environments.
*   **Current State (`yolo-01`)**:
    *   Added custom `NetworkPolicy` manifests for Platform, Operator, and DevTeam namespace environments.
    *   Restricts egress traffic to DNS (port 53) and the required API controllers, blocking arbitrary external network access.
*   **Rationale/Reason**: Pass internal security compliance audits by sandboxing agent execution environments and preventing data exfiltration.
