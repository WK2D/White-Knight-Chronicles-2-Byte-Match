"""Find entries of config/functions.txt that are pieces of the function
before them: no descriptor (OPD), never the target of a call, and the
target of a branch from the previous entry."""
import struct, sys
from collections import Counter
sys.path.insert(0, 'tools')
from common import Original, ROOT
orig = Original()
def bt(a, w):
    off = w & 0x03FFFFFC
    if off & 0x02000000: off -= 0x04000000
    return a + off
def s16(v): return v - 0x10000 if v & 0x8000 else v
lines = (ROOT / 'config' / 'functions.txt').read_text().splitlines()
entries = [(int(l.split()[0], 16), int(l.split()[1], 16)) for l in lines if l and not l.startswith('#')]
opd = orig.read(0x12d9b90, 0x72388)
described = {struct.unpack_from('>I', opd, i)[0] for i in range(0, len(opd) - 7, 8)}
called, branched_from = set(), {}
for a, s in entries:
    w = struct.unpack(f'>{s//4}I', orig.read(a, s))
    for i, x in enumerate(w):
        pc = a + 4 * i
        if x >> 26 == 18 and x & 3 == 1: called.add(bt(pc, x))
        elif x >> 26 == 18 and x & 3 == 0: branched_from.setdefault(bt(pc, x), set()).add(a)
        elif x >> 26 == 16 and x & 3 == 0: branched_from.setdefault(pc + s16(x & 0xFFFC), set()).add(a)
pieces = []
for (pa, ps), (a, s) in zip(entries, entries[1:]):
    if a in described or a in called: continue
    if pa in branched_from.get(a, ()):
        pieces.append((a, s, pa))
cond = set()
for a, s_ in entries:
    w = struct.unpack(f'>{s_//4}I', orig.read(a, s_))
    for i, x in enumerate(w):
        if x >> 26 == 16 and x & 3 == 0: cond.add(a + 4 * i + s16(x & 0xFFFC))
pieces = [p_ for p_ in pieces if p_[0] in cond]          # a conditional branch, so not a tail call
print(len(pieces), 'pieces of the previous function')
if '--write' in sys.argv:
    merge = {a for a, _, _ in pieces}
    out = []
    for a, s in entries:
        if a in merge and out and out[-1][0] + out[-1][1] == a:
            out[-1] = (out[-1][0], out[-1][1] + s)
        else:
            out.append((a, s))
    header = [l for l in lines if l.startswith('#')]
    header[-1] = header[-1].rstrip(')') + ' Other pieces with no'
    header.append('#                 descriptor that no call targets and the function before branches to are merged too.)')
    (ROOT / 'config' / 'functions.txt').write_text('\n'.join(header + [f'{a:08x} {s:x}' for a, s in out]) + '\n', newline='\n')
    print(len(entries), '->', len(out))
