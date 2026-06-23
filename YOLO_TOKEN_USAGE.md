# Token Usage & Context Optimization Analysis

This document provides a token usage breakdown for the `kube-agents` harness, outlines optimization steps already completed, and maps out remaining optimization actions.

---

## 1. Initial Token Usage Breakdown Analysis

From inspecting recent request dumps (e.g. `request_dump_*.json`) from active platform, operator, and devteam agents:

### Platform Agent (`platform_dump.json`)
*   **Total Payload Size**: ~210,000 characters (~50,000 tokens)
*   **System Prompt**: 46,174 chars (22%) — Large `SOUL.md` and `AGENTS.md`.
*   **Tool Definitions (Schemas)**: 69,206 chars (33%) — **Largest component.** Full JSON schemas for all 49 registered MCP/core tools sent on every turn.
*   **Tool Results**: 41,070 chars (19.6%) — History of execution output.

### Operator Agent (`operator_dump.json`)
*   **Total Payload Size**: ~197,000 characters (~48,000 tokens)
*   **Tool Results**: 61,401 chars (31.1%) — **Largest component.**
    *   *Culprit:* A single `terminal` command dumped a raw GKE cluster description of **54,046 characters** (54KB) into the LLM context.

### DevTeam Agent (`devteam_dump.json`)
*   **Total Payload Size**: ~229,000 characters (~55,000 tokens)
*   **User Prompts**: 87,459 chars (38.1%) — **Largest component.**
    *   *Culprit:* The harness automatically injected the entire instruction set for the `gke-workload-troubleshooting` skill, adding **87KB** of markdown to the context.

---

## 2. Completed Optimizations

We have addressed the following items to immediately reduce token bloat:

1.  **Pruned Deprecated Grounding Instructions**:
    *   Completely removed references and instructions for the deprecated `answer_query` Developer Knowledge API tool from all platform, operator, and devteam `SOUL.md` files and templates.
    *   Removed `answer_query` recommendation from the `gke-manifest-generation` skill file.
    *   Configured agents to use only the lightweight `search_documents` and `get_document` tools.
2.  **Enforced Context-Efficient CLI Queries**:
    *   Added rules to all `SOUL.md` configs and templates requiring agents to filter/format terminal outputs.
    *   Mandated the use of `--format` flags in `gcloud` (e.g. `--format="yaml(name,status)"`) and query filter paths/custom-columns in `kubectl` to prevent huge raw JSON/YAML configuration dumps.
3.  **Optimized Hermes Core Tool (The "Big 5") Schemas**:
    *   Created and registered the custom python plugin `tool_overrides` which intercepts and replaces standard, wordy schemas for `terminal`, `cronjob`, `delegate_task`, `session_search`, and `skill_manage` with token-efficient wrappers.
    *   Deployed the plugin to GKE and verified massive token savings per LLM turn:
        *   `delegate_task`: 9,760 -> 871 chars (91% reduction)
        *   `cronjob`: 6,826 -> 1,364 chars (80% reduction)
        *   `terminal`: 5,372 -> 736 chars (86% reduction)
        *   `session_search`: 4,692 -> 495 chars (89% reduction)
        *   `skill_manage`: 3,439 -> 889 chars (74% reduction)
        *   **Net savings: ~25.7KB (~6,430 tokens) per API call!**
    *   Moved the overrides to the shared plugins directory `agents/shared/plugins/` to automatically benefit all Platform, Operator, and DevTeam agents.
4.  **Role-Based Tool Stripping**:
    *   Implemented explicit role-based tool limits using `platform_toolsets` configurations under default configs and overlays.
    *   Customized schemas to limit tool definitions based on agent persona:
        *   **Platform Coordinator**: 13 tools (adds `delegation` and `mcp-platform_control`; excludes `browser`).
        *   **Operator SRE**: 11 tools (adds `mcp-worker_control`; excludes `delegation` and `browser`).
        *   **DevTeam Developer**: 13 tools (adds `browser` and `mcp-worker_control`; excludes `delegation`).
    *   Enabled `code_execution` for all agent personas to retain scripting flexibility.
    *   Stripping unused tools (like `browser` for platform/operator) saves ~10.5KB (~2,600 tokens) per turn.
    *   Rebuilt and pushed the docker images to the registry containing these optimizations.

---

## 3. Remaining Items to Address (Roadmap)

To achieve maximum token efficiency, the following items should be addressed:

### A. Compact and Prune Verbose Skill Files
*   **The Issue**: Skill instructions (like `gke-workload-troubleshooting` at 87KB) are too wordy and take up massive prompt space when triggered.
*   **Remediation**: 
    *   Rewrite `SKILL.md` instructions to be extremely concise (targeting under 10KB per file).
    *   Remove conversational padding, and replace detailed procedural lists with high-level checklists.
