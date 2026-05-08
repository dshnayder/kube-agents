# Kubernetes Agent - OpenClaw Integration

This directory contains the integration components for bringing specialized Kubernetes (k8s) AI agents and expert skills directly into the [OpenClaw](https://openclaw.ai/) ecosystem.

## What is Installed?

When you run the installation script, it enriches your OpenClaw environment with a multiagent cooperative layout and specialized Kubernetes skills:

1. **Kubernetes Operator Agent (`operator`)**: An autonomous custodian of the infrastructure. It manages global cluster concerns (multi-cluster balancing, capacity scaling, version upgrades, security patching) and executes scheduled operational cron tasks (health patrols, CVE scans, log rotations, backup validation).
2. **Development Team Agent (`devteam`)**: A production-safety coach and application workload custodian. It acts as the developers' first-responder, automating manifest validation, PR reviews (enforcing requests/limits and Pod Security Standards), canary rollouts, dependency management, and incident root-cause analysis.

---

## Agent Delegation & Routing Policy

The "main" agent acts as the primary orchestrator and dispatcher. It uses a strict routing guide (`ROUTING.md`) to safely delegate incoming developer requests to the most appropriate specialized subagent:

### 1. Quick Routing Commands (TUI & Shared Chat Shortcuts)
- **`@devteam <task>`**: Routes development-related work (writing code, manifests, build pipelines, rollouts, application-level bug fixes and debugging).
- **`@operator <task>`**: Routes cluster/platform operations (cluster health, scaling, upgrades, platform policies, cert scans, global security patches).
- **`@main <task>`**: Routes coordination, tradeoffs verification, planning, and human-in-the-loop communication.

### 2. Key Agent Responsibilities Matrix

| Feature Area | Primary Agent | Action Role |
|---|---|---|
| **App Code / Bug Fixes** | `devteam` | Complete code changes, compilation, staging debugging. |
| **Builds & Pipelines** | `devteam` | Manage Helm updates, container builds, SBOM verification. |
| **App Deployments** | `devteam` | Execute canary rollouts, monitor error thresholds (>1% auto-revert). |
| **Cluster Operations** | `operator` | Execute upgrades, tune fleet quotas, handle auto-remediation (e.g., restart hung kubelet). |
| **Platform Policies** | `operator` | Provision namespaces, enforce default-deny network policies. |
| **Coordination & Review** | `main` | Interpret user intent, verify subagent proof before reporting success. |

### 3. Strict Proof Gates
Before the main agent reports success to the human operator, it enforces strict proof validation gates:
- **For Development Tasks**: Requires Git commit SHAs, changed files listing, build/compilation terminal outputs, container digests (`@sha256`), and live deployment status evidence (`kubectl get deploy/pods/svc`).
- **For Operational Tasks**: Requires active context checking (`kubectl config current-context`), resource inspection scope, before/after state comparisons, and event/log evidence.

---

## Installation

You can install and configure the entire integration (agents, skills, and configuration) using a single command:

```bash
curl -fsSL https://raw.githubusercontent.com/gke-labs/kube-agents/main/openclaw/scripts/install.sh | bash
```

### Installing from a Custom Branch or Fork

If you are testing from a fork or a custom branch, export the `REPO` and `BRANCH` environment variables first:

```bash
export REPO="https://github.com/<owner>/kube-agents"
export BRANCH="<branch-name>"
curl -fsSL "${REPO}/raw/${BRANCH}/openclaw/scripts/install.sh" | bash
```

---

## Getting Started

Once installation is complete, restart your OpenClaw gateway if it is already running.

You can interact with your new cooperative agent layout in two ways:

### 1. Chat with the Main Coordinator (Recommended)
To see the **Agent Delegation & Routing Policy** in action, start the standard OpenClaw TUI session (which connects you to the **Main Agent**):

```bash
openclaw tui
```

Once inside, you can use the routing shortcuts to delegate tasks:
- `@devteam Implement a new React checkout component in repo X...`
- `@operator Audit the current cluster egress policies...`
- Or simply describe your task and let `main` automatically interpret your intent and route it.

### 2. Chat Directly with a Subagent
If you want to open a direct session with a specialized agent (bypassing the coordinator), launch the TUI with their specific session key:

- **Kubernetes Operator**:
  ```bash
  openclaw tui --session agent:operator:main
  ```
- **Development Team Agent**:
  ```bash
  openclaw tui --session agent:devteam:main
  ```## Showcasing the Harness in Action (Demo Scenarios)

After running the installer, you can immediately test and showcase the advanced capabilities of the Kubernetes agentic harness using these structured chat scenarios. 

To run these, start the standard TUI session to chat with the **Main Coordinator** (using `openclaw tui`).

### 1. Infrastructure Scope Binding (Cluster Operator Agent)
*   **Scenario**: Tell the active `operator` agent to bind its operational scope to a target cluster and region.
*   **Chat Command**:
    ```bash
    @operator Use cluster 'payment-prod' in 'us-central1'.
    ```
*   **Expected Behavior**: The `operator` agent will parse the request, locate the `payment-prod` context in `us-central1`, configure its internal `kubectl` credentials, and respond with confirmation of its new active target scope.

### 2. Workload Onboarding & Deployment (Development Team Agent)
*   **Scenario**: Instruct the active `devteam` agent to deploy a specific application to a target namespace.
*   **Chat Command**:
    ```bash
    @devteam Deploy the payment-gateway application to the 'payment' namespace.
    ```
*   **Expected Behavior**: The `devteam` agent will generate the necessary deployment and service manifests, verify them against Pod Security Standards, execute the rollout in the `payment` namespace, and return proof (such as Git SHAs, container digests, and successful rollout status).

### 3. GitOps Manifest Resiliency & Autonomy (Development Team Agent)
*   **Scenario**: Ask the `devteam` agent to configure node-failure resiliency for a deployment. The agent must autonomously determine and apply the correct Kubernetes strategy (such as configuring `PodAntiAffinity` or pod topology spread constraints) without being given the solution.
*   **Chat Command**:
    ```bash
    @devteam Make my payment-gateway deployment tolerant to node failures.
    ```
*   **Expected Behavior**: The `devteam` agent will audit the deployment manifest, identify that all replicas could fail if a single node goes down, autonomously inject a `podAntiAffinity` block into the manifest, apply the change via GitOps, and explain the rationale for its fix.

### 4. Proactive Audit & Auto-Remediation (Cluster Operator Agent)
*   **Scenario**: Manually trigger a diagnostic patrol to show how the agent identifies and automatically remediates infrastructure failures (e.g., simulating a hung worker node `kubelet` that causes `NodeNotReady`).
*   **Chat Command**:
    ```bash
    @operator Perform a cluster health check.
    ```
*   **Expected Behavior**: The `operator` agent will scan all nodes, identify a node in `NotReady` status, execute its remediation playbook to automatically restart the hung `kubelet` process via SSH, verify the node returns to `Ready`, and post a clean incident timeline and trace log summary to the chat.

### 5. Cross-Agent Handoff & Resource Negotiation (Visible Collaboration)
*   **Scenario**: Showcase how the `operator` and `devteam` agents collaborate, negotiate resource optimizations, and relay requests transparently.
*   **Trigger Command**:
    ```bash
    @operator Analyze resource utilization and suggest optimizations.
    ```
*   **Expected Cooperative Flow**:
    1.  `operator` audits metrics, identifies potential savings by bin-packing, and sends a direct request: `@devteam Proposing a 30% reduction in on-demand nodes for payment-gateway to optimize resource spending. Do you approve?`
    2.  The `main` agent automatically intercepts and **relays** this proposal to the `devteam` session.
    3.  `devteam` audits its historical latency profiles and SLO metrics, determines that CPU throttling would cause unacceptable cold-start degradation, and replies: `@operator Rejecting proposal. Historical performance telemetry shows CPU throttling causes severe cold-start latency degradation, violating our payment-gateway SLO.`
    4.  `main` automatically **relays** the rejection back to `operator` and **mirrors the entire negotiation chat thread** to your active TUI chat screen, allowing the human operator to transparently review the collaborative cost-vs-performance decision.

---

## References

- [OpenClaw Documentation](https://docs.openclaw.ai/)
- [Building OpenClaw Plugins](https://docs.openclaw.ai/plugins/building-plugins)
