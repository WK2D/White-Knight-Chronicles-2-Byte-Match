"""Write config/units.txt: provisional units of about 64 KB of .text each,
cut at function starts. Each is replaced by a real source file's unit once
that file's extent in the binary is known.

    python tools/make_units.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, load_functions

CHUNK = 0x10000


def main():
    functions = sorted(load_functions().items())
    text_end = functions[-1][0] + functions[-1][1]
    lines = ['# name start end category',
             '# auto_* units are provisional 64 KB slices; real source files replace them.']
    start = functions[0][0]
    for address, _ in functions[1:]:
        if address - start >= CHUNK:
            lines.append(f'auto_{start:08x} {start:08x} {address:08x} auto')
            start = address
    lines.append(f'auto_{start:08x} {start:08x} {text_end:08x} auto')
    (ROOT / 'config' / 'units.txt').write_text('\n'.join(lines) + '\n', newline='\n')
    print(len(lines) - 2, 'units')


if __name__ == '__main__':
    main()
