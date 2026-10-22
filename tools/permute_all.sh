#!/bin/sh
# Search other source forms for every lifted near-miss, then converge the
# lifter output: unpruned lift and check, tools/permute.py, tools/lift_all.sh.
#   sh tools/permute_all.sh [python]
PY=${1:-python3}
rm -f build/lift_pruned.txt
$PY tools/lift.py > /dev/null && $PY tools/check.py -v > build/check_unpruned.log
$PY tools/permute.py build/check_unpruned.log
sh tools/lift_all.sh "$PY"
