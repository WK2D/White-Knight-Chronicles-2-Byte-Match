"""Count functions by instruction shape, to see what can be matched in bulk.

    python tools/shapes.py [top N]
"""
import struct
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Original, load_functions


def shape(word):
    """Opcode-level fingerprint of one instruction (registers kept, immediates dropped)."""
    op = word >> 26
    if op in (14, 15, 24, 25, 26, 27, 28, 29, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45,
              46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 10, 11):
        return f'{op}:{(word >> 21) & 31}:{(word >> 16) & 31}'
    if op == 58 or op == 62:
        return f'{op}.{word & 3}:{(word >> 21) & 31}:{(word >> 16) & 31}'
    return f'{word:08x}'


def main():
    top = int(sys.argv[1]) if len(sys.argv) > 1 else 25
    orig, functions = Original(), load_functions()
    counts, sizes, example = Counter(), Counter(), {}
    for a, s in functions.items():
        if s > 24:
            continue
        words = struct.unpack(f'>{s // 4}I', orig.read(a, s))
        key = ' '.join(shape(w) for w in words)
        counts[key] += 1
        sizes[s] += 1
        example.setdefault(key, a)
    print('functions of 4..24 bytes:', sum(counts.values()), dict(sorted(sizes.items())))
    for key, n in counts.most_common(top):
        print(f'{n:6d}  {example[key]:08x}  {key}')


if __name__ == '__main__':
    main()
