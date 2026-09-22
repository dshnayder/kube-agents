#!/bin/bash
# Score one matrix run directory into results/<name>.{md,json,csv}.
# Usage: make_results.sh <run-root> <name>   e.g. make_results.sh ~/dev/csc-runs/gemini gemini-3.1-pro
set -euo pipefail
readonly RUN_ROOT=${1:?run root}
readonly NAME=${2:?result name}
HERE=$(cd "$(dirname "$0")" && pwd); readonly HERE
readonly OUT="$HERE/results"
readonly CELLS="stock-shipped fulldesc-shipped scoped-all-shipped stock-grown fulldesc-grown scoped-all-grown"
readonly COMPARE_LINES=3   # the three first-skill lines --compare prints
mkdir -p "$OUT"
dirs=()
for c in $CELLS; do [ -d "$RUN_ROOT/$c" ] && dirs+=("$RUN_ROOT/$c"); done
python3 "$HERE/analyze.py" --runs "${dirs[@]}" --scenarios "$HERE/scenarios.json" --csv "$OUT/$NAME.csv" > "$OUT/$NAME.md"
python3 "$HERE/analyze.py" --runs "${dirs[@]}" --scenarios "$HERE/scenarios.json" --json > "$OUT/$NAME.json"
{
  echo; echo "## Comparisons"; echo
  for rung in shipped grown; do
    for pair in "stock:fulldesc" "stock:scoped-all" "fulldesc:scoped-all"; do
      base=${pair%%:*}; treat=${pair##*:}
      [ -d "$RUN_ROOT/$base-$rung" ] && [ -d "$RUN_ROOT/$treat-$rung" ] || continue
      echo "### $base-$rung vs $treat-$rung"; echo
      python3 "$HERE/analyze.py" --runs "$RUN_ROOT/$base-$rung" "$RUN_ROOT/$treat-$rung" --scenarios "$HERE/scenarios.json" --compare "$base-$rung" "$treat-$rung" | head -"$COMPARE_LINES" | sed 's/^/- /'
      echo
    done
  done
} >> "$OUT/$NAME.md"
echo "wrote $OUT/$NAME.{md,json,csv}"
