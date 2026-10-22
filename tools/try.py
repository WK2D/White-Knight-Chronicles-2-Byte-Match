"""Compile one source file and compare its fn_<address> functions with the
original: a quick loop for trying source idioms by hand. The file must be
inside the repository (build/try/ is a good place for experiments).

    python tools/try.py build/try/x.cpp        # match percent per function
    python tools/try.py build/try/x.cpp -d     # and a side-by-side diff
    python tools/try.py build/try/x.cpp -O2    # other compiler flags than the project's
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check
from check import compare, compile_all, reloc_masks
from common import ROOT, Object, Original, load_functions
from diff import instructions, objdump


def main():
    src = Path(sys.argv[1]).resolve()
    flags = [a for a in sys.argv[2:] if a.startswith(('-O', '-f', '-m'))]
    if flags:
        check.CFLAGS = flags
    result = compile_all([src])[src]
    if isinstance(result, str):
        raise SystemExit(result)
    orig, functions = Original(), load_functions()
    obj = Object(result)
    for name, section, start, size in obj.functions():
        m = re.fullmatch(r'\.fn_([0-9a-f]{8})', name)
        if not m:
            continue
        address = int(m.group(1), 16)
        ours = obj.section_bytes(section)[start:start + size]
        theirs = orig.read(address, functions[address])
        fuzzy, exact = compare(ours, theirs, reloc_masks(obj, section, start, size))
        print(f'{address:08x} {fuzzy:6.2f}% {"MATCH" if exact else ""}')
        if '-d' in sys.argv and not exact:
            a = instructions(objdump(['-D', f'--start-address={address:#x}',
                                      f'--stop-address={address + functions[address]:#x}', 'orig/EBOOT.elf']))
            text = objdump(['-dr', result.relative_to(ROOT).as_posix()])
            body = re.search(rf'^[0-9a-f]+ <{re.escape(name)}>:\n(.*?)(?:\n\n|\Z)', text, re.S | re.M)
            b = instructions(body.group(1)) if body else []
            for i in range(max(len(a), len(b))):
                x = a[i] if i < len(a) else ('', '')
                y = b[i] if i < len(b) else ('', '')
                mark = ' ' if x[0] == y[0] else ('~' if x[0][:2] == y[0][:2] else '!')
                print(f'{mark} {x[0]:8} {x[1][:44]:44} | {y[0]:8} {y[1]}')


if __name__ == '__main__':
    main()
