"""Find C++ classes in the original EBOOT through RTTI: type names, their
type_info objects, the vtables that point at those, and the methods the
vtables list. Writes build/classes.json:
{class name: [method addresses]}.

    python tools/classes.py
"""
import json
import re
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import BUILD, Original, load_functions

TEXT = (0x10240, 0x10240 + 0xfb95fc)


def demangle_type(name):
    """'10CCharacter' -> 'CCharacter'; 'N4Game6CActorE' -> 'Game::CActor'."""
    def take(s, i):
        m = re.match(r'(\d+)', s[i:])
        n = int(m.group(1))
        start = i + len(m.group(1))
        return s[start:start + n], start + n
    try:
        if name.startswith('N') and name.endswith('E'):
            parts, i = [], 1
            while i < len(name) - 1:
                part, i = take(name, i)
                parts.append(part)
            return '::'.join(parts)
        part, i = take(name, 0)
        return part if i == len(name) else None
    except (AttributeError, ValueError):
        return None


def main():
    orig = Original()
    functions = load_functions()
    data = orig.data
    segs = orig.segments
    def va_of(off):
        for va, o, n in segs:
            if o <= off < o + n:
                return va + off - o
    def word(va):
        return struct.unpack('>I', orig.read(va, 4))[0]
    # Every big-endian word in the loaded data, by value -> addresses holding it.
    by_value = {}
    for va, off, n in segs:
        if va < TEXT[1] and va + n > TEXT[0] and va <= TEXT[0]:
            continue   # skip the code segment
        for i in range(0, n - 3, 4):
            v = struct.unpack_from('>I', data, off + i)[0]
            by_value.setdefault(v, []).append(va + i)
    # Type names: NUL-terminated mangled class names in read-only data.
    classes, vtables = {}, {}
    for m in re.finditer(rb'(?<=\x00)(N?\d{1,3}[A-Za-z_][A-Za-z0-9_]*E?)\x00', data):
        name = demangle_type(m.group(1).decode())
        if not name:
            continue
        name_va = va_of(m.start(1))
        if name_va is None:
            continue
        # type_info object: { vtable of the type_info class, pointer to the name }
        for ti_name_ptr in by_value.get(name_va, []):
            ti = ti_name_ptr - 4
            # vtable: { offset to top, type_info*, virtual functions... }
            for vt_ti in by_value.get(ti, []):
                methods = []
                at = vt_ti + 4
                while len(methods) < 512:
                    try:
                        d = word(at)
                        code, toc = word(d), word(d + 4)
                    except ValueError:
                        break
                    if code not in functions:
                        break
                    methods.append(code)
                    at += 4
                if methods:
                    classes.setdefault(name, set()).update(methods)
                    vtables.setdefault(name, set()).add(vt_ti + 4)   # what an object's vptr holds
    BUILD.mkdir(exist_ok=True)
    out = {k: sorted(v) for k, v in sorted(classes.items())}
    (BUILD / 'classes.json').write_text(json.dumps(out, indent=1), encoding='utf-8')
    (BUILD / 'vtables.json').write_text(json.dumps({k: sorted(v) for k, v in sorted(vtables.items())}),
                                        encoding='utf-8')
    labelled = set(a for v in out.values() for a in v)
    print(f'{len(out)} classes with vtables, {len(labelled)} distinct virtual methods '
          f'({sum(functions[a] for a in labelled) / sum(functions.values()):.1%} of code)')
    for k in list(out)[:12]:
        print(' ', k, len(out[k]))


if __name__ == '__main__':
    main()
