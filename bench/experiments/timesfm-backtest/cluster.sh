#!/usr/bin/env bash
# up: cluster, bucket, corpus upload, Workload Identity grant, forecaster.
# run RUN: start the backtest Job and wait for it. fetch RUN: copy its results into results/.
# down: delete the cluster and the bucket.
set -euo pipefail

PROJECT="${PROJECT:-agentic-harness-demo}"
ZONE="${ZONE:-us-central1-a}"
CLUSTER="${CLUSTER:-timesfm-exp}"
BUCKET="${BUCKET:-${PROJECT}-timesfm-exp}"
MACHINE="n2-standard-16"
MAX_NODES=4
JOB_TIMEOUT="12h"
HERE="$(cd "$(dirname "$0")" && pwd)"

case "${1:-}" in
up)
  gcloud container clusters create "${CLUSTER}" --project "${PROJECT}" --zone "${ZONE}" \
    --machine-type "${MACHINE}" --num-nodes 1 --enable-autoscaling --min-nodes 1 \
    --max-nodes "${MAX_NODES}" --workload-pool="${PROJECT}.svc.id.goog" \
    --release-channel regular --labels purpose=timesfm-backtest
  gcloud storage buckets create "gs://${BUCKET}" --project "${PROJECT}" \
    --location us-central1 --uniform-bucket-level-access
  gcloud storage cp "${HERE}/data/series.jsonl.gz" "gs://${BUCKET}/series.jsonl.gz"
  number="$(gcloud projects describe "${PROJECT}" --format='value(projectNumber)')"
  gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" --role roles/storage.objectAdmin \
    --member "principal://iam.googleapis.com/projects/${number}/locations/global/workloadIdentityPools/${PROJECT}.svc.id.goog/subject/ns/timesfm/sa/backtest"
  gcloud container clusters get-credentials "${CLUSTER}" --project "${PROJECT}" --zone "${ZONE}"
  kubectl create namespace timesfm --dry-run=client -o yaml | kubectl apply -f -
  kubectl apply -f "${HERE}/k8s/forecaster.yaml"
  kubectl -n timesfm rollout status deploy/forecaster --timeout=30m
  ;;
run)
  RUN="${2:?run name}" envsubst '${RUN}' < "${HERE}/k8s/backtest-job.yaml" | kubectl apply -f -
  kubectl -n timesfm wait --for=condition=complete "job/backtest-${2}" --timeout="${JOB_TIMEOUT}"
  ;;
fetch)
  gcloud storage cp "gs://${BUCKET}/results/${2:?run name}/*" "${HERE}/results/"
  ;;
down)
  gcloud container clusters delete "${CLUSTER}" --project "${PROJECT}" --zone "${ZONE}" --quiet
  gcloud storage rm -r "gs://${BUCKET}"
  ;;
*)
  echo "usage: $0 up | run RUN | fetch RUN | down" >&2
  exit 1
  ;;
esac
