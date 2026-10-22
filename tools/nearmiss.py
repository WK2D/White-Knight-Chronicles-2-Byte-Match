"""Classify lifted functions that compile but do not match: the first
differing instruction (original vs ours) and the size difference."""
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, 'tools')
from check import reloc_masks
from common import ROOT, Object, Original, load_functions

NOP, TOC_RESTORE = 0x60000000, 0xE8410028
LIMIT = int(sys.argv[2]) if len(sys.argv) > 2 else 10**9


def kind(x):
    op = x >> 26
    if op == 31:
        return f'31/{(x >> 1) & 0x3FF}'
    if op in (19, 30, 59, 63):
        return f'{op}/{(x >> 1) & 0x3FF if op in (19, 63) else (x >> 1) & 0x1F if op == 59 else (x >> 2) & 7}'
    return str(op)


NAMES = {'14': 'addi', '15': 'addis', '32': 'lwz', '34': 'lbz', '40': 'lhz', '42': 'lha', '36': 'stw', '38': 'stb',
         '44': 'sth', '48': 'lfs', '50': 'lfd', '52': 'stfs', '54': 'stfd', '58': 'ld', '62': 'std', '18': 'b/bl',
         '16': 'bc', '19/16': 'bclr', '31/444': 'or/mr', '11': 'cmpi', '10': 'cmpli', '31/0': 'cmp', '31/32': 'cmpl',
         '21': 'rlwinm', '30/0': 'rldicl', '31/986': 'extsw', '24': 'ori', '31/266': 'add', '31/40': 'subf',
         '31/339': 'mfspr', '31/467': 'mtspr', '7': 'mulli', '31/235': 'mullw', '59/21': 'fadds', '63/72': 'fmr',
         '28': 'andi.', '31/954': 'extsb', '31/922': 'extsh', '31/104': 'neg', '63/12': 'frsp'}


def name(k):
    return NAMES.get(k, k)


def main():
    log = Path(sys.argv[1]).read_text(encoding='utf-8', errors='replace')
    miss = {int(m.group(1), 16): float(m.group(2))
            for m in re.finditer(r'^([0-9a-f]{8}) +([\d.]+)% (?!MATCH)', log, re.M)}
    orig, functions = Original(), load_functions()
    pairs, sizes, examples = Counter(), Counter(), {}
    for obj_path in (ROOT / 'build' / 'obj' / 'src' / 'lift').glob('*.o'):
        obj = Object(obj_path)
        for fname, section, start, size in obj.functions():
            m = re.fullmatch(r'\.fn_([0-9a-f]{8})', fname)
            if not m or int(m.group(1), 16) not in miss:
                continue
            a = int(m.group(1), 16)
            if functions[a] > 4 * LIMIT:
                continue
            ours = obj.section_bytes(section)[start:start + size]
            theirs = orig.read(a, functions[a])
            masks = reloc_masks(obj, section, start, size)
            n = min(len(ours), len(theirs)) // 4
            first = None
            for i in range(n):
                x = int.from_bytes(theirs[4 * i:4 * i + 4], 'big')
                y = int.from_bytes(ours[4 * i:4 * i + 4], 'big')
                mask = masks.get(4 * i, (0, None))[0]
                if (x & ~mask) == (y & ~mask) or (y == NOP and x == TOC_RESTORE):
                    continue
                first = (name(kind(x)), name(kind(y)))
                break
            d = (len(ours) - len(theirs)) // 4
            sizes['same size' if d == 0 else ('ours longer' if d > 0 else 'ours shorter')] += 1
            key = first or ('(prefix equal)', 'length differs')
            pairs[key] += 1
            examples.setdefault(key, a)
    print(sizes)
    for k, c in pairs.most_common(40):
        print(f'{c:6d}  {k[0]:>10} -> {k[1]:<10} e.g. {examples[k]:08x}')


if __name__ == '__main__':
    main()
