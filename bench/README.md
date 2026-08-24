# bench

Evaluation harness that runs [kubernetes-sigs/devops-bench](https://github.com/kubernetes-sigs/devops-bench) against the Platform Agent, with devops-bench consumed as a pip-installed library (pinned git SHA — no PyPI release yet) instead of the legacy evaluator baked into the eval image. Tasks and the agent transport live here, so kube-agents and devops-bench ship independently.

## Layout

- `kube_agents_bench/harness.py` — the `kubeagents` agent harness: establishes `kubectl port-forward` to `svc/platform-agent` when the local port is closed, POSTs the task prompt to `/v1/responses`, and waits out any work the agent delegates to a subagent. Environment variables are documented in the module docstring.
- `kube_agents_bench/parsing.py` — pure payload and trajectory reading: maps a response onto devops-bench's canonical `AgentResult`, and reads back which kanban cards a turn filed, what statuses it reported, and what a finished card delivered.
- `kube_agents_bench/cuj.py` — black-box CUJ evaluator for the portal's shared
  `/api/v1` interaction contract. It waits for aggregate terminal state before
  producing assertions.
- `kube_agents_bench/cases.py`, `scoring.py`, `baselines.py`, `gate.py` — the presubmit's verdict, described under [The gate](#the-gate) below. Nothing devops-bench calls; these read the records it writes.
- `tasks/` — task definitions. `agent-kanban-smoke` is a no-infrastructure smoke task that exercises the whole pipeline using only toolsets the deployed agent actually ships with.
- `baselines/` — checked-in screening evidence, one append-only JSONL file per case, one screening campaign per line, each keyed on the five software versions a score depends on. See [baselines/README.md](baselines/README.md).
- `scenarios/` — evaluation matrices using `Agent + Persona + Scenario + Goals
-> Run -> Assertions` terminology.
- `tests/` — offline tests: the harness against a local HTTP stub, and the gate against real run records captured from a live cluster (`tests/fixtures/runs/`).

To add a task or plug in a different agent, see
[CUSTOM-TASKS.md](CUSTOM-TASKS.md).

## Running evals

```bash
cd bench
uv sync
PLATFORM_AGENT_TOKEN=$(kubectl get secret platform-agent-secrets -n <namespace> \
  -o jsonpath='{.data.API_SERVER_KEY}' | base64 --decode) \
  JUDGE_PROVIDER=<provider> JUDGE_MODEL=<model> \
  uv run devops-bench ./tasks --no-infra --agent-type kubeagents
```

This is the stock `devops-bench` CLI — there is no wrapper command. `source` is positional. Drop `--no-infra` for tasks that provision infrastructure, and see `--help` for the rest.

## The gate

devops-bench scores a run; it does not decide whether a pull request may merge. That decision is `bench-gate`, and `hack/ci-eval-pr.sh` is what drives it: the shell runs each task `EVAL_REPETITIONS` times (default 3) and hands the resulting run **directories** to the scorer, which reads `results.json` for the scores, `manifest.json` for `setupId`, and `rows.json` for `scoringVersion`.

```bash
uv run bench-gate case --task ./tasks/<id>/task.yaml \
  --result <run-dir> --result MISSING --result <run-dir> \
  --json-out case-<id>.json          # exit 0 with a verdict, 2 if it could not grade
uv run bench-gate suite --case-result case-<id>.json --markdown-out verdict.md
                                     # exit 0 green, 1 red
```

The gate is rate-based, not all-must-pass: at a few hundred cases and realistic per-case reliability, "every case green" is a state the suite reaches on a vanishing fraction of runs, and a gate that reds most pull requests gets switched off. So a case is graded on a ladder — a forbidden action, a declared check that never ran, or a record that is not a real agent run reds the job outright; a case that merely _fails_ reds it only by failing **every** repetition, and only once the baseline store holds screening evidence that the case is reliable enough to mean something.

That evidence is what `baselines/` holds, and admission is computed from it rather than declared in `task.yaml` — a case cannot admit itself in the same diff that makes it pass. A case with no record at the current version key is reported unadmitted, and one whose record was measured on different software is reported _stale_ rather than silently compared against.

Two speeds, deliberately: the deterministic `Verification*` scores gate, and the judged scores are recorded but never block. The captured fixtures are the argument — three byte-identical failing runs scored `OutcomeValidity` 0.9, 1.0 and 0.2 while `VerificationCorrectness` held at 0.5 on all three.

Thresholds are named environment variables, all with the documented default: `EVAL_REPETITIONS`, `DETERMINISTIC_CORRECTNESS_FLOOR`, `EVAL_ADMISSION_RATE`, `EVAL_ADMISSION_MIN_RUNS`, `EVAL_AGGREGATE_MARGIN`, and `BOOTSTRAP_ADMITTED` (the transition bridge — cases that keep blocking before any screening exists).

## Portal CUJ evaluations

The portal evaluator is the black-box path for conversational CUJs with
asynchronous work. It creates an interaction, observes approvals according to
the Persona, waits until the root run and delegated tasks are terminal, and only
then evaluates Goals. It does not modify kube-agents to signal test completion.

The matrix terms are:

- **Agent** — portal API endpoint, black-box agent ID, and profile.
- **Persona** — the complete user role, actor identity/credential reference,
  description, and approval policy.
- **Scenario** — prompt, timeout, polling policy, and ordered Goals.
- **Tool Goal** — requires trusted `toolCalls` evidence. Response prose or a
  promise to act cannot pass it.
- **Message Goal** — required/forbidden response signals plus an optional
  semantic rubric.
- **Soft Goal** — quality rubric with deterministic limits and an injected
  semantic judge. Without a judge its assertion is inconclusive, never passed.
- **Run** — one observed conversation and terminal interaction projection.
- **Assertion** — pass, fail, or inconclusive evidence for completion or one
  Goal, including repair diagnostics.

When a Persona's credential reference resolves to a token, its Agent endpoint
must use HTTPS, except on a loopback host (`127.0.0.1`, `::1`, `localhost`),
where the token never leaves the machine. The evaluator rejects redirects
instead of forwarding the credential; configure the Agent with the final
canonical API URL.

Run the checked-in read-only smoke matrix against a locally running portal.
Every portal `/api/v1` request requires the portal's launch capability, so set
`KUBE_AGENTS_PORTAL_API_TOKEN` (at least 32 characters) before starting
`scripts/admin_portal.sh` — otherwise the portal generates a random token the
evaluator cannot know — and run the matrix with the same value:

```bash
cd bench
KUBE_AGENTS_PORTAL_API_URL=http://127.0.0.1:8501/api/v1 \
KUBE_AGENTS_PORTAL_API_TOKEN=<the portal's token> \
EXPECTED_PROJECT=<project> \
EXPECTED_CLUSTER=<cluster> \
EXPECTED_LOCATION=<location> \
uv run python -m kube_agents_bench.cuj scenarios/portal-readonly-smoke.json
```

The command prints the real user and assistant messages plus the complete
interaction and assertions as JSON. Exit status is zero only when the
interaction completed and every Goal passed. Portal coverage exercises the Chat
Agent front door and its delegation chain; Google Chat Pub/Sub and Slack ingress
remain separate transport Scenarios.

`hack/ci-eval-pr.sh` exports `PLATFORM_AGENT_TOKEN` for you in CI. The harness also honours the same `AGENT_*` variables as the legacy runner.

Tasks that provision infrastructure name their OpenTofu stack relative to `BENCH_TF_ROOT`; point it at a stack directory in this repo so the eval never depends on stacks bundled with the library:

```bash
AGENT_CLUSTER_CONTEXT=gke_<project>_<location>_<agent-cluster> \
  PROJECT_ID=<project> CLUSTER_NAME=<task-cluster> \
  BENCH_TF_ROOT=./tf uv run devops-bench ./tasks --agent-type kubeagents
```

`PROJECT_ID` and `CLUSTER_NAME` are required once infrastructure is on; without them the run exits before provisioning. Set `AGENT_CLUSTER_CONTEXT` for these too. Provisioning a task cluster runs `gcloud container clusters get-credentials`, which repoints kubectl's current context at it; without the pin, the harness port-forwards into the task cluster, where the agent does not run.

A stack under `tf/` does not have to vendor the upstream OpenTofu modules — reference them over git, pinned to a SHA:

```hcl
module "cluster" {
  source = "git::https://github.com/kubernetes-sigs/devops-bench.git//tf/modules/cluster?ref=<sha>"
}
```

The deployer scans `*.tf` in the stack directory only and never descends into modules, so re-declare every variable you want to reach the module in the stack's own `variables.tf` and pass it through. A variable a task's `variables:` block sets but the stack does not declare raises `ConfigError`; one the runner injects is dropped with a log warning.

## Registration

The harness is registered solely by the `devops_bench.agents` entry point declared in `pyproject.toml`. devops-bench scans that group on the first unresolved agent lookup, so `--agent-type kubeagents` resolves without importing this package — nothing in the invocation references `kube_agents_bench` by name. Importing the harness module has no side effects.

## Tests

```bash
cd bench
uv sync
uv run pytest tests
```

No cluster or `kubectl` required — the suite drives the full request → parse → `AgentResult` path against a local stub, and grades the gate against run records captured from a live cluster and then mutated. Every gate failure mode is a mutation of a real record rather than a hand-written dict, so a test cannot agree with the scorer about a field devops-bench does not actually emit; `tests/fixtures/runs/README.md` records where the captures came from.
