"""Write C for straight-line functions (no branches but the final return,
no indirect calls) by running them symbolically, into src/lift/<unit>.cpp.

Each register holds a C expression. Loads, stores, simple arithmetic and
direct calls become C; types come from how values are used (a value used
as a base address is a pointer, extsw marks an int, and so on). A callee's
argument count comes from the callee's own code: the argument registers it
reads before writing them. Each function is lifted twice, the second time
with the types the first pass found.

Nothing is trusted: check.py compares every function, and --prune drops
the ones that do not match.

    python tools/lift.py
    python tools/check.py -v > build/check.log
    python tools/lift.py --prune build/check.log
"""
import bisect
import re
import struct
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import BUILD, ROOT, Original, load_functions, load_units

OUT = ROOT / 'src' / 'lift'
LOADS = {32: 'u32', 34: 'u8', 40: 'u16', 42: 's16', 48: 'float', 50: 'double'}
STORES = {36: 'u32', 38: 'u8', 44: 'u16', 52: 'float', 54: 'double'}
CTYPES = {'ptr': 'void*', 'addr': 'void*', 'gaddr': 'void*', 'u32': 'u32', 'u8': 'u8', 'u16': 'u16',
          's16': 's16', 'u64': 'u64', 'int': 'int', 'float': 'float', 'double': 'double'}
BLR, NOP, TOC_RESTORE = 0x4E800020, 0x60000000, 0xE8410028
MAX_WORDS = 128


class Fail(Exception):
    pass


def s16(v):
    return v - 0x10000 if v & 0x8000 else v


def branch_target(address, word):
    off = word & 0x03FFFFFC
    if off & 0x02000000:
        off -= 0x04000000
    return address + off


class Value:
    def __init__(self, text, ctype='int', prec=0, call=None, origin=None, vt=None):
        self.text, self.ctype, self.prec, self.call = text, ctype, prec, call
        self.origin = origin       # for 'x + n': x, which is a pointer if the sum is used as one
        # Virtual dispatch: ('vptr', obj) the word at offset 0 of obj, ('slot', obj, k)
        # vtable entry k (a function descriptor), ('code', obj, k) its code address.
        self.vt = vt


def paren(v):
    return v.text if v.prec == 0 else f'({v.text})'


SIZES = {'u8': 1, 'u16': 2, 's16': 2, 'u32': 4, 'float': 4, 'void*': 4, 'double': 8, 'u64': 8}


def struct_decl(tag, by_offset, ptrs=None):
    """struct tag { fields at their offsets }, or None if they overlap, are
    misaligned or lie before the start (negative offsets). ptrs: {offset:
    struct tag} for word fields that point to a known struct."""
    ptrs = ptrs or {}
    out, at = [f'struct {tag} {{'], 0
    for off in sorted(by_offset):
        t = by_offset[off]
        if off < at or off % SIZES[t]:
            return None
        if off > at:
            out.append(f'    char pad_{at:x}[{off - at}];')
        out.append(f'    {ptrs[off] + "*" if off in ptrs else t} f_{off:x};')
        at = off + SIZES[t]
    out.append('};')
    return '\n'.join(out)


def class_decl(tag, slots, fields, ptrs=None):
    """A class with virtual functions vf0..vfN (the used slots with their
    signatures, the rest as fillers) and its fields after the vtable pointer."""
    ptrs = ptrs or {}
    out = [f'struct {tag} {{']
    for k in range(max(slots) + 1):
        if k in slots:
            rtype, types = slots[k]
            out.append(f'    virtual {rtype} vf{k}({", ".join(types) if types else "void"});')
        else:
            out.append(f'    virtual void vf{k}();')
    at = 4
    for off in sorted(fields):
        t = fields[off]
        if off < at or off % SIZES[t]:
            return None
        if off > at:
            out.append(f'    char pad_{at:x}[{off - at}];')
        out.append(f'    {ptrs[off] + "*" if off in ptrs else t} f_{off:x};')
        at = off + SIZES[t]
    out.append('};')
    return '\n'.join(out)


def negate(cond):
    flip = {' < ': ' >= ', ' >= ': ' < ', ' > ': ' <= ', ' <= ': ' > ', ' == ': ' != ', ' != ': ' == '}
    for a, b in flip.items():
        if a in cond:
            return cond.replace(a, b, 1)
    return f'!({cond})'


def reads_before_writes(words):
    """(integer argument count, float argument count) a function reads on entry."""
    written, fwritten, used, fused = set(), set(), set(), set()
    for w in words:
        op = w >> 26
        rt, ra, rb = (w >> 21) & 31, (w >> 16) & 31, (w >> 11) & 31
        srcs, dst, fsrcs, fdst = [], None, [], None
        if op in (14, 15):
            srcs, dst = ([ra] if ra else []), rt
        elif op in (24, 25, 28, 29):
            srcs, dst = [rt], ra
        elif op in (32, 34, 40, 42, 58):
            srcs, dst = [ra], rt
        elif op in (48, 50):
            srcs, fdst = [ra], rt
        elif op in (36, 38, 44, 62):
            srcs = [rt, ra]
        elif op in (52, 54):
            srcs, fsrcs = [ra], [rt]
        elif op == 31:
            xo = (w >> 1) & 0x3FF
            if xo in (444, 28, 316, 124, 24, 536, 792):        # or and xor nor slw srw sraw: rs, rb -> ra
                srcs, dst = [rt, rb], ra
            elif xo in (986, 922, 954, 26, 824):               # extsw extsh extsb cntlzw srawi
                srcs, dst = [rt], ra
            elif xo in (266, 40, 235, 491, 459, 10, 8, 138, 136):
                srcs, dst = [ra, rb], rt
            elif xo == 104:
                srcs, dst = [ra], rt
            else:
                break
        elif op in (21, 30):
            srcs, dst = [rt], ra
        elif op == 7:
            srcs, dst = [ra], rt
        elif op in (59, 63):
            fsrcs, fdst = [ra, rb, (w >> 6) & 31], rt
        else:
            break                                  # a branch or anything unknown ends the scan
        for r in srcs:
            if 3 <= r <= 10 and r not in written:
                used.add(r)
        for r in fsrcs:
            if 1 <= r <= 13 and r not in fwritten:
                fused.add(r)
        if dst is not None:
            written.add(dst)
        if fdst is not None:
            fwritten.add(fdst)
    return (max(used) - 2 if used else 0), (max(fused) if fused else 0)


class Lifter:
    def __init__(self, address, words, arity, preset=None, layouts=None, poly=None):
        self.address, self.words, self.arity = address, words, arity
        self.preset = preset or {}
        self.layouts = layouts or {}      # access path -> {offset: type} (second pass)
        self.poly = poly or set()         # argument registers known (earlier pass) to be polymorphic
        # Offset-0 words are read as vtable pointers only where the function
        # makes an indirect call; elsewhere they are ordinary fields.
        self.virtual = 0x4E800421 in words
        self.access = defaultdict(set)    # access path -> {(offset, type)} (first pass)
        self.static = {}                  # callees called without a TOC slot: static in this file
        self.gpr = {r: None for r in range(32)}
        self.fpr = {r: None for r in range(32)}
        self.args, self.fargs = {}, {}
        self.stmts, self.calls, self.globals = [], {}, {}
        self.fresh = set()
        self.cr = {}                      # condition field -> (lhs, rhs, 'signed'|'unsigned'|'pointer')
        self.ctr = None                   # value moved to CTR (mtctr)
        self.ffresh = set()               # float argument registers written since the last call
        self.loops = {}                   # loop header index -> latch index (the last back edge)
        self.in_loop = []                 # headers of the loops being lifted
        self.loop_vars = []               # per loop: register -> its variable
        self.loop_exits = []              # per loop: how many break/continue/return statements
        self.latch_cond = None
        self.loop_broke = False
        self.while_entry = {}             # test block -> header, for loops entered by a jump to their test
        self.return_exits = set()
        self.back_returns = set()         # targets of backward branches that are not loops
        self.active_paths = set()         # return paths being lifted (guards against cycles)
        self.made_call = False            # a call has clobbered r3-r12 somewhere before
        self.slots = {}                   # stack offset -> (kind, value): int/float conversions go through memory         # blocks outside a loop that only lead to a return
        self.polymorphic = set()          # argument registers whose offset-0 word is a vptr
        self.vclasses = {}                # class tag -> {slot: (rtype, [types])}
        self.vars = []                    # merge variables: (type, name)
        self.returns = []                 # one dict per return statement
        self.ended = False                # the current path has returned

    def tag(self, path):
        """Struct name for an access path."""
        parts = [f'a{path[0] - 3}' if path[0] != 'g' else (f'g{path[1]:x}' if path[1] >= 0 else f'gm{-path[1]:x}')]
        parts += [f'{d:x}' if d >= 0 else f'm{-d:x}' for d in path[1 if path[0] != 'g' else 2:]]
        return f'unk_{self.address:08x}_' + '_'.join(parts)

    def pointer_fields(self, path):
        """{offset: tag} for the word fields of path that point to structs."""
        return {d: self.tag(path + (d,)) for d, t in self.layouts.get(path, {}).items()
                if t == 'u32' and path + (d,) in self.layouts}

    def param(self, k):
        for j in range(3, k + 1):
            if j not in self.args:
                v = Value(f'a{j - 3}', self.preset.get(j, 'int'))
                v.path = (j,)
                self.args[j] = v
                if self.gpr[j] is None:
                    self.gpr[j] = v

    def reg(self, r):
        v = self.gpr[r]
        if v is None:
            if 3 <= r <= 10 and r not in self.args:
                self.param(r)
                return self.gpr[r]
            if r == 2:
                v = Value('TOC', 'toc')
                v.toc_off = 0
                return v
            raise Fail(f'r{r} read before it is set')
        if v.call is not None:
            v.call['used'] = True
        return v

    def freg(self, r):
        v = self.fpr[r]
        if v is None:
            if 1 <= r <= 13 and r not in self.fargs:
                for j in range(1, r + 1):
                    if j not in self.fargs:
                        self.fargs[j] = Value(f'f{j - 1}', 'float')
                        if self.fpr[j] is None:
                            self.fpr[j] = self.fargs[j]
                return self.fpr[r]
            raise Fail(f'f{r} read before it is set')
        if v.call is not None:
            v.call['used'] = True
        return v

    def set(self, r, v):
        self.gpr[r] = v
        if 3 <= r <= 10:
            self.fresh.add(r)

    def pointer(self, v):
        if v.ctype == 'toc':
            raise Fail('TOC as a base')
        if v.ctype not in ('ptr', 'addr', 'gaddr'):
            v.ctype = 'ptr'
            if v.call is not None:
                v.call['type'] = 'void*'
        if v.origin is not None:
            self.pointer(v.origin)
        return v

    def address_of_global(self, d):
        if d not in self.globals:
            self.globals[d] = f'g_{self.address:08x}_{len(self.globals)}'
        typed = ('g', d) in self.layouts
        v = Value(f'(&{self.globals[d]})' if typed else self.globals[d], 'gaddr')
        v.path = ('g', d)
        return v

    def field(self, ctype, base, d):
        if base.ctype == 'toc':
            if ctype != 'u32':
                raise Fail('non-word TOC load')
            return self.address_of_global(d + getattr(base, 'toc_off', 0))
        if self.virtual and base.vt is not None and base.vt[0] == 'vptr' and ctype == 'u32' and d % 4 == 0 and d >= 0:
            obj = base.vt[1]
            for k, arg in self.args.items():
                if obj is arg:
                    self.polymorphic.add(k)
                    self.access[(k,)].discard((0, 'u32'))
            return Value(f'/*vtable slot {d // 4}*/', 'u32', vt=('slot', obj, d // 4))
        if base.vt is not None and base.vt[0] == 'slot' and ctype == 'u32' and d in (0, 4):
            return Value('/*virtual*/', 'u32', vt=('code' if d == 0 else 'toc',) + base.vt[1:])
        self.pointer(base)
        path = getattr(base, 'path', None)
        if path is not None and not (d == 0 and ctype == 'u32' and (self.virtual or len(path) == 1 and path[0] in self.poly)):
            self.access[path].add((d, ctype))
            if self.layouts.get(path, {}).get(d) == ctype:
                v = Value(f'{base.text}->f_{d:x}' if d >= 0 else f'{base.text}->f_m{-d:x}', ctype)
                if ctype == 'u32':
                    v.path = path + (d,)
                    if v.path in self.layouts:
                        v.ctype = 'ptr'
                        v.cast = self.tag(v.path) + '*'   # stores need this type
                return v
            if ctype == 'u32':
                v = Value(f'FIELD({ctype}, {base.text}, {d})', ctype)
                v.path = path + (d,)
                return v
        if getattr(base, 'ivbase', None) is not None:                # element of an array: base, offset + i * stride
            p_, i_ = base.ivbase
            return Value(f'FIELD({ctype}, (char*){paren(p_)}, {d} + {i_.text})', ctype)
        if getattr(base, 'iv', None) is not None:
            raise Fail('induction variable as an address')
        for k, arg in self.args.items():
            if base is arg:
                if d == 0 and ctype == 'u32' and k in self.poly:
                    return Value(f'FIELD(u32, {base.text}, 0)', 'u32', vt=('vptr', base))
        if self.virtual and d == 0 and ctype == 'u32':
            return Value(f'FIELD({ctype}, {base.text}, {d})', ctype, vt=('vptr', base))
        return Value(f'FIELD({ctype}, {base.text}, {d})', ctype)

    def run(self):
        """Lift the function's forward-only control flow into nested ifs."""
        w = self.words
        n = len(w)
        leaders = {0}
        back = []                                          # backward branches: (source, target)
        for i, x in enumerate(w):
            op = x >> 26
            if op == 16:                                   # bc
                t = i + (s16(x & 0xFFFC) >> 2)
                if x & 3 or t > n or t < 0:
                    raise Fail('absolute, linked or outside conditional branch')
                if t <= i:                                 # a back edge to the header t
                    back.append((i, t))
                leaders |= {t, i + 1}
            elif op == 18 and not x & 1:                   # b (not bl)
                t = i + ((branch_target(0, x)) >> 2)
                if x & 2 or t > n or t < 0:
                    raise Fail('outside branch')
                if t <= i:
                    back.append((i, t))
                leaders |= {t, i + 1}
            elif op == 19:
                xo = (x >> 1) & 0x3FF
                if x == 0x4E800421:                       # bctrl: a call, handled by step()
                    continue
                if xo != 16 or x & 1:
                    raise Fail('indirect branch')
                leaders.add(i + 1)
        self.leaders = sorted(l for l in leaders if l < n)
        # A backward branch is a loop only if its target leads back to it.
        # Otherwise GCC placed a shared tail (often the epilogue) earlier and
        # jumps back to it: lift that as the path it is.
        for i, t in back:
            if self.block_start(i) in self.reach_all(t):
                self.loops[t] = max(self.loops.get(t, i), i)
            else:
                self.back_returns.add(t)
        for h, l in self.loops.items():
            latch_block = self.leaders[bisect.bisect_right(self.leaders, l) - 1]
            for j in range(h, l + 1):                      # exits: the block after the loop, or a return path
                x = w[j]
                t = None
                if x >> 26 == 16:
                    t = j + (s16(x & 0xFFFC) >> 2)
                elif x >> 26 == 18 and not x & 1:
                    t = j + (branch_target(0, x) >> 2)
                if t is None or h <= t <= l + 1 or t in self.back_returns:
                    continue
                if t > l + 1 and self.only_returns(t) and not any(
                        h2 <= x_ <= l2 for x_ in self.successors(t) for h2, l2 in self.loops.items()):
                    self.return_exits.add(t)
                    continue
                raise Fail('branch out of a loop body')
            for j in range(0, h):                          # the way in: the header, or a jump to the test
                x = w[j]
                t = None
                if x >> 26 == 16:
                    t = j + (s16(x & 0xFFFC) >> 2)
                elif x >> 26 == 18 and not x & 1:
                    t = j + (branch_target(0, x) >> 2)
                if t is None or not h < t <= l or t in self.back_returns:
                    continue
                if x >> 26 == 18 and j == h - 1 and t == latch_block and w[l] >> 26 == 16:
                    self.while_entry[t] = h                # while (cond) { body }: enter at the test
                    continue
                raise Fail('jump into a loop body')
        self.region(0, None)
        if not self.ended:
            raise Fail('falls off the end')
        return self

    def return_path(self, b):
        """Lift a path from inside a loop that ends in a return, as plain
        forward code (outside the loop's context)."""
        if b in self.active_paths:
            raise Fail('return path loops back on itself')
        self.active_paths.add(b)
        saved = (self.in_loop, self.loop_vars, self.loop_exits)
        self.in_loop, self.loop_vars, self.loop_exits = [], [], []
        try:
            self.region(b, None)
        finally:
            self.in_loop, self.loop_vars, self.loop_exits = saved
            self.active_paths.discard(b)
        if not self.ended:
            raise Fail('return path does not return')

    def block_start(self, i):
        return self.leaders[bisect.bisect_right(self.leaders, i) - 1]

    def reach_all(self, b):
        """Blocks reachable from block b, following backward branches too."""
        out, todo = set(), [b]
        while todo:
            x = todo.pop()
            if x in out or x >= len(self.words):
                continue
            out.add(x)
            last = self.block_end(x) - 1
            t = self.words[last]
            op = t >> 26
            if op == 16:
                todo += [last + (s16(t & 0xFFFC) >> 2), last + 1]
            elif op == 18 and not t & 1:
                todo.append(last + (branch_target(0, t) >> 2))
            elif op == 19 and t != 0x4E800421:
                if (t >> 21) & 0x14 != 0x14:
                    todo.append(last + 1)
            else:
                todo.append(last + 1)
        return out

    def only_returns(self, b):
        """Whether every path from block b ends in a return (no loop back)."""
        for x in self.successors(b):
            last = self.block_end(x) - 1
            t = self.words[last]
            if t >> 26 == 16 and s16(t & 0xFFFC) <= 0:
                return False
        return True

    def block_end(self, b):
        k = bisect.bisect_right(self.leaders, b)
        return self.leaders[k] if k < len(self.leaders) else len(self.words)

    def successors(self, b):
        """Blocks reachable from block b (forward only)."""
        out, todo = set(), [b]
        while todo:
            x = todo.pop()
            if x in out or x >= len(self.words):
                continue
            out.add(x)
            last = self.block_end(x) - 1
            t = self.words[last]
            op = t >> 26
            if op == 16:
                target = last + (s16(t & 0xFFFC) >> 2)
                todo += ([target] if target > last else []) + [last + 1]
            elif op == 18 and not t & 1:
                target = last + (branch_target(0, t) >> 2)
                if target > last:
                    todo.append(target)
            elif op == 19 and t != 0x4E800421:
                if (t >> 21) & 0x14 != 0x14:              # conditional return: may fall through
                    todo.append(last + 1)
            else:
                todo.append(last + 1)
        return out

    def snapshot(self):
        return (dict(self.gpr), dict(self.fpr), set(self.fresh), dict(self.cr), self.ended, set(self.ffresh))

    def restore(self, state):
        self.gpr, self.fpr, self.fresh, self.cr, self.ended = (dict(state[0]), dict(state[1]), set(state[2]),
                                                               dict(state[3]), state[4])
        self.ffresh = set(state[5])

    def condition(self, crf, bit, true):
        if crf not in self.cr:
            raise Fail('condition field never set')
        a, b, kind = self.cr[crf]
        ops = {(0, True): '<', (1, True): '>', (2, True): '==', (0, False): '>=', (1, False): '<=', (2, False): '!='}
        if (bit, true) not in ops:
            raise Fail('summary-overflow bit')
        bt = b.text if isinstance(b, Value) else b
        if kind == 'float':
            return f'{a.text} {ops[bit, true]} {bt}'
        if kind == 'unsigned':
            return f'(u32)({a.text}) {ops[bit, true]} (u32)({bt})'
        if kind == 'pointer':
            return f'{a.text} {ops[bit, true]} {bt}'
        return f'(int)({a.text}) {ops[bit, true]} (int)({bt})'

    def branch_condition(self, word):
        """C condition under which a bc/bclr is taken."""
        bo, bi = (word >> 21) & 31, (word >> 16) & 31
        if bo & 0x16 == 0x10:                           # bdnz: decrement CTR, loop while non-zero
            return 'BDNZ'

        if bo & 0x1E in (12, 14):
            true = True
        elif bo & 0x1E in (4, 6):
            true = False
        else:
            raise Fail(f'BO {bo}')
        return self.condition(bi >> 2, bi & 3, true)

    def emit_return(self):
        self.returns.append({'value': self.gpr[3], 'fresh': 3 in self.fresh, 'stmt': None})
        stmt = ['return', len(self.returns) - 1]
        self.stmts.append(stmt)
        self.ended = True

    def run_block(self, b):
        """Execute block b up to its terminator; return (kind, data)."""
        end = self.block_end(b)
        for i in range(b, end - 1):
            self.step(self.words[i], i)
        last = end - 1
        t = self.words[last]
        op = t >> 26
        if t == BLR:
            return 'ret', None
        if op == 16:
            return 'cond', (last + (s16(t & 0xFFFC) >> 2), last + 1, self.branch_condition(t))
        if op == 18 and not t & 1:
            return 'goto', last + (branch_target(0, t) >> 2)
        if op == 19:
            return 'condret', (last + 1, self.branch_condition(t))
        self.step(t, last)
        return 'fall', end

    def merge(self, states, joins_into):
        """Registers differing between paths that reach the same block get a variable."""
        live = [st for st in states if not st[0][4]]
        if not live:
            self.ended = True
            return
        if len(live) == 1:
            self.restore(live[0][0])
            return
        (a, sa), (b, sb) = live[0], live[1]
        merged = dict(a[0])
        fresh = a[2] | b[2]
        for r in range(32):
            va, vb = a[0].get(r), b[0].get(r)
            if va is vb:
                continue
            if va is None or vb is None or r in (1, 2):
                merged[r] = None
                continue
            ta, tb = CTYPES.get(va.ctype, 'int'), CTYPES.get(vb.ctype, 'int')
            ctype = va.ctype if ta == tb else 'int'
            name = f'v{len(self.vars)}'
            vtype = CTYPES.get(ctype, 'int')
            self.vars.append((vtype, name))
            for v in (va, vb):
                if v.call is not None:
                    v.call['used'] = True
            sa.append(f'{name} = ({vtype})({va.text});')
            sb.append(f'{name} = ({vtype})({vb.text});')
            merged[r] = Value(name, ctype)
        self.gpr, self.fpr, self.fresh, self.cr, self.ended = merged, dict(a[1]), fresh, {}, False

    def written_in(self, start, end):
        """Registers the instructions in [start, end) write (a call writes r0, r3-r12)."""
        return self.written_in_words(self.words[start:end])

    def written_in_words(self, words):
        out = set()
        for x in words:
            op = x >> 26
            rt, ra = (x >> 21) & 31, (x >> 16) & 31
            if op in (14, 15, 7, 32, 34, 40, 42, 58) or (op == 31 and (x >> 1) & 0x3FF in (266, 40, 235, 491, 459, 104)):
                out.add(rt)
            elif op in (24, 25, 28, 29, 21, 30) or (op == 31 and (x >> 1) & 0x3FF in (444, 28, 316, 124, 24, 536, 792, 986, 922, 954, 824)):
                out.add(ra)
            elif op in (33, 35, 41, 43, 37, 39, 45):        # update forms write the base too
                out |= {rt, ra}
            elif op == 18 and x & 1:
                out |= {0} | set(range(3, 13))
        out.discard(1)
        out.discard(2)
        return out

    def loop_updates(self):
        """Assignments that give the loop variables their current values."""
        out = []
        for r, var in self.loop_vars[-1].items():
            now = self.gpr[r]
            if now is not None and now is not var:
                out.append(f'{var.text} = ({CTYPES.get(var.ctype, "int")})({now.text});')
        return out

    def induction_registers(self, start, end):
        """{register: stride} for registers that [start, end) writes only
        with one addi r,r,stride."""
        writes = defaultdict(list)
        for x in self.words[start:end]:
            for r in self.written_in_word(x):
                writes[r].append(x)
        out = {}
        for r, xs in writes.items():
            x = xs[0]
            if len(xs) == 1 and x >> 26 == 14 and (x >> 21) & 31 == r and (x >> 16) & 31 == r and s16(x & 0xFFFF):
                out[r] = s16(x & 0xFFFF)
        return out

    def written_in_word(self, x):
        return self.written_in_words([x])

    def loop(self, header, test=None):
        """Lift the loop at header; return the block after it. With test, the
        loop is entered by a jump to its test block: while (cond) { body }."""
        latch = self.loops[header]
        exit_ = latch + 1
        last = self.words[latch]
        conditional = last >> 26 == 16
        counted = conditional and self.branch_condition(last) == 'BDNZ'
        written = self.written_in(header, exit_)
        # Counted loops become for (i = 0; i < n; i++). Registers that start
        # at 0 and only step by a constant are byte offsets i * stride:
        # written that way, GCC picks the original's induction variables.
        ivs = self.induction_registers(header, exit_) if counted and test is None and self.ctr is not None else {}
        ivs = {r: s for r, s in ivs.items() if self.gpr[r] is not None and self.gpr[r].text == '0'}
        if counted and test is None and self.ctr is not None:
            index = f'v{len(self.vars)}'
            self.vars.append(('int', index))
            trips = self.ctr.text
            self.ctr = None
            carried = {}
            for r in sorted(written):
                if r in ivs:
                    carried[r] = Value(f'(int)({index}) * {ivs[r]}', 'int', 1)
                    carried[r].iv = (index, ivs[r], 0)
                    self.gpr[r] = carried[r]
                    continue
                v = self.gpr[r]
                ctype = 'int' if v is None or v.ctype == 'call' else v.ctype
                name = f'v{len(self.vars)}'
                self.vars.append((CTYPES.get(ctype, 'int'), name))
                if v is not None:
                    self.stmts.append(f'{name} = ({CTYPES.get(ctype, "int")})({v.text});')
                    if v.call is not None:
                        v.call['used'] = True
                carried[r] = Value(name, ctype)
                self.gpr[r] = carried[r] if v is not None else None
            outer, body = self.stmts, []
            self.stmts = body
            self.in_loop.append(header)
            self.loop_vars.append({r: v for r, v in carried.items() if r not in ivs})
            self.loop_exits.append(0)
            self.latch_cond = None
            self.region(header, exit_)
            self.loop_exits.pop()
            if self.latch_cond is None:
                raise Fail('counted loop body never reaches its latch')
            for r, var in carried.items():
                now = self.gpr[r]
                if r in ivs:
                    if getattr(now, 'iv', None) != (index, ivs[r], 1):
                        raise Fail('induction variable not stepped once')
                elif now is not None and now is not var:
                    body.append(f'{var.text} = ({CTYPES.get(var.ctype, "int")})({now.text});')
            self.loop_vars.pop()
            self.in_loop.pop()
            self.stmts = outer
            self.stmts.append(['cfor', index, trips, body])
            for r in written:
                self.gpr[r] = carried.get(r)
            self.ended = False
            self.loop_broke = False
            return exit_
        carried = {}
        for r in sorted(written):
            v = self.gpr[r]
            ctype = 'int' if v is None or v.ctype == 'call' else v.ctype
            name = f'v{len(self.vars)}'
            self.vars.append((CTYPES.get(ctype, 'int'), name))
            if v is not None:
                self.stmts.append(f'{name} = ({CTYPES.get(ctype, "int")})({v.text});')
                if v.call is not None:
                    v.call['used'] = True
            carried[r] = Value(name, ctype)
            self.gpr[r] = carried[r] if v is not None else None
        counter = None
        if counted:
            if self.ctr is None:
                raise Fail('counted loop without mtctr')
            counter = f'v{len(self.vars)}'
            self.vars.append(('u32', counter))
            self.stmts.append(f'{counter} = (u32)({self.ctr.text});')
            self.ctr = None
        outer, body = self.stmts, []
        if test is not None:
            if counted:
                raise Fail('counted while loop')
            # The test block runs first, with the loop variables.
            self.stmts = []
            for i in range(test, latch):
                self.step(self.words[i], i)
            if self.stmts:
                raise Fail('loop test with side effects')
            cond = self.branch_condition(last)
            self.stmts = body
            self.in_loop.append(header)
            self.loop_vars.append(carried)
            self.loop_exits.append(0)
            self.while_test = test
            self.region(header, test)
            self.loop_exits.pop()
            if not self.ended:
                body += self.loop_updates()
            self.loop_vars.pop()
            self.in_loop.pop()
            self.stmts = outer
            self.stmts.append(['while', cond, body])
            for r in written:
                self.gpr[r] = carried.get(r)
            self.ended = False
            self.loop_broke = False
            return exit_
        self.stmts = body
        self.in_loop.append(header)
        self.loop_vars.append(carried)
        self.loop_exits.append(0)
        self.latch_cond = None
        self.region(header, exit_)
        jumps = self.loop_exits.pop()           # breaks, continues and returns inside the body
        reached_latch = self.latch_cond is not None
        cond = None
        if reached_latch:
            updates = {}
            for r, var in carried.items():
                now = self.gpr[r]
                if now is not None and now is not var:
                    body.append(f'{var.text} = ({CTYPES.get(var.ctype, "int")})({now.text});')
                    updates[id(now)] = var
            if counted:
                cond = f'--{counter} != 0'
            else:
                bi = (last >> 16) & 31
                if (bi >> 2) not in self.cr:
                    raise Fail('loop condition field never set')
                a, b_, kind = self.cr[bi >> 2]
                a = updates.get(id(a), a)
                b_ = updates.get(id(b_), b_) if isinstance(b_, Value) else b_
                self.cr[bi >> 2] = (a, b_, kind)
                cond = self.branch_condition(last)
        self.loop_vars.pop()
        self.in_loop.pop()
        self.stmts = outer
        if reached_latch and not jumps:
            self.stmts.append(['do', body, cond])
        else:
            if reached_latch:
                body.append(['if', negate(cond), ['break;'], []])
            self.stmts.append(['for', body])
        # After the loop: the loop variables; anything else the body wrote is unknown.
        for r in written:
            self.gpr[r] = carried.get(r)
        exits = reached_latch or self.loop_broke
        self.ended = not exits
        self.loop_broke = False
        return exit_

    def region(self, b, stop):
        """Lift blocks from b until stop (or a return), appending to self.stmts."""
        while b is not None and b != stop and b < len(self.words):
            if b in self.loops and b not in self.in_loop:
                b = self.loop(b)
                continue
            kind, data = self.run_block(b)
            if kind == 'goto' and data in self.while_entry and self.while_entry[data] == self.block_end(b) \
                    and self.while_entry[data] not in self.in_loop:
                b = self.loop(self.while_entry[data], test=data)
                continue
            header = self.in_loop[-1] if self.in_loop else None
            exit_ = self.loops[header] + 1 if header is not None else None
            if kind == 'goto' and data in self.back_returns and data <= b:
                if header is not None:
                    self.loop_exits[-1] += 1
                self.return_path(data)                 # a shared tail placed earlier
                return
            if kind == 'cond' and data[0] in self.back_returns and data[0] <= b:
                taken, fall, cond = data
                state = self.snapshot()
                outer, inner = self.stmts, []
                self.stmts = inner
                self.return_path(taken)
                self.stmts = outer
                self.restore(state)
                self.stmts.append(['if', cond, inner, []])
                if header is not None:
                    self.loop_exits[-1] += 1
                b = fall
                continue
            if kind == 'goto' and header is not None and data in self.return_exits:
                self.loop_exits[-1] += 1
                self.return_path(data)                 # lifts through to its return
                return
            if kind == 'cond' and header is not None and data[0] in self.return_exits:
                taken, fall, cond = data
                state = self.snapshot()
                outer, inner = self.stmts, []
                self.stmts = inner
                self.return_path(taken)
                self.stmts = outer
                self.restore(state)
                self.stmts.append(['if', cond, inner, []])
                self.loop_exits[-1] += 1
                b = fall
                continue
            if kind == 'ret':
                if header is not None:
                    self.loop_exits[-1] += 1
                self.emit_return()
                return
            if kind == 'goto' and header is not None and data in (header, exit_):
                block_last = self.block_end(b) - 1
                if data == header and block_last == self.loops[header]:
                    self.latch_cond = 'always'         # the unconditional latch: end of the body
                    self.stmts += self.loop_updates()
                    self.ended = True
                    return
                self.stmts += self.loop_updates()
                self.stmts.append('continue;' if data == header else 'break;')
                self.loop_exits[-1] += 1
                if data == exit_:
                    self.loop_broke = True
                self.ended = True
                return
            if kind in ('goto', 'fall'):
                b = data
                continue
            if kind == 'condret':
                after, cond = data
                outer = self.stmts
                body = []
                self.stmts = body
                state = self.snapshot()
                self.emit_return()
                self.restore(state)
                self.stmts = outer
                self.stmts.append(['if', cond, body, []])
                b = after
                continue
            taken, fall, cond = data
            if header is not None and taken == header:
                if self.block_end(b) - 1 == self.loops[header]:
                    self.latch_cond = cond             # the loop's own latch: end of the body
                    return
                self.stmts.append(['if', cond, self.loop_updates() + ['continue;'], []])
                self.loop_exits[-1] += 1
                b = fall
                continue
            if header is not None and taken == exit_:
                self.stmts.append(['if', cond, self.loop_updates() + ['break;'], []])
                self.loop_exits[-1] += 1
                self.loop_broke = True
                b = fall
                continue
            if cond == 'BDNZ':
                raise Fail('bdnz that is not a latch')
            reach_t, reach_f = self.successors(taken), self.successors(fall)
            common = sorted(reach_t & reach_f)
            join = common[0] if common else None
            if stop is not None and join is not None and join > stop:
                join = stop
            if stop is not None and join is None and self.in_loop:
                join = None
            start = self.snapshot()
            outer = self.stmts
            then_body = []
            self.stmts = then_body
            self.region(fall, join)
            after_then = self.snapshot()
            self.restore(start)
            else_body = []
            if join is not None and taken == join:
                after_else = start
            elif join is None:
                after_else = None             # the taken path continues outside the if
            else:
                self.stmts = else_body
                self.region(taken, join)
                after_else = self.snapshot()
            self.stmts = outer
            self.stmts.append(['if', negate(cond), then_body, else_body])
            if join is None:
                if not after_then[4]:
                    raise Fail('if without a join does not return')
                self.restore(start)
                b = taken
                continue
            self.merge([(after_then, then_body), (after_else, else_body)], join)
            if self.ended:
                return
            b = join

    def step(self, w, i):
        op = w >> 26
        rt, ra, rb = (w >> 21) & 31, (w >> 16) & 31, (w >> 11) & 31
        d = s16(w & 0xFFFF)
        if op in (10, 11):                                           # cmpli / cmpi
            l64 = (w >> 21) & 1
            crf = (w >> 23) & 7
            v = self.reg(ra)
            if op == 10:
                self.cr[crf] = (v, str(w & 0xFFFF), 'pointer' if l64 else 'unsigned')
            else:
                self.cr[crf] = (v, str(d), 'pointer' if l64 else 'signed')
            if l64:
                self.pointer(v)               # a 64-bit compare is a pointer test
            return
        if op == 31 and ((w >> 1) & 0x3FF) in (0, 32):               # cmp / cmpl
            l64 = (w >> 21) & 1
            crf = (w >> 23) & 7
            kind = 'pointer' if l64 else ('signed' if ((w >> 1) & 0x3FF) == 0 else 'unsigned')
            self.cr[crf] = (self.reg(ra), self.reg(rb), kind)
            return
        if op == 15 and ra != 0:                                     # addis rD,rA,imm
            base = self.reg(ra)
            if base.ctype == 'toc':
                v = Value('TOC', 'toc')
                v.toc_off = getattr(base, 'toc_off', 0) + (d << 16)
                self.set(rt, v)
                return
            self.set(rt, Value(f'(int)({base.text}) + {d << 16}', 'int', 1))
            return
        if op in (24, 25, 26, 27) and w != NOP:                      # ori oris xori xoris
            v = self.reg(rt)
            imm = (w & 0xFFFF) << (16 if op in (25, 27) else 0)
            sym = '|' if op in (24, 25) else '^'
            if op in (24, 25) and re.fullmatch(r'-?\d+', v.text):     # lis/li then ori: a constant
                self.set(ra, Value(str((int(v.text) | imm) & 0xFFFFFFFF), 'u32'))
                return
            self.set(ra, Value(f'(u32)({v.text}) {sym} {imm:#x}', 'u32', 1))
            return
        if op == 7:                                                  # mulli
            self.set(rt, Value(f'(int)({self.reg(ra).text}) * {d}', 'int', 1))
            return
        if op == 21:                                                 # rlwinm
            self.rlwinm(w)
            return
        if op in (59, 63):                                           # floating point
            self.float_op(w)
            return
        if op == 28:                                                 # andi.
            v = self.reg(rt)
            res = Value(f'{paren(v)} & {w & 0xFFFF}', 'int', 1)
            self.set(ra, res)
            self.cr[0] = (res, '0', 'signed')
            return
        if w == 0x7C0802A6:                                          # mflr r0
            self.gpr[0] = Value('/*return address*/', 'lr')
            return
        if w in (NOP, TOC_RESTORE, 0x7C0803A6):                      # nop, TOC restore, mtlr r0
            return
        if op == 62 and ra == 1:                                     # stdu r1 / std rX,d(r1)
            if w & 3 == 1 and rt == 1:
                return
            if w & 3 == 0 and (rt in (0, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)) and self.gpr[rt] is not None                     and self.gpr[rt].ctype != 'lr':
                self.slots[d] = ('int64', self.reg(rt))              # an integer on its way to fcfid
                return
            if w & 3 == 0 and (rt == 0 or rt == 2 or rt >= 14):  # r2: TOC saved around an indirect call
                return
            raise Fail('spill')
        if op == 58 and w & 3 == 0 and ra == 1:                      # ld rX,d(r1)
            if self.slots.get(d, (None,))[0] == 'fctid' and (rt == 0 or 3 <= rt <= 12):
                f = self.slots[d][1]
                v = Value(f'(u32)({f.text})', 'u32')                 # fctidz + clrldi: float to unsigned
                v.conv = True
                self.set(rt, v)
                return
            if rt == 0 or rt >= 14:
                return
            raise Fail('stack load')
        if op == 54 and ra == 1:                                     # stfd fX,d(r1)
            v = self.freg(rt)
            if getattr(v, 'f2i', None) is None:
                raise Fail('float spill')
            self.slots[d] = v.f2i
            return
        if op == 50 and ra == 1:                                     # lfd fX,d(r1)
            if self.slots.get(d, (None,))[0] != 'int64':
                raise Fail('float stack load')
            v = Value(self.slots[d][1].text, 'bits')
            v.i2f = self.slots[d][1]
            self.fpr[rt] = v
            return
        if op == 32 and ra == 1 and self.slots.get(d - 4, (None,))[0] == 'fctiw':   # lwz after fctiwz/stfd
            v = Value(f'(int)({self.slots[d - 4][1].text})', 'int')
            v.conv = True
            self.set(rt, v)
            return
        if op == 14 and rt == 1 and ra == 1:                         # frame pop
            return
        if op == 14:
            if ra == 0:
                self.set(rt, Value(str(d), 'int'))
                return
            base = self.reg(ra)
            if base.ctype == 'toc':
                raise Fail('address inside the TOC')
            iv = getattr(base, 'iv', None)
            if iv is not None:                                       # the induction step
                index, stride, k = iv
                if d != stride:
                    raise Fail('offset from an induction variable')
                v = Value(f'(int)({index} + {k + 1}) * {stride}', 'int', 1)
                v.iv = (index, stride, k + 1)
                self.set(rt, v)
                return
            if base.ctype in ('ptr', 'addr', 'gaddr'):
                self.set(rt, Value(f'(char*){paren(base)} + {d}', 'addr', 1))
            else:
                self.set(rt, Value(f'{paren(base)} + {d}', 'int', 1, origin=base))
            return
        if op == 15 and ra == 0:
            self.set(rt, Value(str(d << 16), 'int'))
            return
        if op == 32 and rt == 2:                                     # lwz r2,4(descriptor): callee's TOC
            v = self.field('u32', self.reg(ra), d)
            if v.vt is None or v.vt[0] != 'toc':
                raise Fail('r2 loaded from something other than a descriptor')
            return
        if w & 0xFC1FFFFF == 0x7C0903A6:                              # mtctr rS
            self.ctr = self.reg(rt)
            return
        if w == 0x4E800421:                                          # bctrl
            self.vcall()
            return
        if op in LOADS:
            v = self.field(LOADS[op], self.reg(ra), d)
            if op in (48, 50):
                self.fpr[rt] = v
                self.ffresh.add(rt)
            else:
                self.set(rt, v)
            return
        if op == 58 and w & 3 == 0:
            self.set(rt, self.field('u64', self.reg(ra), d))
            return
        if op in STORES:
            base = self.reg(ra)
            if base.ctype == 'toc':
                raise Fail('store into the TOC')
            value = self.freg(rt) if op in (52, 54) else self.reg(rt)
            ctype = STORES[op]
            if ctype == 'u32' and value.ctype in ('ptr', 'addr', 'gaddr'):
                ctype = 'void*'
            target = self.field(ctype, base, d)
            if getattr(target, 'cast', None) is not None:
                ctype = target.cast
            text = value.text if ctype in ('float', 'double') else f'({ctype})({value.text})'
            self.stmts.append(f'{target.text} = {text};')
            return
        if op == 31:
            xo = (w >> 1) & 0x3FF
            if xo == 444 and rt == rb:                               # mr
                v = self.gpr[rt] if self.gpr[rt] is not None else self.reg(rt)
                self.set(ra, v)
                return
            if xo in (922, 954):                                     # extsh / extsb
                v = self.reg(rt)
                t = 'short' if xo == 922 else 'signed char'
                self.set(ra, Value(f'(int)({t})({v.text})', 'int'))
                if w & 1:
                    self.cr[0] = (self.gpr[ra], '0', 'signed')
                return
            if xo == 824:                                            # srawi
                n = rb
                self.set(ra, Value(f'(int)({self.reg(rt).text}) >> {n}', 'int', 1))
                if w & 1:
                    self.cr[0] = (self.gpr[ra], '0', 'signed')
                return
            if xo in (28, 316, 124, 60) or (xo == 444 and rt != rb):  # and xor nor andc or
                a, b = self.reg(rt), self.reg(rb)
                text = {28: f'(u32)({a.text}) & (u32)({b.text})', 316: f'(u32)({a.text}) ^ (u32)({b.text})',
                        124: f'~((u32)({a.text}) | (u32)({b.text}))', 60: f'(u32)({a.text}) & ~(u32)({b.text})',
                        444: f'(u32)({a.text}) | (u32)({b.text})'}[xo]
                self.set(ra, Value(text, 'u32', 1))
                if w & 1:
                    self.cr[0] = (self.gpr[ra], '0', 'signed')
                return
            if xo == 104:                                            # neg
                self.set(rt, Value(f'-(int)({self.reg(ra).text})', 'int', 1))
                return
            if xo == 235:                                            # mullw
                self.set(rt, Value(f'(int)({self.reg(ra).text}) * (int)({self.reg(rb).text})', 'int', 1))
                return
            if xo in (24, 536, 792):                                 # slw srw sraw
                a, b = self.reg(rt), self.reg(rb)
                text = {24: f'(u32)({a.text}) << (int)({b.text})', 536: f'(u32)({a.text}) >> (int)({b.text})',
                        792: f'(int)({a.text}) >> (int)({b.text})'}[xo]
                self.set(ra, Value(text, 'int' if xo == 792 else 'u32', 1))
                return
            if xo == 986:                                            # extsw
                v = self.reg(rt)
                if v.ctype in ('u32', 'ptr'):
                    v = Value(f'(int){paren(v)}', 'int')
                elif v.call is not None:
                    v.call['type'] = 'int'
                    v.ctype = 'int'
                self.set(ra, v)
                return
            if xo in (266, 40):                                      # add, subf
                a, b = self.reg(ra), self.reg(rb)
                if xo == 266 and (getattr(a, 'iv', None) is not None) != (getattr(b, 'iv', None) is not None):
                    i_, p_ = (a, b) if getattr(a, 'iv', None) is not None else (b, a)
                    self.pointer(p_)                                 # base + i * stride: an array element
                    v = Value(f'(char*){paren(p_)} + {i_.text}', 'addr', 1)
                    v.ivbase = (p_, i_)
                    self.set(rt, v)
                    return
                ptr = ('ptr', 'addr', 'gaddr')
                if xo == 266 and (a.ctype in ptr or b.ctype in ptr):
                    p_, i_ = (a, b) if a.ctype in ptr else (b, a)
                    self.set(rt, Value(f'(char*){paren(p_)} + (int)({i_.text})', 'addr', 1))
                    return
                text = f'(int)({a.text}) + (int)({b.text})' if xo == 266 else f'(int)({b.text}) - (int)({a.text})'
                self.set(rt, Value(text, 'int', 1))
                return
            raise Fail(f'op 31/{xo}')
        if op == 30 and (w >> 2) & 7 == 0:                           # rldicl
            sh = ((w >> 11) & 31) | (((w >> 1) & 1) << 5)
            mb = ((w >> 6) & 31) | (((w >> 5) & 1) << 5)
            v = self.reg(rt)
            if sh == 0 and mb == 32 and getattr(v, 'conv', False):  # clrldi 32 after a conversion
                self.set(ra, v)
                return
            if sh == 0 and mb == 32:                                 # clrldi 32: a pointer
                if v.ctype == 'addr':
                    v = Value(v.text, 'ptr', v.prec)
                else:
                    self.pointer(v)
                    if v.origin is not None and v.ctype == 'ptr' and v.text.startswith('('):
                        pass
                self.set(ra, v)
                return
            if sh == 0 and mb in (48, 56):
                t = 'u16' if mb == 48 else 'u8'
                self.set(ra, Value(f'({t}){paren(v)}', t))
                return
            raise Fail('rldicl')
        if op == 18 and w & 3 == 1:
            following = self.words[i + 1] if i + 1 < len(self.words) else BLR
            self.call(branch_target(self.address + 4 * i, w), local=following not in (NOP, TOC_RESTORE))
            return
        raise Fail(f'opcode {op}')

    def gather_args(self, first, nint=0):
        """Argument values in parameter order, from GPR first (r3, or r4 after
        'this'). Floats go in f1, f2, ... and each also takes a GPR position:
        GPR positions the caller left unwritten hold the floats, in order;
        floats beyond the last GPR follow. nint: arguments the callee reads."""
        gfresh = {r for r in self.fresh if 3 <= r <= 10 and self.gpr[r] is not None and self.gpr[r].vt is None}
        floats = []
        k = 1
        while k in self.ffresh:
            floats.append(self.fpr[k])
            k += 1
        last = max(gfresh | {first - 1, 2 + nint})
        args = []
        for r in range(first, last + 1):
            if r in gfresh:
                args.append(self.reg(r))
            elif floats:
                args.append(floats.pop(0))
            elif self.gpr[r] is None and (r in self.args or self.made_call):
                break       # clobbered or never set: the callee's apparent read is not a real argument
            else:
                args.append(self.reg(r))
        return args + floats

    def call(self, target, local=False):
        nargs, nfargs = self.arity(target)
        args = self.gather_args(3, nargs)
        info = {'used': False, 'type': 'int', 'target': target, 'args': args, 'temp': f't{len(self.calls)}',
                'local': local}
        self.calls[len(self.calls)] = info
        self.stmts.append(info)
        for r in range(0, 13):
            if r != 2:
                self.gpr[r] = None
        for r in range(0, 14):
            self.fpr[r] = None
        self.made_call = True
        self.gpr[3] = Value(info['temp'], 'int', 0, info)
        self.fresh = set()
        self.ffresh = set()

    def rlwinm(self, w):
        rs, ra = (w >> 21) & 31, (w >> 16) & 31
        sh, mb, me = (w >> 11) & 31, (w >> 6) & 31, (w >> 1) & 31
        if mb > me:
            raise Fail('wrapping rlwinm mask')
        x = f'(u32)({self.reg(rs).text})'
        mask = ((1 << (32 - mb)) - 1) & ~((1 << (31 - me)) - 1) & 0xFFFFFFFF
        if sh == 0:
            text = f'{x} & {mask:#x}'
        elif mb == 0 and me == 31 - sh:                              # slwi
            text = f'{x} << {sh}'
        elif me == 31 and mb == 32 - sh:                             # srwi
            text = f'{x} >> {mb}'
        elif me == 31 and mb >= 32 - sh:                             # extract a field from the top part
            text = f'({x} >> {32 - sh}) & {(1 << (32 - mb)) - 1:#x}'
        elif mask & ((1 << sh) - 1) == 0:                           # shift left, then mask
            text = f'({x} << {sh}) & {mask:#x}'
        else:
            raise Fail('rlwinm rotate')
        self.set(ra, Value(text, 'u32', 1))
        if w & 1:
            self.cr[0] = (self.gpr[ra], '0', 'signed')

    def float_op(self, w):
        op = w >> 26
        ft, fa, fb, fc = (w >> 21) & 31, (w >> 16) & 31, (w >> 11) & 31, (w >> 6) & 31
        xo5, xo10 = (w >> 1) & 31, (w >> 1) & 0x3FF
        t = 'float' if op == 59 else 'double'
        if op == 63 and xo10 == 846:                                 # fcfid: integer bits to double
            src = getattr(self.freg(fb), 'i2f', None)
            if src is None:
                raise Fail('fcfid of a non-integer')
            t_ = 'u32' if src.ctype == 'u32' else 'int'
            self.fpr[ft] = Value(f'(double)({t_})({src.text})', 'double')
            self.fpr[ft].int_src = (t_, src)
            self.ffresh.add(ft)
            return
        if op == 63 and xo10 in (815, 15):                           # fctidz / fctiwz: float to integer bits
            v = Value(self.freg(fb).text, 'bits')
            v.f2i = ('fctid' if xo10 == 815 else 'fctiw', self.freg(fb))
            self.fpr[ft] = v
            return
        if op == 63 and xo10 == 0:                                   # fcmpu crf,fa,fb
            self.cr[(w >> 23) & 7] = (self.freg(fa), self.freg(fb), 'float')
            return
        if op == 63 and xo10 in (72, 40, 264, 12):                   # fmr fneg fabs frsp
            v = self.freg(fb)
            text = {72: v.text, 40: f'-{paren(v)}', 264: f'__builtin_fabsf({v.text})', 12: f'(float)({v.text})'}[xo10]
            if xo10 == 12 and getattr(v, 'int_src', None) is not None:
                text = f'(float)({v.int_src[0]})({v.int_src[1].text})'
            self.fpr[ft] = Value(text, v.ctype if xo10 != 12 else 'float', 1 if xo10 == 40 else 0)
            self.ffresh.add(ft)
            return
        bin_ops = {21: '+', 20: '-', 18: '/'}
        if xo5 in bin_ops:
            a, b = self.freg(fa), self.freg(fb)
            self.fpr[ft] = Value(f'{paren(a)} {bin_ops[xo5]} {paren(b)}', t, 1)
        elif xo5 == 25:                                              # fmul: fa * fc
            a, c = self.freg(fa), self.freg(fc)
            self.fpr[ft] = Value(f'{paren(a)} * {paren(c)}', t, 1)
        elif xo5 in (29, 28):                                        # fmadd / fmsub
            a, b, c = self.freg(fa), self.freg(fb), self.freg(fc)
            sign = '+' if xo5 == 29 else '-'
            self.fpr[ft] = Value(f'{paren(a)} * {paren(c)} {sign} {paren(b)}', t, 1)
        else:
            raise Fail(f'float op {op}/{xo5}')
        self.ffresh.add(ft)

    def vcall(self):
        c = self.ctr
        if c is None or c.vt is None or c.vt[0] != 'code':
            raise Fail('indirect call that is not a virtual call')
        obj, slot = c.vt[1], c.vt[2]
        this = self.reg(3)
        if this is not obj and this.text != obj.text:
            raise Fail('this is not the object whose vtable was read')
        args = [this] + self.gather_args(4)
        info = {'used': False, 'type': 'int', 'target': None, 'args': args, 'temp': f't{len(self.calls)}',
                'local': False, 'virtual': (obj, slot)}
        self.calls[len(self.calls)] = info
        self.stmts.append(info)
        for r in range(0, 13):
            if r != 2:
                self.gpr[r] = None
        for r in range(0, 14):
            self.fpr[r] = None
        self.made_call = True
        self.gpr[3] = Value(info['temp'], 'int', 0, info)
        self.fresh = set()
        self.ffresh = set()
        self.ctr = None

    def render(self):
        # A function returns a value if any return path set r3 (or returns a
        # used call result); then every return returns r3's value.
        valued = any(r['fresh'] or (r['value'] is not None and r['value'].call is not None and r['value'].call['used'])
                     for r in self.returns)
        ret_type = 'void'
        if valued:
            types = {CTYPES.get(r['value'].ctype) for r in self.returns if r['value'] is not None}
            if None in types or len(types) != 1 or any(r['value'] is None for r in self.returns):
                raise Fail('return type')
            ret_type = types.pop()
        if valued:
            for r in self.returns:
                if r['value'] is not None and r['value'].call is not None:
                    r['value'].call['used'] = True
        callees, top = {}, []
        for info in self.calls.values():
            if info['used']:
                top.append(f'{info["type"]} {info["temp"]};')
        top += [f'{t} {n};' for t, n in self.vars]
        def lines(stmts, indent):
            out = []
            pad = '    ' * indent
            for st in stmts:
                if isinstance(st, dict) and st.get('virtual'):
                    obj, slot = st['virtual']
                    types = [CTYPES.get(a.ctype, 'int') for a in st['args'][1:]]
                    rtype = st['type'] if st['used'] else 'void'
                    tag = None
                    for k, arg in self.args.items():
                        if obj is arg:
                            tag = f'unk_{self.address:08x}_{arg.text}'
                            target = arg.text
                    if tag is None:
                        tag = f'unk_{self.address:08x}_vc{len(self.vclasses)}'
                        target = f'(({tag}*)({obj.text}))'
                    slots = self.vclasses.setdefault(tag, {})
                    if slots.get(slot, (rtype, types)) != (rtype, types):
                        raise Fail('one slot, two signatures')
                    slots[slot] = (rtype, types)
                    call = f'{target}->vf{slot}({", ".join(f"({t})({a.text})" for a, t in zip(st["args"][1:], types))})'
                    out.append(pad + (f'{st["temp"]} = {call};' if st['used'] else f'{call};'))
                    continue
                if isinstance(st, dict):
                    types = [CTYPES.get(a.ctype, 'int') for a in st['args']]
                    rtype = st['type'] if st['used'] else 'void'
                    key = (rtype, types, st['local'])
                    if st['target'] in callees and callees[st['target']] != key:
                        raise Fail('one callee, two signatures')
                    callees[st['target']] = key
                    prefix = 'stub' if st['local'] else 'fn'
                    call = f'{prefix}_{st["target"]:08x}({", ".join(f"({t})({a.text})" for a, t in zip(st["args"], types))})'
                    out.append(pad + (f'{st["temp"]} = {call};' if st['used'] else f'{call};'))
                elif isinstance(st, list) and st[0] == 'return':
                    r = self.returns[st[1]]
                    out.append(pad + (f'return ({ret_type})({r["value"].text});' if valued else 'return;'))
                elif isinstance(st, list) and st[0] == 'while':
                    out.append(pad + f'while ({st[1]}) {{')
                    out += lines(st[2], indent + 1)
                    out.append(pad + '}')
                elif isinstance(st, list) and st[0] == 'cfor':
                    n = st[2] if re.fullmatch(r'\d+', st[2]) else f'(int)({st[2]})'
                    out.append(pad + f'for ({st[1]} = 0; {st[1]} < {n}; {st[1]}++) {{')
                    out += lines(st[3], indent + 1)
                    out.append(pad + '}')
                elif isinstance(st, list) and st[0] == 'for':
                    out.append(pad + 'for (;;) {')
                    out += lines(st[1], indent + 1)
                    out.append(pad + '}')
                elif isinstance(st, list) and st[0] == 'do':
                    out.append(pad + 'do {')
                    out += lines(st[1], indent + 1)
                    out.append(pad + f'}} while ({st[2]});')
                elif isinstance(st, list) and st[0] == 'if':
                    out.append(pad + f'if ({st[1]}) {{')
                    out += lines(st[2], indent + 1)
                    if st[3]:
                        out.append(pad + '} else {')
                        out += lines(st[3], indent + 1)
                    out.append(pad + '}')
                else:
                    out.append(pad + st)
            return out
        body = [l.strip('\n') for l in top] + lines(drop_loop_guards(self.stmts), 0)
        body = drop_dead_vars(body, {n for _, n in self.vars})
        # A trailing "return;" in a void function is implicit.
        while body and body[-1] == 'return;':
            body.pop()
        params = []
        structs = []
        nested = [path for path in self.layouts if path[0] == 'g' or len(path) > 1]
        structs += [f'struct {self.tag(path)};' for path in nested]   # forward declarations
        for path in nested:
            structs.append(struct_decl(self.tag(path), self.layouts[path], self.pointer_fields(path)))
        for k in sorted(self.args):
            tag = f'unk_{self.address:08x}_{self.args[k].text}'
            if tag in self.vclasses:
                decl = class_decl(tag, self.vclasses.pop(tag), self.layouts.get((k,), {}), self.pointer_fields((k,)))
                if decl is None:
                    raise Fail('class layout')
                structs.append(decl)
                params.append(f'{tag}* {self.args[k].text}')
            elif (k,) in self.layouts:
                structs.append(struct_decl(tag, self.layouts[(k,)], self.pointer_fields((k,))))
                params.append(f'{tag}* {self.args[k].text}')
            else:
                params.append(f'{CTYPES.get(self.args[k].ctype, "int")} {self.args[k].text}')
        if self.fargs:
            if self.args:
                raise Fail('mixed arguments')
            params = [f'float {self.fargs[k].text}' for k in sorted(self.fargs)]
        for tag, slots in self.vclasses.items():           # objects that are not arguments
            structs.append(class_decl(tag, slots, {}))
        decls = structs + [f'extern {self.tag(("g", d))} {n};' if ('g', d) in self.layouts else f'extern char {n}[];'
                           for d, n in self.globals.items()]
        if any('/*vtable slot' in l or '/*virtual*/' in l for l in body):
            raise Fail('vtable internals used as an ordinary value')
        sig = f'{ret_type} fn_{self.address:08x}({", ".join(params) if params else "void"})'
        return decls, sig + ' {\n    ' + '\n    '.join(body) + '\n}', callees


WHY = None   # set to a Counter by --why: failure reasons


def drop_loop_guards(stmts):
    """GCC tests a for loop's count before entering it. Lifted, that test is
    an if around the loop; the source had none, so remove it. Statements in
    the guard's else branch may only set merge variables (dead without it)."""
    out = []
    for st in stmts:
        if isinstance(st, list) and st[0] == 'if':
            st = ['if', st[1], drop_loop_guards(st[2]), drop_loop_guards(st[3])]
            then, other = st[2], st[3]
            loops = [s for s in then if isinstance(s, list)]
            merges = lambda xs: all(isinstance(s, str) and re.match(r'v\d+ = ', s) for s in xs)
            if (len(loops) == 1 and loops[0][0] == 'cfor' and not re.fullmatch(r'\d+', loops[0][2])
                    and loops[0][2] in st[1] and re.search(r'(>|!=) \(\w+\)\(0\)$', st[1])
                    and merges(other) and merges(s for s in then if s is not loops[0])):
                out += then
                continue
        elif isinstance(st, list) and st[0] == 'cfor':
            st = ['cfor', st[1], st[2], drop_loop_guards(st[3])]
        elif isinstance(st, list) and st[0] in ('for', 'do'):
            st = [st[0], drop_loop_guards(st[1])] + st[2:]
        elif isinstance(st, list) and st[0] == 'while':
            st = ['while', st[1], drop_loop_guards(st[2])]
        out.append(st)
    return out


def drop_dead_vars(body, names):
    """Remove merge variables nobody reads: their declarations and every
    assignment to them. GCC deletes such stores too, but late: left in, they
    change which induction variables it picks for a loop."""
    while True:
        dead = set()
        for name in names:
            assign = re.compile(rf'^\s*{name} = .*;$')
            decl = re.compile(rf'\w[\w ]*\*? {name};')
            if not any(re.search(rf'\b{name}\b', l) for l in body
                       if not assign.match(l) and not decl.fullmatch(l.strip())):
                dead.add(name)
        if not dead:
            break
        names = names - dead
        body = [l for l in body
                if not any(re.match(rf'^\s*{n} = .*;$', l) or re.fullmatch(rf'\w[\w ]*\*? {n};', l.strip())
                           for n in dead)]
    # Branches left empty: "} else {" straight before "}", and "if (c) {}".
    out = []
    for l in body:
        if l.strip() == '}' and out and out[-1].strip() == '} else {':
            out[-1] = l
            continue
        if l.strip() == '}' and out and re.fullmatch(r'if \(.*\) \{', out[-1].strip()):
            out.pop()
            continue
        out.append(l)
    return out


def lift(address, words, arity):
    try:
        first = Lifter(address, words, arity).run()
        preset = {k: v.ctype for k, v in first.args.items()}
        typed = Lifter(address, words, arity, preset, poly=first.polymorphic).run()
        layouts = {}
        for path, fields in sorted(typed.access.items(), key=lambda kv: (len(kv[0]), str(kv[0]))):
            if len(path) > 1 and path[0] != 'g' or len(path) > 2:
                parent = path[:-1]
                if layouts.get(parent, {}).get(path[-1]) != 'u32':
                    continue                      # loaded from something without a struct
            by_offset = {}
            for off, ctype in fields:
                if by_offset.get(off, ctype) != ctype:
                    break
                by_offset[off] = ctype
            else:
                if struct_decl('x', by_offset) is not None:
                    layouts[path] = by_offset
        second = Lifter(address, words, arity, preset, layouts, poly=first.polymorphic).run()
        if not second.stmts and not second.args:
            return None
        return second.render()
    except RecursionError:
        return None
    except Fail as e:
        if WHY is not None:
            WHY[re.sub(r'\d+', 'N', str(e))] += 1
        return None


def arity_finder(orig, functions):
    """arity(target) for Lifter: (integer, float) argument counts the callee
    reads. A TOC-switching linker stub (std r2,40(r1); addis/addi r2; b f)
    has the arity of the function it jumps to."""
    cache = {}
    def arity(target):
        if target not in cache:
            size = functions.get(target)
            words = struct.unpack(f'>{size // 4}I', orig.read(target, size)) if size else ()
            if len(words) == 4 and words[0] == 0xF8410028 and words[1] >> 16 == 0x3C42 \
                    and words[2] >> 16 == 0x3842 and words[3] >> 26 == 18 and not words[3] & 3:
                real = target + 12 + branch_target(0, words[3])
                size = functions.get(real)
                words = struct.unpack(f'>{size // 4}I', orig.read(real, size)) if size else ()
            cache[target] = reads_before_writes(words) if words else (None, None)
        if cache[target][0] is None:
            raise Fail('unknown callee')
        return cache[target]
    return arity


def short_name(unit):
    """A file name for a unit: long mangled names are cut and given a hash."""
    if len(unit) <= 80:
        return unit
    import hashlib
    return unit[:64] + '_' + hashlib.sha1(unit.encode()).hexdigest()[:8]


def main():
    global WHY
    if '--why' in sys.argv:
        # Only report why functions with loops fail to lift; write nothing.
        from collections import Counter
        WHY = Counter()
        orig, functions = Original(), load_functions()
        arity = arity_finder(orig, functions)
        for address, size in sorted(functions.items()):
            if size // 4 > MAX_WORDS:
                continue
            words = struct.unpack(f'>{size // 4}I', orig.read(address, size))
            if any(w >> 26 == 16 and s16(w & 0xFFFC) <= 0 for w in words):
                lift(address, words, arity)
        for reason, n in WHY.most_common(25):
            print(f'{n:6d}  {reason}')
        return
    if '--one' in sys.argv:
        # Print one function's lifted source (for experiments); write nothing.
        address = int(sys.argv[sys.argv.index('--one') + 1], 16)
        orig, functions = Original(), load_functions()
        arity = arity_finder(orig, functions)
        size = functions[address]
        from collections import Counter
        WHY = Counter()
        result = lift(address, struct.unpack(f'>{size // 4}I', orig.read(address, size)), arity)
        if result is None:
            print('not lifted:', ', '.join(WHY) or 'nothing to lift')
            return
        decls, code, calls = result
        print('\n'.join(decls) + '\n' + code)
        return
    prune = set()
    pruned_path = BUILD / 'lift_pruned.txt'
    if '--prune' in sys.argv:
        log = Path(sys.argv[sys.argv.index('--prune') + 1]).read_text(encoding='utf-8', errors='replace')
        prune = {int(m.group(1), 16) for m in re.finditer(r'^([0-9a-f]{8}) +[\d.]+% (?!MATCH)', log, re.M)}
        for block in re.split(r'^warning: ', log, flags=re.M):
            if 'compile failed' not in block.split('\n', 1)[0]:
                continue
            named = {int(m, 16) for m in re.findall(r"In function '[^']*\bfn_([0-9a-f]{8})\(", block)}
            if not named:
                path = ROOT / block.split(':', 1)[0].replace('\\', '/')
                if path.exists():
                    named = {int(m, 16) for m in re.findall(r'^\S.* fn_([0-9a-f]{8})\(', path.read_text(encoding='utf-8'), re.M)}
            prune |= named
        if pruned_path.exists():
            prune |= {int(x, 16) for x in pruned_path.read_text().split()}
        pruned_path.write_text('\n'.join(f'{a:08x}' for a in sorted(prune)) + '\n')
    elif pruned_path.exists():
        pruned_path.unlink()
    orig, functions = Original(), load_functions()
    units = load_units()
    starts = [u[1] for u in units]
    elsewhere = set()
    for src in (ROOT / 'src').rglob('*.c*'):
        if 'lift' not in src.parts:
            elsewhere |= {int(m, 16) for m in re.findall(r'\bfn_([0-9a-f]{8})\s*\(', src.read_text(encoding='utf-8'))
                          if f'fn_{m}(' in src.read_text(encoding='utf-8')}
    elsewhere |= {int(l.split()[0], 16) for l in (ROOT / 'config' / 'symbols_gen.txt').read_text().splitlines()
                  if l and not l.startswith('#')}
    elsewhere |= {int(l.split()[0], 16) for l in (ROOT / 'config' / 'symbols.txt').read_text().splitlines()
                  if l.split('#', 1)[0].strip()}
    arity = arity_finder(orig, functions)
    by_unit = defaultdict(list)
    for address, size in sorted(functions.items()):
        if address in elsewhere or address in prune or size // 4 > MAX_WORDS:
            continue
        words = struct.unpack(f'>{size // 4}I', orig.read(address, size))
        result = lift(address, words, arity)
        if result is None:
            continue
        i = bisect.bisect_right(starts, address) - 1
        if 0 <= i and address < units[i][2]:
            by_unit[units[i][0]].append((address,) + result)
    for old in OUT.glob('*.cpp'):
        old.unlink()
    OUT.mkdir(parents=True, exist_ok=True)
    total = 0
    for unit, items in by_unit.items():
        defined = {a for a, *_ in items}
        callees, keep = {}, []
        for address, decls, code, calls in items:
            if any(t in defined for t in calls):
                continue                     # calls a function lifted in this file: signatures may differ
            if any(t in callees and callees[t] != c for t, c in calls.items()):
                continue
            callees.update(calls)
            keep.append((address, decls, code))
        if not keep:
            continue
        text = ['// Generated by tools/lift.py from the original machine code: straight-line',
                f'// functions of unit {unit}. Each one compiles to the original bytes.',
                '#include "wkc2.h"', '', 'extern "C" {', '']
        for target, (rtype, types, local) in sorted(callees.items()):
            if not local:
                text.append(f'{rtype} fn_{target:08x}({", ".join(types) if types else "void"});')
                continue
            # A callee in the same source file: GCC calls static functions
            # without a TOC slot. Placeholder until it is decompiled;
            # check.py ignores stub_* functions.
            params = ', '.join(f'{t} p{n}' for n, t in enumerate(types)) or 'void'
            uses = ', '.join(f'"r"(p{n})' for n in range(len(types)))
            if rtype == 'void':
                body = f'__asm__ volatile ("" : : {uses});' if types else '__asm__ volatile ("");'
            else:
                body = (f'{rtype} r; __asm__ volatile ("" : "=r"(r){" : " + uses if types else ""}); return r;')
            text.append(f'static {rtype} __attribute__((noinline)) stub_{target:08x}({params}) {{ {body} }}')
        text.append('')
        for address, decls, code in keep:
            text += decls + [code, '']
            total += 1
        text.append('}')
        (OUT / f'{short_name(unit)}.cpp').write_text('\n'.join(text) + '\n', encoding='utf-8', newline='\n')
    print(f'{total} functions lifted' + (f' ({len(prune)} pruned so far)' if prune else ''))


if __name__ == '__main__':
    main()
