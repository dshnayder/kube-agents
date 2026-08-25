#!/usr/bin/env bash
# ==============================================================================
# Prow CI Evaluation Pipeline Script
# ==============================================================================
# Runs devops-bench evaluation against deployed platform-agent.
#
# Evaluates the task matrix in section 6 EVAL_REPETITIONS times per task and
# hands the records to `bench-gate`, which applies the rate-based gate:
# a per-case verdict ladder, a collapse rule that needs every repetition to
# fail on a case with screening evidence, and a suite aggregate. The gate is
# two-speed as before -- deterministic verification keys block, judged scores
# are recorded and gate nothing -- but the decision now lives in tested Python
# (bench/kube_agents_bench/) rather than in inline heredocs here. This script
# keeps what is genuinely shell: the loop, the repetitions, the run-directory
# diffing and the artifact handling.
#
# Why a rate and not a pass: at two hundred cases and 95% per-case
# reliability, "every case passes every run" is clean on 0.003% of runs, and a
# gate that reds seven pull requests in eight is a gate people learn to
# ignore. See bench/baselines/README.md for what admits a case.
# ==============================================================================

set -euo pipefail

# 1. Target Cluster Context
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/ci-env.sh"
# collect_bench_results runs FIRST and on green too -- the baseline store the
# gate compares against is built from passing runs on main, and those are
# exactly the records the old failure-only trap threw away. It must precede the
# dump, which reads `$?` on its first line.
trap 'collect_bench_results; dump_prow_artifacts_on_failure' EXIT

START_TIME=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Running PR Smoke Test Evaluation for PR #${PR_ID} in Namespace: ${TARGET_NAMESPACE} ==="

# 2. Cluster Auth
STEP_START=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Authenticating to GKE Cluster ==="
gke_dns_endpoint_flag "$HOST_CLUSTER_NAME" "$REGION" "$PROJECT_ID"
# Unquoted on purpose: empty must contribute no argument. See gke_dns_endpoint.sh.
# shellcheck disable=SC2086
gcloud container clusters get-credentials "$HOST_CLUSTER_NAME" --region "$REGION" --project "$PROJECT_ID" --quiet \
  $GKE_DNS_ENDPOINT_FLAG
echo "✓ Cluster authentication finished in $((SECONDS - STEP_START))s"

# 3. Agent & Harness Configuration
# Configures devops-bench runner to target deployed platform-agent service
export BENCH_AGENT_TYPE="cli"
export AGENT_TARGET="kubeagents"
export BENCH_PARALLEL="false"
export AGENT_CLUSTER_CONTEXT="gke_${PROJECT_ID}_${REGION}_${HOST_CLUSTER_NAME}"
export AGENT_SERVICE_NAME="platform-agent"
export AGENT_NAMESPACE="${TARGET_NAMESPACE}"
export BENCH_TF_ROOT="./tf"

# For opentofu provider
export CLOUD_PROVIDER="gcp"
export TF_VAR_infra_provider="gcp"

# Per-run task-cluster name, derived from the Prow run identity. Within a
# project, two runs can never race on one cluster because they never share a
# name, and a "409 Already Exists" between runs is impossible by construction.
# The old fixed name ("test-cluster") was unsafe the moment two runs shared the
# project.
#
# This alone does NOT make raising the Prow job's max_concurrency safe: every
# run also installs cluster-wide singletons (CRDs, webhooks, ClusterRoles) on
# the shared platform-agent-host cluster. Real concurrency arrives with issue
# #637 (Boskos one-project-per-run leasing); do not raise max_concurrency
# before it. Unique names still matter under #637 -- a retried run in a
# freshly-leased project must not collide with what its predecessor left.
#
# GKE caps names at 40 chars matching [a-z]([-a-z0-9]*[a-z0-9])?. The name is
# lowercased and non-alphanumerics collapse to hyphens; locally it falls back
# to a stable "eval-pr0-<user>" so two laptops sharing a project do not
# collide, and the persistent tofu state under bench/tf makes reuse across
# local runs the intended behaviour.
#
# NEVER clamp an overlong name: the run discriminator (BUILD_ID) sits at the
# tail, so truncation keeps the shared prefix and drops exactly the part that
# differs -- two long BUILD_IDs with a common prefix would collapse to one
# name and resurrect the shared-name race. When the readable form does not
# fit, swap the tail for a hash of the full identity instead.
EVAL_RUN_IDENT="${PULL_NUMBER:-0}-${BUILD_ID:-${USER:-local}}"
EVAL_CLUSTER_NAME="eval-pr${EVAL_RUN_IDENT}"
EVAL_CLUSTER_NAME="$(printf '%s' "${EVAL_CLUSTER_NAME}" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9-' '-' | sed 's/-*$//')"
if [ "${#EVAL_CLUSTER_NAME}" -gt 40 ]; then
  EVAL_IDENT_HASH="$(printf '%s' "${EVAL_RUN_IDENT}" | { md5sum 2>/dev/null || md5 -q; } | tr -d ' -' | cut -c1-8)"
  # The PR component is bounded to 24 chars so the 8-char hash -- the only
  # part guaranteed to differ -- can never be squeezed out of the 40.
  EVAL_PR_PART="$(printf '%s' "${PULL_NUMBER:-0}" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | cut -c1-24 | sed 's/-*$//')"
  EVAL_CLUSTER_NAME="eval-pr${EVAL_PR_PART:-0}-${EVAL_IDENT_HASH}"
fi
export GKE_CLUSTER_NAME="${EVAL_CLUSTER_NAME}"
export CLUSTER_NAME="${EVAL_CLUSTER_NAME}"
export TF_VAR_cluster_name="${EVAL_CLUSTER_NAME}"
echo "Task cluster for this run: ${EVAL_CLUSTER_NAME}"
export GCP_LOCATION="us-west4-a" # set to different zone due to resource availability stockouts in us-central1

# Stamp the run onto every labelable GCP resource the stacks create, alongside
# the fixed managed-by label the cluster module applies. These say *which* run
# left an orphan behind; managed-by is what the sweep matches on. Both are set
# by Prow and empty when running locally, where the stacks fall back to "local".
export TF_VAR_prow_build_id="${BUILD_ID:-}"
export TF_VAR_prow_pull_number="${PULL_NUMBER:-}"

# 4. Token & Model Configuration
# Dynamically fetches API_SERVER_KEY from GKE secret and locks down Gemini 3.1
export PLATFORM_AGENT_TOKEN="$(kubectl get secret platform-agent-secrets -n "${TARGET_NAMESPACE}" -o jsonpath='{.data.API_SERVER_KEY}' | base64 --decode)"
export JUDGE_API_KEY="${GEMINI_API_KEY}"
export JUDGE_PROVIDER="google"
# The judge is pinned INDEPENDENTLY of the agent, and the invariant is:
# upgrading AGENT_MODEL must never move JUDGE_MODEL. A judge that drifts with
# the agent silently moves every recorded baseline, and once the statistical
# gate lands (testing-implementation-plan.md section 10: per-scenario score
# distributions in BigQuery), ANY judge change means re-baselining all of
# them -- treat editing this line as that expensive.
#
# The judge and agent VALUES are still equal today, which partly measures the
# judge grading itself. The split to a distinct judge model is blocked on one
# fact this repository cannot prove: that kube-agents-gemini-api-key serves a
# second model. The tree says it should -- the chart's default for the same
# GEMINI_API_KEY family is gemini-3.5-flash (charts/kube-agents/templates/
# litellm.yaml, docs/site .../inference-gateway.md) -- so the switch is one
# verified run away: confirm the key against the candidate model, then set
# JUDGE_MODEL_OVERRIDE in the Prow job env (or flip the default here) without
# touching the agent line.
export JUDGE_MODEL="${JUDGE_MODEL_OVERRIDE:-gemini-3.1-pro-preview}"
export AGENT_PROVIDER="google"
export AGENT_MODEL="${AGENT_MODEL_OVERRIDE:-gemini-3.1-pro-preview}"

# Unset NAMESPACE so devops-bench OpenTofu deployer does not pass -var namespace=... to stacks that don't declare it
unset NAMESPACE

# 5. Prerequisites Check
if ! command -v uv >/dev/null 2>&1; then
  echo "ERROR: 'uv' is not installed or not in PATH." >&2
  echo "The evaluation harness requires uv to run devops-bench." >&2
  echo "Please install uv (e.g. via 'curl -LsSf https://astral.sh/uv/install.sh | sh') or ensure the Prow runner image provides it." >&2
  exit 1
fi

# 6. Task Matrix Execution Loop
# Paths are relative to BENCH_DIR, which is where devops-bench runs. Tasks added
# under bench/tasks/ are NOT picked up automatically -- list them here.
BENCH_DIR="${SCRIPT_DIR}/../bench"
# agent-kanban-smoke is deployer: noop, so it adds seconds, not a cluster.
TASKS=(
  "./tasks/gpu-stress-test-diagnosis/task.yaml"
  "./tasks/agent-kanban-smoke/task.yaml"
  # The ten domain scenarios, registered here but commented out. Uncommenting
  # is the LAST step of activation, not the only one: bench/tasks/DRAFTS.md
  # carries an activation-blockers section and a per-scenario status column,
  # and every entry below is blocked on at least one of them today. The
  # task-registration lint counts a commented entry as registered.
  #
  # Summarised, so a reader here does not have to guess:
  #   A1  the six audit scenarios and rca-remediation-pr are NOT read-only --
  #       every fleet-audit stream mints a GitHub token and writes a ledger
  #       issue. ci-deploy.sh installs the PR's agent on every run but never
  #       sets platformAgent.integration.github.gitRepo, so SETTINGS.md
  #       renders `- **Git Repo:** None` (buildSettingsConfigMap substitutes
  #       the literal when the field is empty) and audit_report.py start has
  #       nothing to clone. Needs that value passed per leased project (the
  #       throwaway eval GitOps repos) and the minter scoped to it -- the
  #       token has exactly one source and no inherited GITHUB_TOKEN is
  #       honoured, so the value alone only moves the failure to the clone.
  #   A3  fleet-cost-idle-pool is date-gated by the SOP's own age rules
  #       (2026-08-28 for the pool, 2026-09-20 for the disks).
  #   A4  cleared in the code, open on one credential. The six audit
  #       scenarios' objectives no longer read the final message (which the
  #       SOPs keep to one line); they use ledger_issue_contains, which reads
  #       the GitHub ledger issue the run published and proves it is THIS
  #       run's by the generated-at stamp audit_report.py renders into it.
  #       That verifier needs BENCH_GITHUB_TOKEN (or GITHUB_TOKEN) with
  #       issues:read on the eval GitOps repos, which this script does not
  #       export and Prow does not supply -- provision it with A1's minter
  #       work. Until then those checks return status=error, which drops
  #       VerificationCoverage below the gate's 1.0 floor by design.
  #   A5  every resource_property safeguard in the corpus (six scenarios,
  #       cluster-agent-crashloop-debug included) reads the ambient
  #       kubeconfig, and the only get-credentials above is for
  #       platform-agent-host. The seeded namespaces are on seeded cluster A,
  #       so those catastrophic safeguards error and red the presubmit for
  #       every PR in the repo. Needs the runner to fetch the seeded
  #       clusters' credentials and each check to name one via the
  #       verifier's `kubeconfig` field.
  #
  # Two entries are not activatable by uncommenting at all:
  #   autoops-warning-event-triage -- its prompt is a meta-note and nothing
  #     applies its incident workload; it needs a scenario driver, which
  #     arrives with the AutoOps seam work.
  #   chat-routing-fleet-question (A2) -- AGENT_SERVICE_NAME above is a single
  #     global target, so every task here reaches the platform agent. This one
  #     needs the chat front door, and would fail its delegation objective on
  #     a correct system until the harness can target an agent per task.
  # "./tasks/chat-routing-fleet-question/task.yaml"
  # "./tasks/obtainability-planted-pdb/task.yaml"
  # "./tasks/stockout-pinned-pool/task.yaml"
  # "./tasks/fleet-cost-idle-pool/task.yaml"
  # "./tasks/compliance-rbac-overgrant/task.yaml"
  # "./tasks/upgrade-readiness-lagging-cluster/task.yaml"
  # "./tasks/consistency-drift-outlier/task.yaml"
  # "./tasks/rca-remediation-pr/task.yaml"
  # "./tasks/cluster-agent-crashloop-debug/task.yaml"
  # "./tasks/autoops-warning-event-triage/task.yaml"
)

# Floor for VerificationCorrectness on a repetition of a task that declares a
# verification_spec. 1.0 while every declared objective is meant to hold
# outright. Exported: bench-gate reads it, so it is a starting point to tune
# against observed movement on main rather than a constant in the code.
export DETERMINISTIC_CORRECTNESS_FLOOR="${DETERMINISTIC_CORRECTNESS_FLOOR:-1.0}"

# Repetitions per task. Three is what the collapse rule needs: a case reds the
# job alone only by failing ALL of them. Two-of-three would fire 1.45 times per
# pull request by chance at suite scale; three-of-three fires 0.03 times. The
# loop is serial (BENCH_PARALLEL=false), so this multiplies wall-clock by three
# -- how it scales past a handful of tasks is issue #902's lane, not this one.
EVAL_REPETITIONS="${EVAL_REPETITIONS:-3}"
if ! [ "${EVAL_REPETITIONS}" -ge 1 ] 2>/dev/null; then
  echo "ERROR: EVAL_REPETITIONS must be a positive integer, got '${EVAL_REPETITIONS}'." >&2
  echo "Zero repetitions would run nothing and report green -- refusing." >&2
  exit 1
fi

# How far a judged mean may fall below main's before rung 6 fires. 0.5 is
# arithmetic on the measured spread, not a preference: three repetitions of one
# unchanged task scored OutcomeValidity 0.9, 1.0 and 0.2 -- a standard deviation
# near 0.44, so the standard error of a three-repetition mean is about 0.25. One
# standard error would red roughly one unchanged pull request in six; two reds
# about one in fifty, the same order the collapse rule was sized to.
#
# So say plainly what this buys: at this width rung 6 catches a COLLAPSE in
# judged quality and cannot see drift, because at three repetitions drift and
# noise are the same picture. Tightening it needs more repetitions or a less
# variable metric, not a smaller number here.
export EVAL_JUDGED_MARGIN="${EVAL_JUDGED_MARGIN:-0.5}"

# The transition bridge. bench/baselines/ ships EMPTY, so no case is admitted
# and nothing can reach the collapse rung -- which would mean the presubmit
# blocks on nothing for as long as screening takes. Cases named here keep their
# old blocking behaviour meanwhile.
#
# It is a bridge and not a destination: a bootstrap-admitted case has no
# measured evidence, so it arms rung 4 but leaves rung 6 quiet and contributes
# nothing to main's side of the aggregate. Screening replaces it.
#
# agent-kanban-smoke is deliberately NOT named: it has redded pull requests it
# has nothing to do with, and un-arming it is half the point of the change.
export BOOTSTRAP_ADMITTED="${BOOTSTRAP_ADMITTED:-gpu-stress-test-diagnosis}"

# Where the evidence itself lives. Unset means bench/baselines/ in the
# checkout: hermetic, no credential, no network -- and no way for this job to
# commit what it measured, since it has no push credential. Set to
# gs://<bucket>/<prefix> and each batch becomes one immutable object under a
# roles/storage.objectCreator grant, which is what actually closes the loop on
# main. VERSIONS.json stays in git either way; --baseline-dir still finds it.
#
# It defaults to unset because the bucket does not exist yet. Turning this on
# is a one-line change here once it does, and until then the store fills only
# by hand from the --lines-out artefact below.
# See docs/designs/eval-baseline-storage.md.
export EVAL_BASELINE_STORE="${EVAL_BASELINE_STORE:-}"

# Where the per-case hand-offs land. `bench-gate case` writes one per task and
# `bench-gate suite` reads them back to decide the exit status; both files ride
# to Prow as artifacts, which is what makes a verdict reviewable after the job.
ARTIFACT_DIR="${ARTIFACTS:-/tmp/artifacts}"
mkdir -p "${ARTIFACT_DIR}"
CASE_RESULTS=()

for TASK in "${TASKS[@]}"; do
  TASK_NAME="$(basename "$(dirname "${TASK}")")"
  TASK_START=$SECONDS
  echo ">>> [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Running Task: ${TASK_NAME} (${TASK}) x${EVAL_REPETITIONS} <<<"

  # BENCH_NO_INFRA stays false for EVERY task, noop-deployer ones included.
  # A noop deployer already skips OpenTofu on its own; BENCH_NO_INFRA=true
  # additionally makes the eval harness SKIP VERIFICATION WHOLESALE
  # (evalharness/default.py, verification_status "skipped_no_infra"), which
  # silently un-gates any task whose checks read the transcript rather than a
  # cluster -- the kanban probe's tool_called check would never evaluate.
  #
  # The deployer itself is no longer read here. bench-gate parses the task
  # file with a real YAML parser (bench/kube_agents_bench/cases.py) and echoes
  # what it found; the two greps this replaced could not tell a real
  # `deployer:` from one inside a comment or a prompt block.
  export BENCH_NO_INFRA="false"
  echo "Executing with BENCH_NO_INFRA=${BENCH_NO_INFRA}"

  # One --result per repetition, positionally. A repetition that produced no
  # run directory contributes the literal MISSING, so the gate can tell "died
  # before writing anything" from "wrote an unusable record" -- a different
  # diagnosis with a different owner.
  RESULT_ARGS=()
  for REP in $(seq 1 "${EVAL_REPETITIONS}"); do
    echo "--- [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] ${TASK_NAME} repetition ${REP}/${EVAL_REPETITIONS}"
    # Snapshot existing result directories before running to prevent stale score leakage
    PRE_RUNS="$(ls -d "${BENCH_DIR}/results/run_"* 2>/dev/null | sort || true)"
    EVAL_LOG="/tmp/eval_${TASK_NAME}_rep${REP}.log"

    (cd "${BENCH_DIR}" && uv run devops-bench "${TASK}" --agent-type kubeagents 2>&1 | tee "${EVAL_LOG}") || true

    # Use set difference (comm -13) to isolate the brand new directory created strictly by THIS repetition.
    # If devops-bench crashed before or during execution without completing results.json, NEW_RUN_DIR will be empty.
    POST_RUNS="$(ls -d "${BENCH_DIR}/results/run_"* 2>/dev/null | sort || true)"
    NEW_RUN_DIR="$(comm -13 <(echo "${PRE_RUNS}") <(echo "${POST_RUNS}") | head -n 1)"

    # The harness log is kept for every repetition, green ones included: a
    # green record is the raw material for the baseline store, and its log is
    # how anyone reconstructs what produced it.
    cp "${EVAL_LOG}" "${ARTIFACT_DIR}/eval_${TASK_NAME}_rep${REP}.log" 2>/dev/null || true

    if [ -n "${NEW_RUN_DIR}" ]; then
      RESULT_ARGS+=(--result "${NEW_RUN_DIR}")
      cp "${NEW_RUN_DIR}/results.json" "results_${TASK_NAME}_rep${REP}.json" 2>/dev/null || true
    else
      RESULT_ARGS+=(--result MISSING)
    fi
  done

  # The verdict. bench-gate exits 0 for ANY verdict it could reach, including a
  # blocking one -- under `set -e` a non-zero here would abort the loop and
  # silently drop every remaining task. It exits 2 only when it could not grade
  # at all (an unreadable task file, a broken VERSIONS.json), which is a
  # different failure and must stop the job.
  CASE_JSON="${ARTIFACT_DIR}/case-${TASK_NAME}.json"
  (cd "${BENCH_DIR}" && uv run bench-gate case \
    --task "${TASK}" \
    "${RESULT_ARGS[@]}" \
    --json-out "${CASE_JSON}")
  CASE_RESULTS+=(--case-result "${CASE_JSON}")

  echo "Task ${TASK_NAME} finished in $((SECONDS - TASK_START))s"
done

# Baseline collection, and it runs BEFORE the verdict on purpose: the suite
# step exits 1 on a red, which under `set -e` would skip everything after it.
# A red run on main is precisely the evidence that de-admits a case that has
# stopped working, so it is the one run that must not go unrecorded.
#
# Only a postsubmit appends. `bench-gate record` refuses a second time if
# PULL_NUMBER is set, because a guard that lives only in shell is one careless
# edit away from letting a pull request move the baseline it is judged against.
#
# With EVAL_BASELINE_STORE pointing at a bucket the append lands and the loop
# closes. Unset, the store is the git checkout and this job has no push
# credential, so the append dies with the workspace; --lines-out is what
# survives, as a Prow artefact somebody lands by hand in the meantime.
if [ "${JOB_TYPE:-}" = "postsubmit" ] && [ -z "${PULL_NUMBER:-}" ]; then
  echo ">>> [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Recording baseline evidence from main <<<"
  # Never fatal. Bookkeeping must not be the reason a merge to main reds.
  (cd "${BENCH_DIR}" && uv run bench-gate record \
    "${CASE_RESULTS[@]}" \
    --lines-out "${ARTIFACT_DIR}/baseline-append.jsonl") || \
    echo "WARNING: recording baseline evidence failed; the verdict below is unaffected."
else
  echo "Not a postsubmit run: the baseline store is read, never written."
fi

# The suite roll-up: blocking cases, the admitted-case aggregate, and the
# all-infrastructure check. Exit 0 green, 1 red. --baseline-rate is not passed:
# the rate is computed from the store, per admitted case at its own version
# key. While the store holds nothing the aggregate stays advisory and the
# markdown says so, rather than implying a comparison that did not happen.
TOTAL_DURATION=$((SECONDS - START_TIME))
if (cd "${BENCH_DIR}" && uv run bench-gate suite \
  "${CASE_RESULTS[@]}" \
  --markdown-out "${ARTIFACT_DIR}/eval-verdict.md" \
  --json-out "${ARTIFACT_DIR}/eval-verdict.json"); then
  echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] PR Smoke Test Evaluation Succeeded (Total Duration: ${TOTAL_DURATION}s) ==="
else
  echo "❌ [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] PR Smoke Test Evaluation Failed -- see ${ARTIFACT_DIR}/eval-verdict.md (Total Duration: ${TOTAL_DURATION}s)"
  exit 1
fi
