#!/bin/bash
# Score one matrix run directory into results/<name>.{md,json,csv}.
# Usage: make_results.sh <run-root> <name>   e.g. make_results.sh ~/dev/csc-runs/gemini gemini-3.1-pro
set -euo pipefail
readonly RUN_ROOT=${1:?run root}
readonly NAME=${2:?result name}
readonly HERE=$(cd "$(dirname "$0")" && pwd)
readonly OUT="$HERE/results"
readonly CELLS="stock-shipped fulldesc-shipped scoped-all-shipped stock-grown fulldesc-grown scoped-all-grown"
mkdir -p "$OUT"
dirs=()
for c in $CELLS; do [ -d "$RUN_ROOT/$c" ] && dirs+=("$RUN_ROOT/$c"); done
python3 "$HERE/analyze.py" --runs "${dirs[@]}" --scenarios "$HERE/scenarios.json" --csv "$OUT/$NAME.csv" > "$OUT/$NAME.md"
python3 "$HERE/analyze.py" --runs "${dirs[@]}" --scenarios "$HERE/scenarios.json" --json > "$OUT/$NAME.json"
{
  echo; echo "## Comparisons"; echo
  for rung in shipped grown; do
    for pair in "stock fulldesc" "stock scoped-all" "fulldesc scoped-all"; do
      set -- $pair
      [ -d "$RUN_ROOT/$1-$rung" ] && [ -d "$RUN_ROOT/$2-$rung" ] || continue
      echo "### $1-$rung vs $2-$rung"; echo
      python3 "$HERE/analyze.py" --runs "$RUN_ROOT/$1-$rung" "$RUN_ROOT/$2-$rung" --scenarios "$HERE/scenarios.json" --compare "$1-$rung" "$2-$rung" | head -3 | sed 's/^/- /'
      echo
    done
  done
} >> "$OUT/$NAME.md"
echo "wrote $OUT/$NAME.{md,json,csv}"
