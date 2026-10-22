"""Write C for small functions whose whole body is one known shape, into
src/gen/<unit>.cpp. Every generated function still has to match byte for
byte: check.py compares them like any other, and --prune drops the ones it
did not report as MATCH.

    python tools/gen_simple.py                 # generate
    python tools/check.py -v > build/check.log
    python tools/gen_simple.py --prune build/check.log

Shapes (r3 = first argument, f1 = first float argument):
    blr                                   empty function
    li r3,K; blr                          return a constant
    L r3,d(r3); [extsw r3,r3;] blr        return a field (lwz/lbz/lhz/lha/lfs/lfd/ld)
    S r4,d(r3); blr                       store the argument in a field (stw/stb/sth/stfs/stfd/std)
    li r0,K; S r0,d(r3)...; blr           store a constant in one or more fields
    addi r3,r3,d; clrldi r3,r3,32; blr    return the address of a field
    lwz r3,d(r2); blr                     return the address of a global
    lwz r9,d(r2); L r3,e(r9); blr         return a global's field
    lwz r0,d(r2); stw r0,e(r3); blr       store a global's address in a field
"""
import bisect
import re
import struct
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, Original, load_functions, load_units

OPD = (0x12d9b90, 0x72388)     # function descriptors: {code, toc}

BLR = 0x4E800020
LOADS = {32: 'u32', 34: 'u8', 40: 'u16', 42: 's16', 48: 'float', 50: 'double'}
STORES = {36: 'u32', 38: 'u8', 44: 'u16', 52: 'float', 54: 'double'}
GEN = ROOT / 'src' / 'gen'


def s16(v):
    return v - 0x10000 if v & 0x8000 else v


def fields(w):
    return w >> 26, (w >> 21) & 31, (w >> 16) & 31, w & 0xFFFF


SIZE = {'u8': 1, 'u16': 2, 's16': 2, 'u32': 4, 'float': 4, 'double': 8, 'u64': 8}


def struct_layout(tag, fields_at):
    """struct tag { ... } with each (offset, type) at its offset, or None if
    a field is misaligned or overlaps another."""
    out, at = [f'struct {tag} {{'], 0
    for off, t in fields_at:
        if off < at or off % SIZE[t]:
            return None
        if off > at:
            out.append(f'    char pad_{at:x}[{off - at}];')
        out.append(f'    {t} f_{off:x};')
        at = off + SIZE[t]
    out.append('};')
    return '\n'.join(out)


def generate(address, words, ctx=None):
    """C for one function, or None if its body is not a known shape."""
    name = f'fn_{address:08x}'
    n = len(words)
    if words == (BLR,):
        return f'void {name}(void) {{}}'
    if n == 2 and words[1] == BLR:
        op, rt, ra, d = fields(words[0])
        if op == 14 and rt == 3 and ra == 0:
            return f'int {name}(void) {{ return {s16(d)}; }}'
        if op in LOADS and ra == 3 and rt == (1 if op in (48, 50) else 3):
            return f'{LOADS[op]} {name}(void* self) {{ return FIELD({LOADS[op]}, self, {s16(d)}); }}'
        if op == 58 and ra == 3 and rt == 3 and d & 3 == 0:
            return f'u64 {name}(void* self) {{ return FIELD(u64, self, {s16(d)}); }}'
        if op in STORES and ra == 3 and rt == (1 if op in (52, 54) else 4):
            return f'void {name}(void* self, {STORES[op]} value) {{ FIELD({STORES[op]}, self, {s16(d)}) = value; }}'
        if op == 62 and ra == 3 and rt == 4 and d & 3 == 0:
            return f'void {name}(void* self, u64 value) {{ FIELD(u64, self, {s16(d)}) = value; }}'
        if op == 32 and rt == 3 and ra == 2:
            return f'extern char g_{address:08x}[];\nvoid* {name}(void) {{ return g_{address:08x}; }}'
    if n == 3 and words[2] == BLR:
        a, b = fields(words[0]), fields(words[1])
        if a[0] == 32 and a[1:3] == (3, 3) and words[1] == 0x7C6307B4:   # lwz; extsw
            return f'int {name}(void* self) {{ return FIELD(int, self, {s16(a[3])}); }}'
        if a[0] == 14 and a[1:3] == (3, 3) and words[1] == 0x78630020:   # addi; clrldi
            return f'void* {name}(void* self) {{ return (char*)self + {s16(a[3])}; }}'
        if a[0] == 32 and a[1:3] == (9, 2) and b[0] in LOADS and b[1] == (1 if b[0] in (48, 50) else 3) and b[2] == 9:
            t = LOADS[b[0]]
            return (f'extern char g_{address:08x}[];\n'
                    f'{t} {name}(void) {{ return FIELD({t}, g_{address:08x}, {s16(b[3])}); }}')
        if a[0] == 32 and a[1:3] == (0, 2) and b[0] == 36 and b[1:3] == (0, 3):
            return (f'extern char g_{address:08x}[];\n'
                    f'void {name}(void* self) {{ FIELD(void*, self, {s16(b[3])}) = g_{address:08x}; }}')
    if n >= 3 and words[-1] == BLR:
        a = fields(words[0])
        rest = [fields(w) for w in words[1:-1]]
        if a[0] == 14 and a[1:3] == (0, 0) and rest and all(r[0] in STORES and r[0] not in (52, 54)
                                                             and r[1:3] == (0, 3) for r in rest):
            # A real structure, fields set in declaration order: with casts
            # through char* the compiler schedules the stores differently.
            stores = sorted((s16(r[3]), STORES[r[0]]) for r in rest)
            layout = struct_layout(f'unk_{address:08x}', stores)
            if layout:
                body = ' '.join(f'self->f_{off:x} = {s16(a[3])};' for off, _ in stores)
                return f'{layout}\nvoid {name}(unk_{address:08x}* self) {{ {body} }}'
    if n == 3 and words[2] == BLR:
        a, b = fields(words[0]), fields(words[1])
        if a[0] == 32 and a[1:3] == (9, 2) and words[1] == 0x91230000:
            # An empty destructor of a derived class: the base destructor is
            # inlined, and only the base's vtable pointer store survives.
            cls, base = f'gen_{address:08x}', f'gen_base_{address:08x}'
            code = (f'struct {base} {{ virtual ~{base}() {{}} }};\n'
                    f'struct {cls} : {base} {{ virtual ~{cls}(); }};\n'
                    f'{cls}::~{cls}() {{}}')
            return ('class', code, f'._ZN{len(cls)}{cls}D1Ev')
        if a[0] == 32 and a[1:3] == (9, 2) and b[0] in STORES and b[2] == 9 and b[1] == (1 if b[0] in (52, 54) else 3):
            t = STORES[b[0]]
            return (f'extern char g_{address:08x}[];\n'
                    f'void {name}({t} value) {{ FIELD({t}, g_{address:08x}, {s16(b[3])}) = value; }}')
        if a[0] == 32 and a[1:3] == (9, 3) and b[0] in LOADS and b[2] == 9 and b[1] == (1 if b[0] in (48, 50) else 3):
            t = LOADS[b[0]]
            return f'{t} {name}(void* self) {{ return FIELD({t}, FIELD(void*, self, {s16(a[3])}), {s16(b[3])}); }}'
        if a[0] == 32 and a[1:3] == (9, 3) and b[0] in STORES and b[2] == 9 and b[1] == (1 if b[0] in (52, 54) else 4):
            t = STORES[b[0]]
            return (f'void {name}(void* self, {t} value) '
                    f'{{ FIELD({t}, FIELD(void*, self, {s16(a[3])}), {s16(b[3])}) = value; }}')
        if a[0] == 48 and a[1:3] == (1, 2) and ctx and address in ctx['toc']:
            value = struct.unpack('>f', ctx['orig'].read(ctx['toc'][address] + s16(a[3]), 4))[0]
            if value == value and abs(value) != float('inf'):
                return f'float {name}(void) {{ return {float(value)!r}f; }}'
    if n == 4 and words[3] == BLR:
        a, b, c = fields(words[0]), fields(words[1]), fields(words[2])
        if a[0] == 32 and a[1:3] == (9, 2) and b[0] in LOADS and b[1:3] == (3, 9) and words[2] == 0x7C6307B4:
            return (f'extern char g_{address:08x}[];\n'
                    f'int {name}(void) {{ return FIELD(int, g_{address:08x}, {s16(b[3])}); }}')
        if a[0] == 32 and a[1:3] == (9, 2) and b[0] == 14 and b[1:3] == (0, 0) and c[0] in STORES \
                and c[0] not in (52, 54) and c[1:3] == (0, 9):
            return (f'extern char g_{address:08x}[];\n'
                    f'void {name}(void) {{ FIELD({STORES[c[0]]}, g_{address:08x}, {s16(c[3])}) = {s16(b[3])}; }}')
        if a[0] == 14 and a[1:3] == (0, 0) and b[0] == 14 and b[1:3] == (3, 0) and c[0] in STORES \
                and c[0] not in (52, 54) and c[1:3] == (0, 4):
            return (f'int {name}(void* self, void* other) '
                    f'{{ FIELD({STORES[c[0]]}, other, {s16(c[3])}) = {s16(a[3])}; return {s16(b[3])}; }}')
    return None


def rldicl_mask(w):
    """Bits kept by 'clrldi rA,rA,n' (rldicl with shift 0), or None."""
    if w >> 26 != 30 or (w >> 2) & 7 != 0 or (w >> 21) & 31 != (w >> 16) & 31:
        return None
    sh = ((w >> 11) & 31) | (((w >> 1) & 1) << 5)
    mb = (((w >> 6) & 31)) | (((w >> 5) & 1) << 5)
    return None if sh else {48: 'u16', 56: 'u8', 32: 'u32'}.get(mb)


def thunk(address, words):
    """(target, [(C type, value)], C body) for 'li r3..rN,K; [clrldi]; b target', or None."""
    last = words[-1]
    if last >> 26 != 18 or last & 3:
        return None
    offset = last & 0x03FFFFFC
    if offset & 0x02000000:
        offset -= 0x04000000
    target = address + 4 * (len(words) - 1) + offset
    regs = {}
    for w in words[:-1]:
        op, rt, ra, d = fields(w)
        if op == 14 and ra == 0 and 3 <= rt <= 10:
            regs[rt] = ['int', s16(d)]
            continue
        kind = rldicl_mask(w)
        if kind and rt in regs and regs[rt][0] == 'int':
            regs[rt][0] = kind
            continue
        return None
    if not regs or sorted(regs) != list(range(3, 3 + len(regs))):
        return None
    args = []
    for r in sorted(regs):
        kind, value = regs[r]
        value = value & {'u16': 0xFFFF, 'u8': 0xFF, 'u32': 0xFFFFFFFF}.get(kind, -1) if kind != 'int' else value
        args.append((kind, value))
    return target, args


def main():
    prune = None
    if '--prune' in sys.argv:
        log = Path(sys.argv[sys.argv.index('--prune') + 1]).read_text(encoding='utf-8', errors='replace')
        prune = {int(m.group(1), 16) for m in re.finditer(r'^([0-9a-f]{8}) +[\d.]+% (?!MATCH)', log, re.M)}
    orig, functions = Original(), load_functions()
    units = load_units()
    opd = orig.read(*OPD)
    toc_of = {}
    for i in range(0, len(opd) - 7, 8):
        code, toc = struct.unpack_from('>II', opd, i)
        toc_of[code] = toc
    ctx = {'orig': orig, 'toc': toc_of}
    starts = [u[1] for u in units]
    by_unit = defaultdict(list)
    thunks = defaultdict(list)
    for address, size in sorted(functions.items()):
        if size > 64 or (prune and address in prune):
            continue
        words = struct.unpack(f'>{size // 4}I', orig.read(address, size))
        code = generate(address, words, ctx)
        i = bisect.bisect_right(starts, address) - 1
        if not (i >= 0 and address < units[i][2]):
            continue
        if code is not None:
            by_unit[units[i][0]].append((address, code))
            continue
        call = thunk(address, words) if len(words) >= 2 else None
        if call:
            thunks[units[i][0]].append((address,) + call)
    for old in list(GEN.glob('*.cpp')) + list(GEN.glob('*.c')):
        old.unlink()
    GEN.mkdir(parents=True, exist_ok=True)
    total = 0
    symbols = ['# Generated by tools/gen_simple.py: address and compiled symbol of',
               '# generated functions that are not named fn_<address>.']
    for unit, items in by_unit.items():
        text = ['// Generated by tools/gen_simple.py: functions whose whole body is a simple',
                f'// shape, from unit {unit}. Each one compiles to the original bytes.',
                '#include "wkc2.h"', '']
        for address, code in items:
            if isinstance(code, tuple):          # C++ classes, mapped by symbol
                text += [code[1], '']
                symbols.append(f'{address:08x} {code[2]}')
                total += 1
        text += ['extern "C" {', '']
        for address, code in items:
            if not isinstance(code, tuple):
                text += [code, '']
                total += 1
        text.append('}')
        (GEN / f'{unit}.cpp').write_text('\n'.join(text) + '\n', encoding='utf-8', newline='\n')
    for unit, items in thunks.items():
        # Tail calls: GCC 4.1 only jumps (b) to a static function of the same
        # file, so each callee gets a placeholder stub here until it is
        # decompiled. check.py ignores stub_* functions.
        stubs = {}
        for _, target, args in items:
            stubs.setdefault(target, [k for k, _ in args])
        text = ['/* Generated by tools/gen_simple.py: tail calls with constant arguments,',
                f' * from unit {unit}. Each one compiles to the original bytes. */',
                '#include "wkc2.h"', '']
        for target, kinds in sorted(stubs.items()):
            params = ', '.join(f'{k} a{n}' for n, k in enumerate(kinds))
            uses = ', '.join(f'"r"(a{n})' for n in range(len(kinds)))
            text += [f'/* Placeholder for the callee at 0x{target:08x}, not yet decompiled. */',
                     f'static void __attribute__((noinline)) stub_{target:08x}({params})',
                     f'{{ __asm__ volatile ("" : : {uses}); }}', '']
        for address, target, args in items:
            if [k for k, _ in args] != stubs[target]:
                continue
            values = ', '.join(str(v) for _, v in args)
            text += [f'void fn_{address:08x}(void) {{ stub_{target:08x}({values}); }}', '']
            total += 1
        (GEN / f'{unit}.c').write_text('\n'.join(text) + '\n', encoding='utf-8', newline='\n')
    (ROOT / 'config' / 'symbols_gen.txt').write_text('\n'.join(symbols) + '\n', encoding='utf-8', newline='\n')
    print(f'{total} functions generated in {len(by_unit)} files' + (f' ({len(prune)} pruned)' if prune else ''))


if __name__ == '__main__':
    main()
