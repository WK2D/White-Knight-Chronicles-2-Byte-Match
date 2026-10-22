"""Infer the game's translation units from the stripped EBOOT and write
config/units.txt.

GCC lays out each source file's functions together, and each file's TOC
entries (the table of addresses its code loads through r2) together, both in
link order. So:

1. Classes label functions. A function listed in a class's vtable (found
   through RTTI, see classes.py) or one that loads that vtable's address
   (a constructor or destructor) belongs to that class's file.
2. Between two labelled runs, the boundary goes where the TOC entries the
   functions load stop overlapping the earlier run's entries.
3. Unlabelled stretches split the same way: a new unit starts when a
   function's TOC entries all lie past every entry the current unit used.
   Units under MIN_UNIT bytes join their neighbour.

The result is an estimate. A unit is renamed to its real source path once
that is known, and units can be edited by hand.

    python tools/make_units.py
"""
import json
import struct
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import BUILD, ROOT, Original, load_functions

OPD = (0x12d9b90, 0x72388)     # function descriptors: {code, toc}
MIN_UNIT = 0x400


def toc_loads(code, toc):
    """TOC entry addresses a function loads from (D-form loads off r2)."""
    out = set()
    for i in range(0, len(code), 4):
        w = struct.unpack_from('>I', code, i)[0]
        op, ra = w >> 26, (w >> 16) & 31
        if ra != 2 or op not in (32, 34, 40, 42, 48, 50, 58):   # lwz lbz lhz lha lfs lfd ld
            continue
        d = w & 0xFFFF
        if op == 58:
            d &= 0xFFFC
        out.add(toc + (d - 0x10000 if d & 0x8000 else d))
    return out


def main():
    orig = Original()
    functions = load_functions()
    order = sorted(functions)
    if not (BUILD / 'classes.json').exists():
        raise SystemExit('run tools/classes.py first')
    classes = json.loads((BUILD / 'classes.json').read_text(encoding='utf-8'))
    # TOC base of each function: from its descriptor, else the previous function's.
    opd = orig.read(*OPD)
    toc_of = {}
    for i in range(0, len(opd) - 7, 8):
        code, toc = struct.unpack_from('>II', opd, i)
        if code in functions:
            toc_of[code] = toc
    toc = next(iter(toc_of.values()))
    loads = {}
    for a in order:
        toc = toc_of.get(a, toc)
        loads[a] = toc_loads(orig.read(a, functions[a]), toc)
    # Vtable address -> class, for constructors and destructors.
    candidates = {a: set() for a in order}
    for name, methods in classes.items():
        for m in methods:
            candidates[m].add(name)
    vptr_class = {}
    for name, info in json.loads((BUILD / 'vtables.json').read_text(encoding='utf-8')).items():
        for vptr in info:
            vptr_class[vptr] = name
    size_of = {k: len(v) for k, v in classes.items()}
    for a in order:
        refs = set()
        for entry in loads[a]:
            try:
                value = struct.unpack('>I', orig.read(entry, 4))[0]
            except ValueError:
                continue
            if value in vptr_class:
                refs.add(vptr_class[value])
        if refs and not candidates[a]:
            # A constructor or destructor stores every vtable pointer of its
            # class's bases too; the class itself has the longest vtable.
            candidates[a] = {max(refs, key=lambda k: size_of.get(k, 0))}
    # One owner per virtual method: the class its neighbours agree on, else
    # the one with the smallest vtable (a base class's methods also appear in
    # every derived vtable).
    label = {}
    for i, a in enumerate(order):
        c = candidates[a]
        if not c:
            continue
        if len(c) == 1:
            label[a] = next(iter(c))
            continue
        near = Counter()
        for b in order[max(0, i - 8):i + 9]:
            if b != a and len(candidates[b]) == 1:
                near.update(candidates[b])
        best = [k for k in c if near[k]]
        label[a] = max(best, key=lambda k: near[k]) if best else min(c, key=lambda k: size_of.get(k, 0))
    # Segment.
    units = []          # [start, name or None, toc lo, toc hi]
    def start_unit(a, name):
        units.append([a, name, None, None])
    def note_toc(a):
        u = units[-1]
        if loads[a]:
            lo, hi = min(loads[a]), max(loads[a])
            u[2] = lo if u[2] is None else min(u[2], lo)
            u[3] = hi if u[3] is None else max(u[3], hi)
    start_unit(order[0], label.get(order[0]))
    for a in order[1:]:
        cur = units[-1]
        name = label.get(a)
        past = loads[a] and cur[3] is not None and min(loads[a]) > cur[3]
        if name is not None and cur[1] is None:
            cur[1] = name             # an unlabelled run takes the class that follows
        elif name is not None and name != cur[1]:
            start_unit(a, name)
        elif name is None and past:
            start_unit(a, None)       # TOC entries all past this unit's: a new file
        note_toc(a)
    # Merge tiny units into their predecessor.
    merged = []
    ends = [u[0] for u in units[1:]] + [order[-1] + functions[order[-1]]]
    for u, end in zip(units, ends):
        if merged and end - u[0] < MIN_UNIT and (u[1] is None or u[1] == merged[-1][1]):
            merged[-1][2] = end
            continue
        merged.append([u[0], u[1], end])
    # Names.
    seen = Counter()
    lines = ['# name start end category',
             '# Inferred by tools/make_units.py: class names from RTTI, auto_* where no class',
             '# is known. Rename a unit to its source path once that is known.']
    for start, name, end in merged:
        if name:
            base = name.replace('::', '_')
            seen[base] += 1
            unit = base if seen[base] == 1 else f'{base}_{seen[base]}'
            category = 'havok' if name.startswith('hk') else 'game'
        else:
            unit, category = f'auto_{start:08x}', 'auto'
        lines.append(f'{unit} {start:08x} {end:08x} {category}')
    (ROOT / 'config' / 'units.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8', newline='\n')
    sizes = sorted(e - s for s, _, e in merged)
    named = sum(1 for _, n, _ in merged if n)
    print(f'{len(merged)} units ({named} named after classes); size median {sizes[len(sizes) // 2]:#x}, '
          f'largest {sizes[-1]:#x}')


if __name__ == '__main__':
    main()
