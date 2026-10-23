#!/bin/sh
# Search other source forms for every lifted near-miss, then converge the
# lifter output: unpruned lift; drop only what does not compile (repeat), so
# every near-miss is checked; tools/permute.py; tools/classgen.py
# (constructors as C++ classes); tools/lift_all.sh.
#   sh tools/permute_all.sh [python]
PY=${1:-python3}
rm -f build/lift_pruned.txt
$PY tools/lift.py > /dev/null && $PY tools/check.py -v > build/check_unpruned.log
for round in 1 2 3 4 5 6; do
  [ "$(grep -c 'compile failed' build/check_unpruned.log)" = 0 ] && break
  $PY tools/lift.py --prune-errors build/check_unpruned.log > /dev/null && $PY tools/check.py -v > build/check_unpruned.log
done
$PY tools/permute.py build/check_unpruned.log
[ -f build/vtables.json ] || $PY tools/classes.py
$PY tools/classgen.py
sh tools/lift_all.sh "$PY"
