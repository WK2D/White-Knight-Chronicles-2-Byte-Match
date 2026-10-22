"""Draw progress/map.svg from progress/report.json: one rectangle per unit,
sized by its code, filling in from the bottom as its functions match
(green: matched, blue: partly matched, grey: not started).

    python tools/progress_map.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT

W, H, TOP = 960, 540, 64
BG, EMPTY, BLUE, GREEN, EDGE, TEXT, MUTED = '#0d1117', '#30363d', '#3b82f6', '#22c55e', '#0d1117', '#e6edf3', '#8b949e'


def squarify(values, x, y, w, h):
    """Squarified treemap (Bruls, Huizing and van Wijk): [(x, y, w, h)] per value, in order."""
    out = [None] * len(values)
    order = sorted(range(len(values)), key=lambda i: -values[i])
    total = sum(values)
    scale = w * h / total if total else 0
    areas = [(i, values[i] * scale) for i in order]

    def worst(row, side):
        s = sum(a for _, a in row)
        return max(max(side * side * a / (s * s), (s * s) / (side * side * a)) for _, a in row)

    while areas:
        side = min(w, h)
        row = [areas.pop(0)]
        while areas and worst(row + [areas[0]], side) <= worst(row, side):
            row.append(areas.pop(0))
        s = sum(a for _, a in row)
        if w >= h:   # lay the row down the left edge
            cw = s / h if h else 0
            cy = y
            for i, a in row:
                ch = a / cw if cw else 0
                out[i] = (x, cy, cw, ch)
                cy += ch
            x, w = x + cw, w - cw
        else:        # along the top edge
            rh = s / w if w else 0
            cx = x
            for i, a in row:
                cw2 = a / rh if rh else 0
                out[i] = (cx, y, cw2, rh)
                cx += cw2
            y, h = y + rh, h - rh
    return out


def main():
    report = json.loads((ROOT / 'progress' / 'report.json').read_text())
    units = report['units']
    sizes = [int(u['measures']['totalCode']) for u in units]
    rects = squarify(sizes, 0, TOP, W, H - TOP)
    m = report['measures']
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
             f'font-family="Segoe UI, Helvetica, Arial, sans-serif">',
             f'<rect width="{W}" height="{H}" fill="{BG}"/>',
             f'<text x="12" y="28" fill="{TEXT}" font-size="20" font-weight="600">'
             f'White Knight Chronicles II: {m["matchedCodePercent"]:.3f}% of code matched</text>',
             f'<text x="12" y="50" fill="{MUTED}" font-size="13">{m["matchedFunctions"]:,} of '
             f'{m["totalFunctions"]:,} functions compile to identical bytes · {len(units)} units · '
             f'each square fills in as its functions match</text>']
    for u, (x, y, w, h) in zip(units, rects):
        total = int(u['measures']['totalCode']) or 1
        matched = int(u['measures']['matchedCode']) / total
        fuzzy = max(0.0, u['measures']['fuzzyMatchPercent'] / 100.0 - matched)
        parts.append(f'<g><title>{u["name"]}: {100 * matched:.2f}% matched, '
                     f'{u["measures"]["fuzzyMatchPercent"]:.2f}% fuzzy, {total:,} bytes</title>')
        parts.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h:.2f}" fill="{EMPTY}" '
                     f'stroke="{EDGE}" stroke-width="1"/>')
        gh, bh = h * matched, h * fuzzy
        if bh > 0:
            parts.append(f'<rect x="{x:.2f}" y="{y + h - gh - bh:.2f}" width="{w:.2f}" height="{bh:.2f}" '
                         f'fill="{BLUE}" stroke="{EDGE}" stroke-width="1"/>')
        if gh > 0:
            parts.append(f'<rect x="{x:.2f}" y="{y + h - gh:.2f}" width="{w:.2f}" height="{gh:.2f}" '
                         f'fill="{GREEN}" stroke="{EDGE}" stroke-width="1"/>')
        parts.append('</g>')
    parts.append('</svg>')
    (ROOT / 'progress' / 'map.svg').write_text('\n'.join(parts) + '\n', newline='\n')
    print('wrote progress/map.svg')


if __name__ == '__main__':
    main()
