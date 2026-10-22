"""Find statement orders that match: for lifted functions that compile but
do not match, try other orders of each run of consecutive assignments,
compile every variant and keep the first one that matches. GCC schedules
stores by its own rules, so the source order that reproduces the original
is often not the order the instructions appear in.

    python tools/permute.py build/check_unpruned.log   # writes src/perm/

The log must come from a check of unpruned lifter output (every lifted
function compiled), e.g. "rm build/lift_pruned.txt; lift.py; check.py -v".
Matched functions go to src/perm/<unit>.cpp; tools/lift.py then leaves them
alone. Existing src/perm files are kept and extended.
"""
import bisect
import itertools
import re
import struct
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check import compare, compile_all, reloc_masks
from common import BUILD, ROOT, Object, Original, load_functions, load_units
from lift import arity_finder, file_text, lift, short_name

PERM = ROOT / 'src' / 'perm'
WORK = BUILD / 'perm'
MAX_WORDS = 48          # functions up to this many instructions
MAX_VARIANTS = 48       # orders tried per function
ASSIGN = re.compile(r'^(\s+)(?!return\b)[^\s].* = .*;$')


def runs(lines):
    """[(start, end)] of runs of two or more consecutive assignments with the
    same indentation. Calls stay where they are."""
    out, i = [], 0
    while i < len(lines):
        m = ASSIGN.match(lines[i])
        if not m or re.search(r'\b(fn|stub)_[0-9a-f]{8}\(|->vf\d', lines[i]):
            i += 1
            continue
        j = i + 1
        while j < len(lines) and ASSIGN.match(lines[j]) and ASSIGN.match(lines[j]).group(1) == m.group(1) \
                and not re.search(r'\b(fn|stub)_[0-9a-f]{8}\(|->vf\d', lines[j]):
            j += 1
        if j - i >= 2:
            out.append((i, j))
        i = j
    return out


def orders(n):
    """Orders of a run of n statements to try: all of them for short runs,
    else reversal, adjacent swaps and rotations."""
    if n <= 4:
        return list(itertools.permutations(range(n)))
    base = list(range(n))
    out = [tuple(base), tuple(reversed(base))]
    for k in range(n - 1):
        o = base[:]
        o[k], o[k + 1] = o[k + 1], o[k]
        out.append(tuple(o))
    for k in range(1, n):
        out.append(tuple(base[k:] + base[:k]))
    return list(dict.fromkeys(out))


def variants(code):
    """Other statement orders of a lifted function's code."""
    lines = code.split('\n')
    spans = runs(lines)
    if not spans:
        return []
    choices = [orders(e - s) for s, e in spans]
    out = []
    for combo in itertools.product(*choices):
        if all(o == tuple(range(len(o))) for o in combo):
            continue                                     # the order already tried
        new = lines[:]
        for (s, e), o in zip(spans, combo):
            new[s:e] = [lines[s + k] for k in o]
        out.append('\n'.join(new))
        if len(out) >= MAX_VARIANTS:
            break
    return out


def main():
    log = Path(sys.argv[1]).read_text(encoding='utf-8', errors='replace')
    misses = {int(m.group(1), 16): float(m.group(2))
              for m in re.finditer(r'^([0-9a-f]{8}) +([\d.]+)% (?!MATCH)', log, re.M)}
    orig, functions = Original(), load_functions()
    arity = arity_finder(orig, functions)
    units = load_units()
    starts = [u[1] for u in units]
    done = set()
    for f in PERM.glob('*.cpp') if PERM.exists() else ():
        done |= {int(m, 16) for m in re.findall(r'^\S.*?\bfn_([0-9a-f]{8})\([^\n]*[^;]$', f.read_text(), re.M)}
    # Variants of every candidate, one file each.
    WORK.mkdir(parents=True, exist_ok=True)
    for old in WORK.glob('*'):
        old.unlink()
    jobs, info = [], {}
    for address, fuzzy in sorted(misses.items()):
        size = functions.get(address)
        if not size or size // 4 > MAX_WORDS or address in done:
            continue
        result = lift(address, struct.unpack(f'>{size // 4}I', orig.read(address, size)), arity)
        if result is None:
            continue
        decls, code, calls = result
        for k, variant in enumerate(variants(code)):
            path = WORK / f'{address:08x}_{k}.cpp'
            path.write_text(file_text([], calls, [(address, decls, variant)]), encoding='utf-8', newline='\n')
            jobs.append(path)
            info[path] = (address, decls, variant, calls)
    print(f'{len(jobs)} variants of {len({i[0] for i in info.values()})} functions')
    if not jobs:
        return
    compiled = compile_all(jobs)
    found = {}
    for path in jobs:
        obj_path = compiled[path]
        if isinstance(obj_path, str):
            continue
        address = info[path][0]
        if address in found:
            continue
        obj = Object(obj_path)
        for name, section, start, size in obj.functions():
            if name != f'.fn_{address:08x}':
                continue
            ours = obj.section_bytes(section)[start:start + size]
            fuzzy, exact = compare(ours, orig.read(address, functions[address]),
                                   reloc_masks(obj, section, start, size))
            if exact:
                found[address] = info[path]
    print(f'{len(found)} functions match in another order')
    # Group by unit; a file holds functions whose callee declarations agree
    # and that do not call each other.
    PERM.mkdir(parents=True, exist_ok=True)
    groups = defaultdict(list)
    for address, (_, decls, code, calls) in sorted(found.items()):
        i = bisect.bisect_right(starts, address) - 1
        unit = units[i][0] if i >= 0 and address < units[i][2] else 'unknown'
        groups[unit].append((address, decls, code, calls))
    for unit, items in groups.items():
        files = []                                       # [(callees, keep)]
        for address, decls, code, calls in items:
            for callees, keep in files:
                if all(callees.get(t, c) == c for t, c in calls.items()) \
                        and not any(t in {a for a, *_ in keep} for t in calls) and address not in callees:
                    callees.update(calls)
                    keep.append((address, decls, code))
                    break
            else:
                files.append((dict(calls), [(address, decls, code)]))
        for n, (callees, keep) in enumerate(files):
            path = PERM / f'{short_name(unit)}{"" if n == 0 else f"_{n}"}.cpp'
            if path.exists():                            # extend an earlier run's file
                path = PERM / f'{short_name(unit)}_{n}_{keep[0][0]:08x}.cpp'
            header = ['// Generated by tools/permute.py: lifted functions of unit ' + unit + ' whose',
                      '// statements needed another order to compile to the original bytes.']
            path.write_text(file_text(header, callees, keep), encoding='utf-8', newline='\n')


if __name__ == '__main__':
    main()
