---
name: decision-memory
description: Check, recall, and explain infrastructure decisions and lessons in the GitOps repository. Validate changes against guards, explain configuration rationale, and draft lessons from verified outcomes.
---

# decision-memory

The GitOps repository stores infrastructure decisions and operational lessons as reviewed records under `knowledge/decisions/` and `knowledge/lessons/` (`docs/architecture/06-api-and-data-contracts.md` §5).
Use this skill to check those records before you modify a file or review a change.
Every record update requires a pull request that a human merges.

All commands output JSON (or Markdown with `--markdown`) and run against a local GitOps repository checkout.
`scripts/decision_memory.py` does not import any agent harness packages.

## When to use

| Scenario                                                                   | Command                                | Required action                                                                                                                                                                 |
| -------------------------------------------------------------------------- | -------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Propose a change (`submit-suggestion`, `fleet-audit`, manifest generation) | `check --base <base> --semantic`       | Stop on any `violates` finding with `severity: block`. Revise the change or ask the user. Include the `--markdown` output under **Prior decisions and lessons** in the PR body. |
| Review a pull request                                                      | `check --base <base> --head <pr-head>` | Post a comment for each `violates` finding with the record ID and its evidence PR URL. Never approve or merge the PR.                                                           |
| Answer "why is X configured this way?"                                     | `why <path>`                           | Cite matching record IDs. If `records` is empty, state that no decision is recorded, show `history`, and offer to draft a record. Never invent a reason from commit logs.       |
| Record a verified fix or rejected PR                                       | `from-outcome <outcome.json>`          | Open a pull request with the drafted lesson record for human review.                                                                                                            |

## Verdicts

- `violates`: The change breaks a recorded guard. `block` means abort the proposal. `warn` means proceed only if the PR body explains why the previous decision no longer applies.
- `related`: The change modifies a governed path or resource without a guard violation. Explain in the PR body how the change relates to the record.
- `consistent`: The change satisfies the evaluated guard.
- `semantic`: The Hindsight index returned similar records outside the explicit file scope. Verify whether they apply.

When `check` returns `no_match`, report _"No recorded decision governs these paths"_.
Never claim that no decision exists.

## Record schema

```yaml
---
type: lesson # decision | lesson
id: LES-2026-001 # DEC-YYYY-NNN | LES-YYYY-NNN
title: checkout OOM-kills below 256Mi
status: active # proposed | active | superseded | retired
date: 2026-09-10
scope: # include at least one key
  paths: ["clusters/*/payments/checkout.yaml"]
  resources: ["Deployment/payments/checkout"]
  clusters: [prod-eu]
  checks: [oom-kill]
guard: # optional machine-checked rule
  rule: Keep checkout memory request at or above 256Mi.
  severity: block # block | warn | info
  expect:
    file: "clusters/*/payments/checkout.yaml"
    path: "spec.template.spec.containers[name=app].resources.requests.memory"
    op: ">=" # == != >= > <= < in contains
    value: 256Mi
evidence:
  pull_request: https://github.com/acme/gitops/pull/31
  commit: 1a2b3c4
  merged_by: alice
  finding_id: oom-kill.prod-eu.payments.checkout
supersedes: []
---
## What happened
## What fixed it
## How we know it worked
## Prevent it next time
```

Escape literal dots inside YAML keys with a backslash: `spec.hard.requests\.memory`.

## Commands

```bash
python3 scripts/decision_memory.py --repo <clone> validate
python3 scripts/decision_memory.py --repo <clone> check --base origin/main --semantic --markdown
python3 scripts/decision_memory.py --repo <clone> why clusters/prod-eu/payments/quota.yaml
python3 scripts/decision_memory.py --repo <clone> from-outcome outcome.json
python3 scripts/decision_memory.py --repo <clone> dream
python3 scripts/decision_memory.py --repo <clone> index
```

Pass `--hindsight-url` (or set `HINDSIGHT_API_URL`) to query the `kube-agents-decisions` Hindsight bank.
Without Hindsight, `check`, `why`, and `dream` operate from git alone.
