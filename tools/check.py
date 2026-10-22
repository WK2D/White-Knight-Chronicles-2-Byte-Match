"""Compile src/, compare every mapped function with the original EBOOT, and
write progress/report.json (objdiff report format, version 2).

    python tools/check.py            # compile, compare, report
    python tools/check.py -v         # also list every compared function
    python tools/check.py --compile-only   # CI: compile and check the symbol map only

A function matches when its compiled bytes equal the original's, except for
fields a linker fills in: the targets of calls and branches to other
functions (REL24/REL14) and 16-bit address/TOC offsets (ADDR16*, TOC16*).
A call the original makes through a TOC-switching stub keeps its TOC
restore (ld r2,40(r1)) where the object has the linker's placeholder nop.
"""
import fnmatch
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (BUILD, CFLAGS, COMPILER, ROOT, Object, Original, load_flags, load_functions,
                    load_symbols, load_units)

REL24 = {10}                                   # R_PPC64_REL24
REL14 = {11, 12, 13}                           # R_PPC64_REL14[_BRTAKEN|_BRNTAKEN]
HALF = {4, 5, 6, 47, 48, 49, 50, 69, 70, 71, 72}   # ADDR16_LO/HI/HA, TOC16, TOC16_LO/HI/HA, TPREL16*
HALF_DS = {56, 57, 63, 64, 95, 96}             # ADDR16_DS/LO_DS, TOC16_DS/LO_DS, TPREL16_DS/LO_DS
NOP, TOC_RESTORE = 0x60000000, 0xE8410028


def reloc_masks(obj, section, start, size):
    """{instruction offset within the function: (mask of fields to ignore, kind)}"""
    out = {}
    for offset, rtype, _ in obj.relocations.get(section, []):
        insn = (offset & ~3) - start
        if not 0 <= insn < size:
            continue
        if rtype in REL24:
            out[insn] = (0x03FFFFFC, 'call')
        elif rtype in REL14:
            out[insn] = (0x0000FFFC, 'branch')
        elif rtype in HALF:
            out[insn] = (0x0000FFFF, 'half')
        elif rtype in HALF_DS:
            out[insn] = (0x0000FFFC, 'half')
        else:
            out[insn] = (0xFFFFFFFF, f'type {rtype}')
    return out


def compare(ours, orig, masks):
    """(fuzzy match percent, exact match) for one function."""
    n_ours, n_orig = len(ours) // 4, len(orig) // 4
    same = 0
    for i in range(min(n_ours, n_orig)):
        a = int.from_bytes(ours[4 * i:4 * i + 4], 'big')
        b = int.from_bytes(orig[4 * i:4 * i + 4], 'big')
        mask, kind = masks.get(4 * i, (0, None))
        if (a & ~mask) == (b & ~mask):
            same += 1
        elif a == NOP and b == TOC_RESTORE and masks.get(4 * i - 4, (0, None))[1] == 'call':
            same += 1
    total = max(n_ours, n_orig)
    return (100.0 * same / total if total else 100.0), (same == total and n_ours == n_orig)


def compile_all(sources):
    """Compile every source in one compiler session; {source: object path or error}."""
    BUILD.mkdir(exist_ok=True)
    jobs, results = [], {}
    for src in sources:
        obj = BUILD / 'obj' / (src.relative_to(ROOT).as_posix() + '.o')   # unit.c and unit.cpp differ
        obj.parent.mkdir(parents=True, exist_ok=True)
        obj.unlink(missing_ok=True)       # a stale object would hide a failed compile
        jobs.append((src.relative_to(ROOT).as_posix(), obj.relative_to(ROOT).as_posix()))
        results[src] = obj
    if os.name == 'nt':
        tools = '/c'
    else:
        tools = str(COMPILER)
    overrides = load_flags()
    # One compiler process per source, as many at once as there are CPUs;
    # .c files go through the C driver, everything else through C++. Flags
    # travel comma-joined so each job stays four words.
    lines = []
    for src, obj in jobs:
        driver = 'gcc' if src.endswith('.c') else 'g++'
        flags = next((f for pattern, f in overrides if fnmatch.fnmatch(src, pattern)), CFLAGS)
        lines.append(f'{tools}/bin/ppu-lv2-{driver} {src} {obj} {",".join(flags)}' + '\n')
    (BUILD / 'jobs.txt').write_text(''.join(lines), newline='\n')
    one = (f'"$0" -B{tools}/libexec/gcc/ppu-lv2/4.1.1/ -B{tools}/bin/ppu-lv2- $(echo "$3" | tr , " ") -Iinclude '
           '-c "$1" -o "$2" 2> "$2.log" || echo FAILED "$1"')
    script = ['#!/bin/sh', f"xargs -P \"$(nproc)\" -n 4 sh -c '{one}' < build/jobs.txt"]
    (BUILD / 'compile.sh').write_text('\n'.join(script) + '\n', newline='\n')
    if os.name == 'nt':
        cmd = ['docker', 'run', '--rm', '-v', f'{COMPILER}:/c:ro', '-v', f'{ROOT}:/w', '-w', '/w',
               os.environ.get('WKC2_CC_IMAGE', 'ubuntu:14.04'), 'sh', 'build/compile.sh']
    else:
        cmd = ['sh', 'build/compile.sh']
    out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                         env=dict(os.environ, MSYS_NO_PATHCONV='1'))
    failed = {line.split(' ', 1)[1] for line in out.stdout.splitlines() if line.startswith('FAILED ')}
    for src in sources:
        # Parallel jobs can interleave their FAILED lines, so a missing object counts too.
        if src.relative_to(ROOT).as_posix() in failed or not results[src].exists():
            results[src] = (BUILD / 'obj' / (src.relative_to(ROOT).as_posix() + '.o.log')).read_text()
    return results


def measures(items):
    """objdiff Measures for [(size, fuzzy percent, exact)]."""
    total = sum(s for s, _, _ in items)
    matched = sum(s for s, _, e in items if e)
    fuzzy = sum(s * f for s, f, _ in items) / total if total else 0.0
    n = len(items)
    m = sum(1 for _, _, e in items if e)
    return {'fuzzyMatchPercent': fuzzy, 'totalCode': str(total), 'matchedCode': str(matched),
            'matchedCodePercent': 100.0 * matched / total if total else 0.0,
            'totalFunctions': n, 'matchedFunctions': m,
            'matchedFunctionsPercent': 100.0 * m / n if n else 0.0}


def main():
    verbose = '-v' in sys.argv
    compile_only = '--compile-only' in sys.argv
    functions, units, symbols = load_functions(), load_units(), load_symbols()
    orig = None if compile_only else Original()
    sources = sorted(p for ext in ('*.c', '*.cpp') for p in (ROOT / 'src').rglob(ext))
    compiled = compile_all(sources)
    results, problems = {}, []
    for src, obj_path in compiled.items():
        if isinstance(obj_path, str):
            problems.append(f'{src.relative_to(ROOT)}: compile failed\n{obj_path}')
            continue
        obj = Object(obj_path)
        for name, section, start, size in obj.functions():
            if name.startswith('.stub_'):
                continue                      # placeholder callee of a generated thunk
            m = re.fullmatch(r'\.fn_([0-9a-f]{8})', name)   # C-linkage fn_<address> maps itself
            if m:
                address = int(m.group(1), 16)
            elif name in symbols:
                address = symbols[name]
            else:
                if 'src/gen/' not in src.relative_to(ROOT).as_posix():   # generated files carry extra
                    problems.append(f'{src.relative_to(ROOT)}: {name} is not in config/symbols.txt')
                continue                  # compiler-made variants (D0/D2 destructors)
            if address not in functions:
                problems.append(f'{name}: {address:#x} is not a known function start')
                continue
            if compile_only:
                continue
            ours = obj.section_bytes(section)[start:start + size]
            theirs = orig.read(address, functions[address])
            fuzzy, exact = compare(ours, theirs, reloc_masks(obj, section, start, size))
            results[address] = (fuzzy, exact, name, src)
            if verbose:
                print(f'{address:08x} {fuzzy:6.2f}% {"MATCH" if exact else "     "} {name}')
    if compile_only:
        for p in problems:
            print('error:', p)
        print(f'{len(compiled)} sources compiled' + ('' if not problems else f', {len(problems)} problems'))
        return 1 if problems else 0
    # Group every original function into its unit.
    by_unit = defaultdict(list)
    unit_of = {}
    for name, start, end, category in units:
        unit_of[(start, end)] = (name, category)
    bounds = sorted(unit_of)
    import bisect
    starts = [s for s, _ in bounds]
    for address, size in functions.items():
        i = bisect.bisect_right(starts, address) - 1
        if i < 0 or address >= bounds[i][1]:
            continue
        fuzzy, exact, name, _ = results.get(address, (0.0, False, f'fn_{address:08x}', None))
        by_unit[bounds[i]].append((address, size, fuzzy, exact, name))
    report_units, everything, categories = [], [], defaultdict(list)
    for key in bounds:
        name, category = unit_of[key]
        items = [(s, f, e) for _, s, f, e, _ in by_unit[key]]
        everything += items
        categories[category] += items
        report_units.append({
            'name': name,
            'measures': {**measures(items), 'totalUnits': 1,
                         'completeUnits': int(all(e for _, _, e in items) and bool(items))},
            'functions': [{'name': n, 'size': str(s), 'fuzzyMatchPercent': f,
                           'metadata': {'virtualAddress': str(a)}}
                          for a, s, f, e, n in sorted(by_unit[key])],
            'metadata': {'complete': False, 'progressCategories': [category],
                         'autoGenerated': name.startswith('auto_')},
        })
    report = {
        'measures': {**measures(everything), 'totalUnits': len(report_units),
                     'completeUnits': sum(u['measures']['completeUnits'] for u in report_units)},
        'units': report_units,
        'version': 2,
        'categories': [{'id': c, 'name': c, 'measures': measures(v)} for c, v in sorted(categories.items())],
    }
    out = ROOT / 'progress' / 'report.json'
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, separators=(',', ':')) + '\n', encoding='utf-8', newline='\n')
    m = report['measures']
    print(f"{m['matchedFunctions']}/{m['totalFunctions']} functions match "
          f"({m['matchedCodePercent']:.4f}% of code, fuzzy {m['fuzzyMatchPercent']:.4f}%)")
    for p in problems:
        print('warning:', p)
    return 1 if any('compile failed' in p for p in problems) else 0


if __name__ == '__main__':
    sys.exit(main())
