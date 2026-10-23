"""Constructors as C++: lifted functions that store a vtable pointer into
their first argument are written again as the constructor of a generated
class (the store becomes the class's own vtable setup), compiled, and kept
where they match. GCC schedules a real constructor's vtable store with the
member stores; written as a plain store it does not.

    python tools/classgen.py        # writes src/cls/ and config/symbols_cls.txt

Needs build/vtables.json (tools/classes.py) and a current
progress/report.json (only unmatched functions are tried).
"""
import bisect
import json
import re
import struct
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lift
from check import compare, compile_all, reloc_masks
from common import BUILD, ROOT, Object, Original, load_functions, load_units
from lift import OPD, arity_finder, file_text, short_name
from permute import reorders, runs

VARIANTS = 24       # bodies tried per constructor

CLS = ROOT / 'src' / 'cls'
WORK = BUILD / 'cls'


def toc_bases(orig):
    """{function: TOC base}: the base most of its descriptors give."""
    opd = orig.read(*OPD)
    tocs = defaultdict(Counter)
    for i in range(0, len(opd) - 7, 8):
        code, toc = struct.unpack_from('>II', opd, i)
        tocs[code][toc] += 1
    return {code: c.most_common(1)[0][0] for code, c in tocs.items()}


def constructor(address, decls, code, vtable_globals, calls):
    """(decls, code, class name, calls) of the function as a constructor, or
    None. A call on this before the vtable store is the base class's
    constructor."""
    lines = code.split('\n')
    m = re.match(rf'^void fn_{address:08x}\((unk_{address:08x}_a0)\* a0(.*)\) \{{$', lines[0])
    if not m:
        return None
    cls, rest = m.group(1), m.group(2)
    store = [i for i, l in enumerate(lines)
             if re.fullmatch(r'    a0->f_0 = \(void\*\)\((\w+)\);', l)
             and re.fullmatch(r'    a0->f_0 = \(void\*\)\((\w+)\);', l).group(1) in vtable_globals]
    if len(store) != 1 or re.search(r'\ba0->f_0\b', '\n'.join(lines[:store[0]] + lines[store[0] + 1:])):
        return None
    before = [i for i in range(1, store[0]) if '=' in lines[i] or '(' in lines[i]]   # statements, not declarations
    base = None
    if before:
        b = re.fullmatch(r'    (stub|fn)_([0-9a-f]{8})\(\(void\*\)\(a0\)\);', lines[before[0]])
        if len(before) != 1 or not b:
            return None
        base = (int(b.group(2), 16), b.group(1) == 'stub')
    skip = {store[0]} | set(before)
    body = [re.sub(r'\ba0\b', 'this', l) for i, l in enumerate(lines[1:-1], 1) if i not in skip]
    params = rest[2:] if rest.startswith(', ') else ''
    types = ', '.join(p.rsplit(' ', 1)[0] for p in params.split(', ')) if params else 'void'
    out = []
    for d in decls:
        if not d.startswith(f'struct {cls} {{'):
            if not re.fullmatch(r'extern char (\w+)\[\];', d) or \
                    re.fullmatch(r'extern char (\w+)\[\];', d).group(1) not in vtable_globals:
                out.append(d)
            continue
        fields = [l for l in d.split('\n')[1:-1] if not re.fullmatch(r'    \S.* f_0;', l)]
        if fields and fields[0].startswith('    char pad_0['):
            return None                                  # something else sits where the vtable pointer goes
        head = f'struct {cls} {{'
        if base is not None:
            # The base class: its constructor stands for the call. In the same
            # file in the original (a local call), so defined here too.
            bname = f'{cls}_base'
            out.append(f'struct {bname} {{ {bname}(); }};')
            if base[1]:
                out.append(f'__attribute__((noinline)) {bname}::{bname}() {{ __asm__ volatile (""); }}')
            head = f'struct {cls} : {bname} {{'
        out.append('\n'.join([head, f'    {cls}({types});', '    virtual void vf0();'] + fields + ['};']))
    if not any(d.startswith(f'struct {cls} ') for d in out):
        return None
    if base is not None:
        calls = {t: c for t, c in calls.items() if t != base[0]}
    return out, '\n'.join([f'{cls}::{cls}({params}) {{'] + body + ['}']), cls, calls


def offset_order(code):
    """The constructor with each run of member stores sorted by offset: the
    order members are usually initialized in (declaration order)."""
    lines = code.split('\n')
    def key(line):
        m = re.match(r'\s+this->f_([0-9a-f]+)\b', line)
        return int(m.group(1), 16) if m else -1
    for start, end in runs(lines):
        lines[start:end] = sorted(lines[start:end], key=key)
    return '\n'.join(lines)


def bodies(code):
    """Constructor bodies to try: offset order first, the lifted order, then
    other orders of each run of assignments."""
    first = offset_order(code)
    return list(dict.fromkeys([first, code] + reorders(first) + reorders(code)))[:VARIANTS]


def main():
    orig, functions = Original(), load_functions()
    arity = arity_finder(orig, functions)
    toc_of = toc_bases(orig)
    vtables = {a for addrs in json.loads((BUILD / 'vtables.json').read_text()).values() for a in addrs}
    report = json.loads((ROOT / 'progress' / 'report.json').read_text(encoding='utf-8'))
    matched = {int(f['metadata']['virtualAddress']) for u in report['units'] for f in u.get('functions', [])
               if float(f.get('fuzzyMatchPercent', 0)) == 100.0}
    ours = ROOT / 'config' / 'symbols_cls.txt'                  # this tool's own earlier matches: redo them
    if ours.exists():
        matched -= {int(l.split()[0], 16) for l in ours.read_text().splitlines() if l and not l.startswith('#')}
    WORK.mkdir(parents=True, exist_ok=True)
    for old in WORK.glob('*'):
        old.unlink()
    jobs, info = [], {}
    for address, size in sorted(functions.items()):
        if address in matched or size // 4 > lift.MAX_WORDS or address not in toc_of:
            continue
        words = struct.unpack(f'>{size // 4}I', orig.read(address, size))
        if not any(w >> 26 == 36 and w & 0xFFFF == 0 for w in words):
            continue                                     # no word store at offset 0
        result = lift.lift(address, words, arity)
        if result is None:
            continue
        decls, code, calls = result
        vtable_globals = set()
        for d, name in lift.LAST.globals.items():
            value = struct.unpack('>I', orig.read(toc_of[address] + d, 4))[0]
            if value in vtables:
                vtable_globals.add(name)
        made = constructor(address, decls, code, vtable_globals, calls) if vtable_globals else None
        if made is None:
            continue
        decls, code, cls, calls = made
        for k, body in enumerate(bodies(code)):
            path = WORK / f'{address:08x}_{k}.cpp'
            path.write_text(file_text([], calls, [(address, decls, body)]), encoding='utf-8', newline='\n')
            jobs.append(path)
            info[path] = (address, decls, body, cls, calls)
    print(f'{len(jobs)} constructor variants of {len({i[0] for i in info.values()})} functions')
    if not jobs:
        return
    compiled = compile_all(jobs)
    found = {}
    for path in jobs:
        obj_path = compiled[path]
        if isinstance(obj_path, str):
            continue
        address, decls, code, cls, calls = info[path]
        if address in found:
            continue
        obj = Object(obj_path)
        for name, section, start, size in obj.functions():
            if not name.startswith(f'._ZN{len(cls)}{cls}C2E'):
                continue
            ours = obj.section_bytes(section)[start:start + size]
            fuzzy, exact = compare(ours, orig.read(address, functions[address]), reloc_masks(obj, section, start, size))
            if exact:
                found[address] = (decls, code, calls, name)
    print(f'{len(found)} constructors match')
    units = load_units()
    starts = [u[1] for u in units]
    groups = defaultdict(list)
    for address, item in sorted(found.items()):
        i = bisect.bisect_right(starts, address) - 1
        groups[units[i][0] if i >= 0 and address < units[i][2] else 'unknown'].append((address,) + item)
    CLS.mkdir(parents=True, exist_ok=True)
    for old in CLS.glob('*.cpp'):
        old.unlink()
    symbols = ['# Generated by tools/classgen.py: address and compiled symbol of constructors',
               '# matched as C++ classes.']
    for unit, items in groups.items():
        files = []                                       # [(callees, keep)]
        for address, decls, code, calls, name in items:
            for callees, keep in files:
                if all(callees.get(t, c) == c for t, c in calls.items()):
                    callees.update(calls)
                    keep.append((address, decls, code))
                    break
            else:
                files.append((dict(calls), [(address, decls, code)]))
            symbols.append(f'{address:08x} {name}')
        for n, (callees, keep) in enumerate(files):
            header = [f'// Generated by tools/classgen.py: constructors of unit {unit}, written as',
                      '// C++ classes (names are placeholders until the classes are identified).']
            (CLS / f'{short_name(unit)}{"" if n == 0 else f"_{n}"}.cpp').write_text(
                file_text(header, callees, keep), encoding='utf-8', newline='\n')
    (ROOT / 'config' / 'symbols_cls.txt').write_text('\n'.join(symbols) + '\n', encoding='utf-8', newline='\n')


if __name__ == '__main__':
    main()
