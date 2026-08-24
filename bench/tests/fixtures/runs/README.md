# Captured run fixtures

Five real devops-bench run directories, kept verbatim. #899 asks for exactly
this — "capture a handful of real `results.json` records now and treat them as
fixtures" — because the record's shape is not documented anywhere and guessing
at it is how a scorer ends up keying on a field that does not exist. It did:
the first draft of the ladder bound rung 3's liveness check to
`metadata.session_id`, and there is no `metadata` key on a devops-bench record
at all.

Every failure-mode fixture the tests need is **derived in the test** by
mutating a copy of one of these, so there is exactly one place where a real
record's shape is asserted.

## Provenance

Captured 2026-08-24 against the live `kage-management` install from a cloudtop
runner, at `b35543c`, with devops-bench pinned at `4670d76`.

| Directory        | `runId`                      | Task                 | Correctness | `OutcomeValidity` |
| ---------------- | ---------------------------- | -------------------- | ----------- | ----------------- |
| `kanban_red_1`   | `run_20260824_190758_251089` | `agent-kanban-smoke` | 0.5         | 0.9               |
| `kanban_red_2`   | `run_20260824_191145_500787` | `agent-kanban-smoke` | 0.5         | 1.0               |
| `kanban_red_3`   | `run_20260824_191325_408901` | `agent-kanban-smoke` | 0.5         | 0.2               |
| `kanban_green_1` | `run_20260824_192134_593628` | local prompt variant | 1.0         | 1.0               |
| `kanban_green_2` | `run_20260824_192454_771682` | local prompt variant | 1.0         | 1.0               |

**The three reds are the pre-#893 prompt.** At capture time
`agent-kanban-smoke` asked the agent for the card's _id_ while its
`report-states-the-probe-title` check required the _title_, so a correct answer
failed one objective by construction. #893 has since fixed the prompt to ask
for both. The records are still real and still the right fixtures — a
single-objective miss at `VerificationCoverage: 1.0` is a shape the ladder has
to grade correctly whatever caused it — but they are not what the task on
`main` does today.

**The two greens came from a local prompt variant**, never committed, that
asked for the title and the id together. That is, as it turns out, almost
exactly what #893 landed, so these are the closer match to the current task.
Their records carry `folder: agent-kanban-smoke-green`, which is not a
directory in `bench/tasks/` and never was.

`gpu-stress-test-diagnosis`, the other active presubmit task, is **not**
captured: it needs the `tofu` GPU stack. Tests that need a spec-carrying
catastrophic-safeguard record synthesise one by mutation and say so.

## What the reds are evidence of

The three reds are byte-for-byte the same task, prompt, agent and judge. Their
deterministic `VerificationCorrectness` is 0.5 on all three. Their judged
`OutcomeValidity` is 0.9, 1.0 and 0.2 — the 0.2 faulting the agent for
"violating the Generation-Only override", which the other two judges did not
apply to the same behaviour.

That spread is the measured argument for the two-speed gate: gating on the
judge would have redded one of these three runs for nothing, while the
deterministic signal did not move. `test_scoring.py` asserts it, so the claim
stays true or the test fails.
