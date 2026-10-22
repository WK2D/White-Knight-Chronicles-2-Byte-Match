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
    def __init__(self, text, ctype='int', prec=0, call=None, origin=None):
        self.text, self.ctype, self.prec, self.call = text, ctype, prec, call
        self.origin = origin       # for 'x + n': x, which is a pointer if the sum is used as one


def paren(v):
    return v.text if v.prec == 0 else f'({v.text})'


SIZES = {'u8': 1, 'u16': 2, 's16': 2, 'u32': 4, 'float': 4, 'void*': 4, 'double': 8, 'u64': 8}


def struct_decl(tag, by_offset):
    """struct tag { fields at their offsets }, or None if they overlap, are
    misaligned or lie before the start (negative offsets)."""
    out, at = [f'struct {tag} {{'], 0
    for off in sorted(by_offset):
        t = by_offset[off]
        if off < at or off % SIZES[t]:
            return None
        if off > at:
            out.append(f'    char pad_{at:x}[{off - at}];')
        out.append(f'    {t} f_{off:x};')
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
    def __init__(self, address, words, arity, preset=None, layouts=None):
        self.address, self.words, self.arity = address, words, arity
        self.preset = preset or {}
        self.layouts = layouts or {}      # argument register -> {offset: type} (second pass)
        self.access = defaultdict(set)    # argument register -> {(offset, type)} (first pass)
        self.static = {}                  # callees called without a TOC slot: static in this file
        self.gpr = {r: None for r in range(32)}
        self.fpr = {r: None for r in range(32)}
        self.args, self.fargs = {}, {}
        self.stmts, self.calls, self.globals = [], {}, {}
        self.fresh = set()
        self.cr = {}                      # condition field -> (lhs, rhs, 'signed'|'unsigned'|'pointer')
        self.vars = []                    # merge variables: (type, name)
        self.returns = []                 # one dict per return statement
        self.ended = False                # the current path has returned

    def param(self, k):
        for j in range(3, k + 1):
            if j not in self.args:
                v = Value(f'a{j - 3}', self.preset.get(j, 'int'))
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
                return Value('TOC', 'toc')
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
        return Value(self.globals[d], 'gaddr')

    def field(self, ctype, base, d):
        if base.ctype == 'toc':
            if ctype != 'u32':
                raise Fail('non-word TOC load')
            return self.address_of_global(d)
        self.pointer(base)
        for k, arg in self.args.items():
            if base is arg:
                self.access[k].add((d, ctype))
                if k in self.layouts and self.layouts[k].get(d) == ctype:
                    return Value(f'{base.text}->f_{d:x}' if d >= 0 else f'{base.text}->f_m{-d:x}', ctype)
        return Value(f'FIELD({ctype}, {base.text}, {d})', ctype)

    def run(self):
        """Lift the function's forward-only control flow into nested ifs."""
        w = self.words
        n = len(w)
        leaders = {0}
        for i, x in enumerate(w):
            op = x >> 26
            if op == 16:                                   # bc
                t = i + (s16(x & 0xFFFC) >> 2)
                if x & 3 or t <= i or t > n:
                    raise Fail('backward, absolute or linked conditional branch')
                leaders |= {t, i + 1}
            elif op == 18 and not x & 1:                   # b (not bl)
                t = i + ((branch_target(0, x)) >> 2)
                if x & 2 or t <= i or t > n:
                    raise Fail('backward or outside branch')
                leaders |= {t, i + 1}
            elif op == 19:
                xo = (x >> 1) & 0x3FF
                if xo != 16 or x & 1:
                    raise Fail('indirect branch')
                leaders.add(i + 1)
        self.leaders = sorted(l for l in leaders if l < n)
        self.region(0, None)
        if not self.ended:
            raise Fail('falls off the end')
        return self

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
                todo += [last + (s16(t & 0xFFFC) >> 2), last + 1]
            elif op == 18 and not t & 1:
                todo.append(last + (branch_target(0, t) >> 2))
            elif op == 19:
                if (t >> 21) & 0x14 != 0x14:              # conditional return: may fall through
                    todo.append(last + 1)
            else:
                todo.append(last + 1)
        return out

    def snapshot(self):
        return (dict(self.gpr), dict(self.fpr), set(self.fresh), dict(self.cr), self.ended)

    def restore(self, state):
        self.gpr, self.fpr, self.fresh, self.cr, self.ended = (dict(state[0]), dict(state[1]), set(state[2]),
                                                               dict(state[3]), state[4])

    def condition(self, crf, bit, true):
        if crf not in self.cr:
            raise Fail('condition field never set')
        a, b, kind = self.cr[crf]
        ops = {(0, True): '<', (1, True): '>', (2, True): '==', (0, False): '>=', (1, False): '<=', (2, False): '!='}
        if (bit, true) not in ops:
            raise Fail('summary-overflow bit')
        bt = b.text if isinstance(b, Value) else b
        if kind == 'unsigned':
            return f'(u32)({a.text}) {ops[bit, true]} (u32)({bt})'
        if kind == 'pointer':
            return f'{a.text} {ops[bit, true]} {bt}'
        return f'(int)({a.text}) {ops[bit, true]} (int)({bt})'

    def branch_condition(self, word):
        """C condition under which a bc/bclr is taken."""
        bo, bi = (word >> 21) & 31, (word >> 16) & 31
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

    def region(self, b, stop):
        """Lift blocks from b until stop (or a return), appending to self.stmts."""
        while b is not None and b != stop and b < len(self.words):
            kind, data = self.run_block(b)
            if kind == 'ret':
                self.emit_return()
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
            reach_t, reach_f = self.successors(taken), self.successors(fall)
            common = sorted(reach_t & reach_f)
            join = common[0] if common else None
            if stop is not None and join is not None and join > stop:
                join = stop
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
        if op == 28:                                                 # andi.
            v = self.reg(rt)
            res = Value(f'{paren(v)} & {w & 0xFFFF}', 'int', 1)
            self.set(ra, res)
            self.cr[0] = (res, '0', 'signed')
            return
        if w in (NOP, TOC_RESTORE, 0x7C0802A6, 0x7C0803A6):        # nop, TOC restore, mflr/mtlr r0
            return
        if op == 62 and ra == 1:                                     # stdu r1 / std rX,d(r1)
            if w & 3 == 1 and rt == 1:
                return
            if w & 3 == 0 and (rt == 0 or rt >= 14):
                return
            raise Fail('spill')
        if op == 58 and w & 3 == 0 and ra == 1:                      # ld rX,d(r1)
            if rt == 0 or rt >= 14:
                return
            raise Fail('stack load')
        if op == 14 and rt == 1 and ra == 1:                         # frame pop
            return
        if op == 14:
            if ra == 0:
                self.set(rt, Value(str(d), 'int'))
                return
            base = self.reg(ra)
            if base.ctype == 'toc':
                raise Fail('address inside the TOC')
            if base.ctype in ('ptr', 'addr', 'gaddr'):
                self.set(rt, Value(f'(char*){paren(base)} + {d}', 'addr', 1))
            else:
                self.set(rt, Value(f'{paren(base)} + {d}', 'int', 1, origin=base))
            return
        if op == 15 and ra == 0:
            self.set(rt, Value(str(d << 16), 'int'))
            return
        if op in LOADS:
            v = self.field(LOADS[op], self.reg(ra), d)
            if op in (48, 50):
                self.fpr[rt] = v
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
            text = value.text if ctype in ('float', 'double') else f'({ctype})({value.text})'
            self.stmts.append(f'{target.text} = {text};')
            return
        if op == 31:
            xo = (w >> 1) & 0x3FF
            if xo == 444 and rt == rb:                               # mr
                v = self.gpr[rt] if self.gpr[rt] is not None else self.reg(rt)
                self.set(ra, v)
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

    def call(self, target, local=False):
        nargs, nfargs = self.arity(target)
        if nfargs:
            raise Fail('float arguments')
        # The caller's own setup also shows arguments: registers it filled.
        fresh = sorted(r for r in self.fresh if 3 <= r <= 10)
        while fresh and fresh == list(range(3, 3 + len(fresh))) and len(fresh) > nargs:
            nargs = len(fresh)
        args = [self.reg(r) for r in range(3, 3 + nargs)]
        info = {'used': False, 'type': 'int', 'target': target, 'args': args, 'temp': f't{len(self.calls)}',
                'local': local}
        self.calls[len(self.calls)] = info
        self.stmts.append(info)
        for r in range(0, 13):
            if r != 2:
                self.gpr[r] = None
        for r in range(0, 14):
            self.fpr[r] = None
        self.gpr[3] = Value(info['temp'], 'int', 0, info)
        self.fresh = set()

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
        body = [l.strip('\n') for l in top] + lines(self.stmts, 0)
        # A trailing "return;" in a void function is implicit.
        while body and body[-1] == 'return;':
            body.pop()
        params = []
        structs = []
        for k in sorted(self.args):
            if k in self.layouts:
                tag = f'unk_{self.address:08x}_{self.args[k].text}'
                structs.append(struct_decl(tag, self.layouts[k]))
                params.append(f'{tag}* {self.args[k].text}')
            else:
                params.append(f'{CTYPES.get(self.args[k].ctype, "int")} {self.args[k].text}')
        if self.fargs:
            if self.args:
                raise Fail('mixed arguments')
            params = [f'float {self.fargs[k].text}' for k in sorted(self.fargs)]
        decls = structs + [f'extern char {n}[];' for n in self.globals.values()]
        sig = f'{ret_type} fn_{self.address:08x}({", ".join(params) if params else "void"})'
        return decls, sig + ' {\n    ' + '\n    '.join(body) + '\n}', callees


def lift(address, words, arity):
    try:
        first = Lifter(address, words, arity).run()
        preset = {k: v.ctype for k, v in first.args.items()}
        typed = Lifter(address, words, arity, preset).run()
        layouts = {}
        for k, fields in typed.access.items():
            by_offset = {}
            for off, ctype in fields:
                if by_offset.get(off, ctype) != ctype:
                    break
                by_offset[off] = ctype
            else:
                if struct_decl('x', by_offset) is not None:
                    layouts[k] = by_offset
        second = Lifter(address, words, arity, preset, layouts).run()
        if not second.stmts and not second.args:
            return None
        return second.render()
    except Fail:
        return None


def short_name(unit):
    """A file name for a unit: long mangled names are cut and given a hash."""
    if len(unit) <= 80:
        return unit
    import hashlib
    return unit[:64] + '_' + hashlib.sha1(unit.encode()).hexdigest()[:8]


def main():
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
    cache = {}
    def arity(target):
        if target not in cache:
            size = functions.get(target)
            cache[target] = reads_before_writes(struct.unpack(f'>{size // 4}I', orig.read(target, size))) \
                if size else (None, None)
        if cache[target][0] is None:
            raise Fail('unknown callee')
        return cache[target]
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
