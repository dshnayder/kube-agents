#!/usr/bin/env bash
# Walk the Honcho ladder: seed, drain, score, one rung at a time.
#
# The rungs are nested, so each seed writes only the delta — the resume check
# in seed_honcho.py is what makes re-running with a larger N cheap. Drain
# between seed and score is not optional: Honcho derives asynchronously, so
# scoring early measures deriver latency rather than retrieval quality.
#
# Hindsight is not re-run. Its numbers at these same rungs are already in
# results/hindsight-r*.json from the earlier ladder, against the same corpus
# (generator seed 20260731, so it is byte-identical).
set -u

API="${API:-http://127.0.0.1:18800}"
WS="${WS:-meridian}"
CORPUS="${CORPUS:-/tmp/scaletest/v2/corpus}"
QUERIES="${QUERIES:-/tmp/scaletest/v2/queries.json}"
OUT="${OUT:-/tmp/scaletest/v2}"
HERE="$(cd "$(dirname "$0")" && pwd)"

for rung in "$@"; do
  echo "=============== RUNG ${rung} ==============="

  echo "--- seed"
  python3 "${HERE}/seed_honcho.py" --api-url "${API}" --workspace "${WS}" \
    --corpus "${CORPUS}" --rung "${rung}" || echo "seed reported failures; continuing"

  echo "--- drain"
  python3 "${HERE}/drain_honcho.py" --api-url "${API}" --workspace "${WS}" \
    --out "${OUT}/honcho-drain-r${rung}.json"

  echo "--- score"
  python3 "${HERE}/eval_fleet.py" --provider honcho --rung "${rung}" \
    --api-url "${API}" --workspace "${WS}" \
    --queries "${QUERIES}" --corpus "${CORPUS}" \
    --allow-leaks --out "${OUT}/honcho-r${rung}.json"
done

echo "=============== LADDER COMPLETE ==============="
