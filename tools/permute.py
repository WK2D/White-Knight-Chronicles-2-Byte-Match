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


def reorders(code):
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


def if_blocks(lines):
    """[(if line, else line or None, closing line)] for every if statement."""
    out = []
    for i, line in enumerate(lines):
        m = re.match(r'^(\s*)if \((.*)\) \{$', line)
        if not m:
            continue
        pad, other = m.group(1), None
        for j in range(i + 1, len(lines)):
            if lines[j] == pad + '} else {':
                other = j
            elif lines[j] == pad + '}':
                out.append((i, other, j))
                break
            elif lines[j].startswith(pad + '}'):
                break                                    # else if: left alone
    return out


def invert(lines, block):
    """if (c) { A } else { B }  ->  if (!(c)) { B } else { A }."""
    i, other, end = block
    m = re.match(r'^(\s*)if \((.*)\) \{$', lines[i])
    pad, cond = m.group(1), m.group(2)
    then = lines[i + 1:other if other is not None else end]
    els = lines[other + 1:end] if other is not None else []
    new = [f'{pad}if (!({cond})) {{'] + els
    if then:
        new += [f'{pad}}} else {{'] + then
    return lines[:i] + new + [f'{pad}}}'] + lines[end + 1:]


def flips(code):
    """The code with each if/else inverted, one at a time and all together.
    Which branch GCC lays out first follows the source."""
    lines = code.split('\n')
    blocks = if_blocks(lines)
    out = []
    for block in blocks:
        out.append('\n'.join(invert(lines, block)))
    if len(blocks) > 1:
        new = lines
        for k in range(len(blocks)):                     # inverting can add a line: find the k-th if again
            new = invert(new, if_blocks(new)[k])
        out.append('\n'.join(new))
    return out


SWAP = {' == ': ' == ', ' != ': ' != ', ' < ': ' > ', ' > ': ' < ', ' <= ': ' >= ', ' >= ': ' <= '}


def split_comparison(cond):
    """(left, operator, right) of a comparison at parenthesis depth 0, or None."""
    depth = 0
    for i, ch in enumerate(cond):
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        elif depth == 0:
            for op in SWAP:
                if cond.startswith(op, i):
                    return cond[:i], op, cond[i + len(op):]
    return None


def swaps(code):
    """The code with each if's comparison written the other way round
    (a < b as b > a), one at a time and all together: GCC keeps the
    operand order in the compare instruction."""
    lines = code.split('\n')
    spots = []
    for i, line in enumerate(lines):
        m = re.match(r'^(\s*)if \((.*)\) \{$', line)
        if m and split_comparison(m.group(2)):
            spots.append(i)
    def swapped(ls, i):
        m = re.match(r'^(\s*)if \((.*)\) \{$', ls[i])
        left, op, right = split_comparison(m.group(2))
        return ls[:i] + [f'{m.group(1)}if ({right}{SWAP[op]}{left}) {{'] + ls[i + 1:]
    out = ['\n'.join(swapped(lines, i)) for i in spots]
    if len(spots) > 1:
        new = lines
        for i in spots:
            new = swapped(new, i)
        out.append('\n'.join(new))
    return out


def voided(code):
    """The function returning nothing: the lifter takes whatever r3 holds at
    a return for a result, but the source may have been void (r3 then held
    a leftover, such as the last call's argument)."""
    lines = code.split('\n')
    m = re.match(r'^(\S.*?) (fn_[0-9a-f]{8}\(.*)$', lines[0])
    if not m or m.group(1) == 'void':
        return None
    body = [re.sub(r'^(\s*)return .+;$', r'\1return;', l) for l in lines[1:]]
    return '\n'.join([f'void {m.group(2)}'] + body)


def variants(code):
    """Other forms of a lifted function: statement orders, inverted ifs,
    swapped comparisons, a void version, and a few statement orders of each."""
    out = reorders(code)
    void = voided(code)
    for other in flips(code) + swaps(code) + ([void] if void else []):
        out.append(other)
        out += reorders(other)[:8]
    if void:                                     # the void version first among the extras
        out.insert(0, void)
    return list(dict.fromkeys(out))[:MAX_VARIANTS * 3]


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
