"""Show one function side by side: the original's disassembly and the
compiled source's, with differing lines marked. Run tools/check.py first so
the object exists.

    python tools/diff.py 004b32e8
"""
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import BUILD, COMPILER, ROOT, Object, load_functions, load_symbols


def objdump(args):
    if os.name == 'nt':
        cmd = ['docker', 'run', '--rm', '-v', f'{COMPILER}:/c:ro', '-v', f'{ROOT}:/w', '-w', '/w',
               'ubuntu:14.04', '/c/bin/ppu-lv2-objdump'] + args
    else:
        cmd = [str(COMPILER / 'bin' / 'ppu-lv2-objdump')] + args
    return subprocess.run(cmd, capture_output=True, text=True, env=dict(os.environ, MSYS_NO_PATHCONV='1')).stdout


def instructions(text):
    out = []
    for line in text.splitlines():
        m = re.match(r'\s*[0-9a-f]+:\s+((?:[0-9a-f]{2} ){4})\s*(.*)', line)
        if m:
            out.append((m.group(1).replace(' ', ''), re.sub(r'\s+', ' ', m.group(2)).strip()))
        elif re.match(r'\s+[0-9a-f]+: R_PPC64_', line) and out:
            out[-1] = (out[-1][0], out[-1][1] + '  <' + line.split()[1].replace('R_PPC64_', '') + ' ' + line.split()[-1] + '>')
    return out


def main():
    address = int(sys.argv[1], 16)
    size = load_functions()[address]
    orig = instructions(objdump(['-D', f'--start-address={address:#x}', f'--stop-address={address + size:#x}',
                                 'orig/EBOOT.elf']))
    names = {v: k for k, v in load_symbols().items()}
    symbol = names.get(address, f'.fn_{address:08x}')
    ours = None
    # Find the object that defines the symbol by its symbol table (one objdump, not thousands).
    candidates = [obj for obj in BUILD.glob('obj/**/*.o')
                  if any(f[0] == symbol for f in Object(obj).functions())]
    for obj in candidates:
        text = objdump(['-dr', obj.relative_to(ROOT).as_posix()])
        m = re.search(rf'^[0-9a-f]+ <{re.escape(symbol)}>:\n(.*?)(?:\n\n|\Z)', text, re.S | re.M)
        if m:
            ours = instructions(m.group(1))
            print(f'{symbol} in {obj.relative_to(ROOT)}')
            break
    if ours is None:
        raise SystemExit(f'{symbol} not found in build/obj (run tools/check.py first)')
    for i in range(max(len(orig), len(ours))):
        a = orig[i] if i < len(orig) else ('', '')
        b = ours[i] if i < len(ours) else ('', '')
        mark = ' ' if a[0] == b[0] else ('~' if a[0][:2] == b[0][:2] else '!')
        print(f'{mark} {a[0]:8} {a[1][:44]:44} | {b[0]:8} {b[1]}')


if __name__ == '__main__':
    main()
