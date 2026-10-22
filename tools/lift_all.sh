#!/bin/sh
# Lift, compile and compare, then drop what does not match and repeat until
# nothing more is dropped. Leaves build/check.log and progress/report.json.
#   sh tools/lift_all.sh [python]
PY=${1:-python3}
rm -f build/lift_pruned.txt
$PY tools/lift.py > /dev/null && $PY tools/check.py -v > build/check.log
grep "functions match" build/check.log
prev=-1
for round in $(seq 1 20); do
  $PY tools/lift.py --prune build/check.log > /dev/null && $PY tools/check.py -v > build/check.log
  grep "functions match" build/check.log
  n=$(wc -l < build/lift_pruned.txt)
  failed=$(grep -c "compile failed" build/check.log)
  [ "$n" = "$prev" ] && [ "$failed" = 0 ] && break
  prev=$n
done
