#!/bin/sh
# Regenerate every generated source: tools/gen_simple.py (pruned against one
# check), then tools/lift_all.sh. Leaves build/check.log and progress/report.json.
#   sh tools/build_all.sh [python]
PY=${1:-python3}
$PY tools/gen_simple.py
$PY tools/check.py -v > build/check.log
$PY tools/gen_simple.py --prune build/check.log
sh tools/lift_all.sh "$PY"
