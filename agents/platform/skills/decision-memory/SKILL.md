---
name: decision-memory
description: Recall, check and explain recorded infrastructure decisions and lessons learned (knowledge/decisions, knowledge/lessons in the GitOps repository) before proposing a change, when reviewing someone else's change, or when asked why something is configured the way it is. Drafts a lesson from a merged, verified fix. PROTOTYPE — see docs/designs/decision-memory.md.
---

# decision-memory — what we decided, what we learned, and what not to undo

The GitOps repository carries the organisation's infrastructure decisions and the lessons
learned from incidents as reviewed records under `knowledge/decisions/` and
`knowledge/lessons/` (markdown + YAML frontmatter, the OKF contract in
`docs/architecture/06-api-and-data-contracts.md` §5). This skill is how you read them before
you act. It never writes to a cluster and never writes memory directly: a record only changes
through a pull request a human merges.

All commands print JSON (or markdown with `--markdown`) and need a checkout of the GitOps
repository — the clone the GitOps workspace or the credential broker gives you, not this
pod's filesystem. `scripts/decision_memory.py` imports nothing from the agent harness.

## When to use

| Situation | Command | What you must do with the result |
| --- | --- | --- |
| You are about to propose a change (submit-suggestion, fleet-audit remediation, manifest generation) | `check --base <base> --semantic` after your edit, before `submit` | A `violates` finding with severity `block` stops the proposal: change the patch or ask the human. Paste `markdown` into the PR body under **Prior decisions and lessons**. |
| You are reviewing a pull request someone else opened | `check --base <base> --head <pr-head>` | Comment on each `violates` finding citing the record id and the PR it was learned in. Do not approve or merge. |
| Someone asks "why is X configured like this?" | `why <path>` | Cite record ids. If `records` is empty, say no decision is recorded, show `history`, and offer to draft one — never invent a rationale from the history. |
| A fix you proposed was merged and the finding is gone | `from-outcome <outcome.json>` | Put the drafted record in a pull request for a human to merge, or include it in the fix PR before it merges. |

## Reading the verdicts

- `violates` — the change breaks a recorded guard (a machine-checked expectation such as
  "memory request ≥ 256Mi"). `block` means do not propose it as is; `warn` means you may
  propose it only if the PR body says why the decision no longer applies.
- `related` — the change touches what a record governs but no guard was evaluated. Read the
  record and say in the PR how the change relates to it.
- `consistent` — the guard was evaluated and holds.
- `semantic` — records that look related by meaning but are not scoped to these files. Treat
  as leads, not facts.

`no_match` is not evidence that nothing was ever decided — only that no record is scoped to
these paths. Say "no recorded decision", never "no decision exists".

## Record shape

```yaml
---
type: lesson            # or decision
id: LES-2026-001        # DEC-YYYY-NNN for decisions
title: checkout OOM-kills below 256Mi
status: active          # proposed | active | superseded | retired
date: 2026-09-10
scope:                  # what the record governs — at least one of these
  paths: ["clusters/*/payments/checkout.yaml"]
  resources: ["Deployment/payments/checkout"]
  clusters: [prod-eu]
  checks: [oom-kill]    # fleet-audit check ids
guard:                  # optional, machine-checkable
  rule: Keep checkout's memory request at or above 256Mi.
  severity: block       # block | warn | info
  expect:
    file: "clusters/*/payments/checkout.yaml"
    path: "spec.template.spec.containers[name=app].resources.requests.memory"
    op: ">="            # == != >= > <= <  (Kubernetes quantities understood)
    value: 256Mi
evidence:
  pull_request: https://github.com/acme/gitops/pull/31
  commit: 1a2b3c
  merged_by: alice
  finding_id: oom-kill.prod-eu.payments.checkout
supersedes: []          # ids this record replaces
---
## What happened / What fixed it / How we know it worked / Prevent it next time
```

A dot inside a key is escaped: `spec.hard.requests\.memory`.

## Commands

```bash
python3 scripts/decision_memory.py --repo <clone> validate
python3 scripts/decision_memory.py --repo <clone> check --base origin/main --semantic --markdown
python3 scripts/decision_memory.py --repo <clone> why clusters/prod-eu/payments/quota.yaml
python3 scripts/decision_memory.py --repo <clone> from-outcome outcome.json
python3 scripts/decision_memory.py --repo <clone> dream          # scheduled, not interactive
python3 scripts/decision_memory.py --repo <clone> index          # scheduled, not interactive
```

`--hindsight-url` (or `HINDSIGHT_API_URL`) enables semantic recall against the
`kube-agents-decisions` bank. Without it, `check` and `why` still work from git alone.
