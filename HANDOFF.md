# Handoff

Read this first if you are an AI or a new contributor picking up this
repository. It is kept current with every progress commit.

_Last updated: 2026-10-23._

## What this is

A matching decompilation of **White Knight Chronicles II** (PS3, USA,
BLUS30784 v01.00). The goal is C/C++ source that compiles, with the game's
own compiler, to the exact bytes of the original `EBOOT.elf`. Progress is
measured per function and drawn as the units map in the README. It is an
alternative to the static-recompilation port at
[White-Knight-Chronicles-2-Recomp](https://github.com/WK2D/White-Knight-Chronicles-2-Recomp).

## Current state

- Compiler: rebuilt from Sony's GPL source (see below) and proven. Real game
  functions compile to identical bytes.
- Flags: `-O2` (in `tools/common.py`, `CFLAGS`). Per-file overrides go in
  `config/flags.txt` (fnmatch pattern, then flags). Three loop functions
  (`fn_007f1cf8`, `fn_008ca824`, `fn_00a25abc`) match 100% from natural C with
  `-O2` and not with `-fno-ivopts`. The project used `-O2 -fno-ivopts` before
  2026-10-22; only `src/game/CPostEffectManager.cpp` keeps that, as an
  override, until a natural `-O2` form of `fn_004b32e8` is found.
- Matched: **19,256 of 50,844 functions** (4.92% of code). Six are by hand
  (`src/game/*.cpp`); the rest are generated: `tools/gen_simple.py`
  (`src/gen/`, small shapes), `tools/lift.py` (`src/lift/`, the lifter),
  `tools/permute.py` (`src/perm/`, lifter output in another statement order)
  and `tools/classgen.py` (`src/cls/`, constructors as C++ classes). Details are in `progress/report.json`, which the README map is
  drawn from.
- Units: about 4,600, **inferred** (see "Units" below). They are estimates.
- Bulk generation (`tools/gen_simple.py`) covers these shapes: empty bodies,
  constant returns, single field loads and stores, field addresses, global
  addresses and global field loads, and constant stores to several fields.
  - Multi-field stores need a real struct per function; `FIELD()` casts
    through `char*` change the compiler's store scheduling.
  - Tail calls with constant arguments (`li r3..; [clrldi]; b target`) are
    generated as C in `src/gen/<unit>.c`. GCC 4.1 only emits `b` to a
    `static` function of the same file, so each callee gets a
    `stub_<address>` placeholder (noinline, empty asm using its arguments),
    which `check.py` ignores. When a callee is decompiled, replace its stub
    with the real static function in that file.
  - `.c` files compile with `ppu-lv2-gcc`, `.cpp` with `ppu-lv2-g++`.
  - `lwz r9,X(r2); stw r9,0(r3); blr` (about 450) is an empty destructor of a
    derived class whose base destructor is inline: only the base's vptr store
    survives. It is generated as a placeholder class pair, and the compiled D1
    symbol is mapped in `config/symbols_gen.txt`, which `gen_simple.py`
    writes. The checker ignores the other compiler-made variants (D0/D2) in
    `src/gen/`.
  - Float constant returns read their value from the TOC entry (the TOC base
    comes from the OPD).
- `tools/lift.py` lifts functions of up to 384 instructions (`MAX_WORDS`; 128
  until 2026-10-23): forward branches,
  loops (`do`/`while`/`for (;;)` with break, continue and return inside), and
  virtual calls.
  `python tools/lift.py --why` counts why functions with loops fail to lift. It runs each one symbolically:
  registers hold C expressions, and types come from use (a base address
  means a pointer, `extsw` means int, `clrldi 32` means a pointer).
  - A callee's arity comes from the callee's code: the argument registers it
    reads before writing.
  - Each function is lifted twice; the second pass uses the argument types
    the first pass found.
  - A `bl` without a following `nop` or `ld r2` means the callee is static in
    the same file. It is called through a `stub_<address>` placeholder.
  - Arity is the larger of the callee's reads-before-writes and the
    caller's filled argument registers.
  - Accesses through an argument use a struct per argument (fields at their
    offsets), because `FIELD()` casts change store scheduling. That takes a
    third pass.
  - Control flow: blocks are split at branches. Compares (`cmpwi`/`cmplwi`/
    `cmpw`/`cmplw`/`cmpdi`, `andi.`) record a condition field. A conditional
    branch emits `if (!taken) { fallthrough } else { taken }` up to the join
    (the first block both paths reach). A conditional return (`beqlr` and the
    like) becomes `if (c) return ...;`.
  - Where paths join with different register values, each branch assigns a
    merge variable (`vN`). Call results and merge variables are declared at
    the top of the function.
  - A function returns a value if any return path sets r3.
  - Virtual calls (only in functions containing `bctrl`): the offset-0 word
    of an object is its vptr. `lwz rX,4k(vptr)` is slot k's function
    descriptor, whose code (+0) goes to CTR and TOC (+4) to r2. The object
    becomes a C++ class with `virtual vf0..vfK`, the used slots carrying
    their signatures and its fields laid out after the vptr. The call is
    `obj->vfK(args)`.
  - Loops: every back edge (`bc` or `b`) marks a loop. The last one is the
    latch; earlier ones become `continue`. Branches to the block after the
    loop become `break`. Exits to a block that only leads to a return are
    lifted as that return (`return_path`, outside the loop context). A loop
    entered by a `b` to its test block (GCC's `while` layout) becomes
    `while (cond) { body }`.
  - Older notes: a backward `bc` is a latch. The body runs from the header to the
    latch. Registers the body writes that held values on entry become
    variables (assigned before the loop and updated at the end of the body),
    and the loop is emitted as `do { } while (cond)` with the condition in
    terms of those variables. `bdnz` is `while (--counter != 0)`, with the
    counter taken from `mtctr`.
  - Instructions: `rlwinm` (shifts, masks, field extracts), `addis` (including
    TOC high parts), `ori`/`oris`/`xori`, `and`/`or`/`xor`/`nor`/`andc`/`neg`,
    `mulli`/`mullw`, `slw`/`srw`/`sraw`/`srawi`, `extsh`/`extsb`/`extsw`,
    single and double float arithmetic, `fmr`/`fneg`/`fabs`/`frsp` and
    `fcmpu`.
  - Arguments: floats go in f1, f2, ... and each also takes a GPR position.
    GPR positions the caller left unwritten hold the floats in order; extra
    floats follow. Registers holding vtable internals never count as
    arguments.
  - `--prune` drops non-matches and the functions named in compile errors.
    The pruned set accumulates in `build/lift_pruned.txt`, and a run without
    `--prune` resets it.
  - After generating, run check and then `gen_simple.py --prune build/check.log`
    to drop non-matches (47 at last run).

## Environment

- **Original:** the user's own decrypted `orig/EBOOT.elf`, SHA-1
  `92198149c562c676815acb560e7b9861b894abd6`. It is never committed, and the
  tools refuse other files. On the maintainer's machine a copy lives at
  `../../work/wkc2-inspection/PS3_GAME/USRDIR/EBOOT.elf`.
- **Compiler:** `sh tools/build_compiler.sh` builds GCC 4.1.1 + binutils 2.17
  for `ppu-lv2` into `tools/ppu-gcc/`:
  - It runs in Docker (`ubuntu:14.04`), because GCC 4.1.1 needs an old host
    compiler; newer glibc images crash and old ones like Debian lenny segfault
    under WSL2.
  - The source is <https://github.com/sagemono/ps3gcc>, branch
    `toolchain-src-3.1.0-GCC411` (rev 3364). The game was built with rev 3320
    of the same SDK; rev 3322 (branch `toolchain-src-2.8.0-GCC411-Jul29`)
    produced identical code on every test so far.
  - Only `as`, `cc1`, `cc1plus` and the drivers are built. libgcc needs Sony's
    SDK headers, and matching only needs object files.
- **On Windows** the tools run the Linux compiler through Docker
  automatically. On Linux they run it directly.
- **Python 3**, standard library only.

## Workflow

```sh
python tools/check.py -v        # compile src/, compare with orig/EBOOT.elf, write progress/report.json
python tools/progress_map.py    # redraw progress/map.svg (the README map)
python tools/classes.py         # RTTI classes -> build/classes.json, build/vtables.json
python tools/make_units.py      # infer config/units.txt (needs classes.py first)
python tools/shapes.py 40       # survey small functions by instruction shape
python tools/gen_simple.py      # (re)write src/gen/ from the original; then check, then --prune
python tools/diff.py 004b32e8   # original vs compiled, side by side (after check.py)
python tools/lift.py            # lift functions to src/lift/; then check, then
                                # "lift.py --prune build/check.log" (repeat until stable)
sh tools/lift_all.sh [python]   # all of the above until nothing more is pruned (15-30 min)
sh tools/build_all.sh [python]  # gen_simple (pruned) and then lift_all: regenerate everything
sh tools/permute_all.sh [python] # unpruned check, permute.py, classgen.py, lift_all (about 45 min)
python tools/classgen.py        # constructors as C++ classes -> src/cls/, config/symbols_cls.txt
                                # (needs build/vtables.json from tools/classes.py; then lift_all)
python tools/permute.py LOG     # try other statement orders and if/else inversions -> src/perm/
                                # (LOG: check -v of UNPRUNED lift output; then rerun lift_all)
python tools/lift.py --one 10764  # print one lifted function (and why it failed, if it did)
```

To match a function by hand:
1. Read its code: `tools/ppu-gcc/bin/ppu-lv2-objdump -D --start-address=0x... --stop-address=0x... orig/EBOOT.elf`.
   On Windows, run it in Docker as `tools/check.py` does.
2. Write C/C++ in `src/<category>/<unit>.cpp`.
3. Map it in `config/symbols.txt`: the address and the compiled code symbol
   (`.` + mangled name). Functions named `fn_<8 hex digits>` with C linkage
   map themselves.
4. Run `python tools/check.py -v` until it prints `MATCH`.

The checker masks only linker-filled fields:
- REL24 and REL14 branch targets, and 16-bit ADDR16/TOC16 immediates;
- a `nop` in the object where the original has `ld r2,40(r1)` right after a
  call: a TOC restore the linker inserted for a call into another TOC group.

## Binary facts

| Item | Value |
| --- | --- |
| `.text` | `0x10240`–`0xfc983c` (16.5 MB) |
| Functions | 55,164 in `.text` (`config/functions.txt`: boundaries from the recomp's analysis; 886 nop-only padding entries dropped, 73 stray `ld r2,40(r1)` words joined to the function before them). 2,885 of them are linker TOC stubs (`config/linker_stubs.txt`), which `load_functions()` leaves out. 841 entries were the ends of exception cleanups (a short block ending in `bl _Unwind_Resume`, 0xb383b8) split off their function, and 594 more were other pieces of the function before them (no descriptor, never called, reached by a conditional branch from that function: e.g. `fn_0002ba70`, the `__cxa_call_unexpected` path of an exception specification); `tools/merge_pieces.py` finds them. All are merged into their function: 50,844 count. |
| OPD (function descriptors) | `0x12d9b90`, size `0x72388`: `{u32 code, u32 toc}` |
| TOC bases | `0x1353f18`, `0x1363ee8`, `0x1373ee0` (multi-TOC) |
| ABI | lv2: 64-bit registers, 32-bit pointers (`sizeof(void*) == 4`) |

- **RTTI:** 1,032 classes with vtables. A vtable is `{offset_to_top, type_info*, vfuncs…}`,
  and an object's vptr points at the first vfunc. A type_info is
  `{vptr, name*}`, with names mangled like `10CCharacter`.
- **Source paths:** the game's assert strings keep original source paths:
  - game: `/project/PS3/shirokishi/program_usa2_release/src/...`
  - Level-5 engine: `/project/PS3/lv5lib/level5_lib310/engine/build_mxlib/mxlib/source/mx_*.cpp`
  - SGX sound: `/prj/sgx/newsgsys/source/...`
  - libraries: Havok (`hk*`), Sony Edge zlib, and Dinkumware (`dinkum-pu`)

## Units

`tools/make_units.py` labels functions by class: vtable entries, plus
constructors and destructors through the vtable pointers they load (it takes
the most-derived class). It puts boundaries where a run of functions' TOC
entries stop overlapping the previous run's. Known weak spots:
- Base classes with many derived classes, such as `CGraphicMenuObject`, still
  appear as many separate units.
- Code without classes (C code: SGX, Dinkumware) is split by TOC only.

Rename units to their real source files as they are identified, and edit
`config/units.txt` by hand.

## Pitfalls

- Near-miss tools need every lifted function to compile. In an unpruned
  check about 740 files failed (so all their functions were invisible to
  `permute.py` and `nearmiss.py`); `permute_all.sh` now repeats
  `lift.py --prune-errors` (drops only what fails to compile) before the
  search. Loop and merge variables keep their declared type in updates
  (`declared()`), which removed the most common error.
- Expressions longer than 4,000 characters fail the lift ("expression too
  large"): nested copies of one expression grew until Python ran out of
  memory, which silently stopped `permute_all.sh` (its `&&` chains skip the
  rest). If a pipeline step's count does not change at all, run that step by
  hand and read its traceback.

- Do not edit `tools/lift.py` (or the other tools a pipeline calls) while
  `lift_all.sh` / `permute_all.sh` runs: each round re-runs the scripts.
- Docker Desktop sometimes stops (and Windows updates restart the machine).
  `docker info` failing means start Docker Desktop again; a crash during a
  write can leave files full of zero bytes (check `git status`, delete and
  regenerate them).
- Every progress push: run the pipeline to convergence, compare the matched
  set with the last commit (lost/gained), then `tools/progress_map.py`, the
  README/HANDOFF numbers, commit as WK2D and push.

- Write text files as UTF-8 explicitly. Windows defaults broke the SVG once.
- Git on Windows: `core.autocrlf=false`. The compiler sources must have LF
  line endings or `configure` breaks.
- The report is about 7 MB. Commit it compact; it is regenerated each time.
- CI has no EBOOT. It only compiles (`--compile-only`) and uploads the
  committed report as the `BLUS30784_report` artifact. Regenerate the report
  locally before committing progress.

## Next steps

Switch statements (done 2026-10-23, `lift.py`): GCC compiles `switch` to a
jump table: `cmplwi x,N-1; ble table` (bounds check; the other side is
`default`), `lwz rT,<toc>(r2)`, `rlwinm rI,x,2,...`, `lwzx r0,rI,rT`,
`extsw`, `add r0,r0,rT`, `mtctr r0`, `bctr`, then N words right after the
`bctr`: offsets from the table's start to each case block (cases share
blocks). `jump_table()` reads them (up to the `cmplwi` bound), marks them as
data, and makes the targets block leaders; `switch_region()` lifts each case
to the common join and merges the paths. Cases are emitted in the order
their blocks sit in memory: GCC lays case bodies out in source order (in
`fn_00039ba0` `case 2: case 3:` comes before `case 0: case 1:`). Fall-through
between cases is not handled (fail). Float-to-int conversions through the
red zone (`addi r9,r1,-16; fctiwz; stfiwx f0,0,r9; lbz r3,-13(r1)`) lift as
`(u8)(f)` etc.

After that, by size (`lift.py` failure reasons, `build/why_all.py`-style
counts over all functions up to 384 instructions):
1. Loops GCC restructured (about 3,800: "branch out of a loop body", "jump
   into a loop body"): early exits that jump threading moved. Needs a real
   structuring algorithm; goto does not reproduce GCC's own loop rotation.
2. Constructors with member objects and destructors (see the C++ notes
   above): structure reproduced, store scheduling not yet.
3. (Done: the "frameless functions ending in a call that never returns"
   were pieces of the function before them, the exception-specification
   path; merged, see Binary facts.)
4. Identify real source files and rename units (assert-string paths, class
   names from RTTI).
5. Submit the repository on bytematch.dev (the user signs in).

## Tools for matching by hand

- `python tools/try.py build/try/x.cpp [-d] [-O2 ...]` compiles one file and
  compares its `fn_<addr>` functions (declare them `extern "C"`): the fast loop
  for trying idioms. `-d` prints a side-by-side diff.
- `python tools/lift.py --one <addr>` prints one lifted function: a starting
  point for rewriting it as natural C.
- `WKC2_CFLAGS="..." python tools/check.py -v` runs the corpus with other flags.
- The lifter now writes counted (`bdnz`) loops as `for (i = 0; i < n; i++)`.
  A register that starts at 0 and only steps by a constant becomes
  `i * stride`, addresses become `FIELD(T, (char*)base + i * stride, off)`,
  and GCC's zero-trip guard (`if (n > 0)` around the loop) is dropped. Dead
  merge-variable assignments are removed (`drop_dead_vars`): left in, they
  change GCC's induction-variable choice.
- Backward branches whose target cannot reach them again are not loops:
  GCC placed a shared tail (usually the epilogue) earlier and jumps back to
  it. The lifter lifts that tail inline as a return path (`back_returns`).
  This alone removed about 4,200 "jump into a loop body" rejections.
- Struct layouts are keyed by access path: `(k,)` argument register k,
  `(k, d)` the pointer loaded from offset d of it, `('g', d)` the global at
  TOC entry d. Pointer fields with a layout become typed members, so the
  lifter writes `a0->f_0->f_1a8` instead of `FIELD(float, a0->f_0, 424)`.
  GCC schedules the two differently; only the typed form matches.
- int/float conversions go through the stack red zone (`std; lfd; fcfid;
  frsp` and `fctidz/fctiwz; stfd; ld/lwz`). `self.slots` follows them and
  renders `(float)(u32)x` and `(u32)f`. `(float)(double)(u32)x` does not match.
- Call arguments stop at a register a call clobbered (`made_call`): the
  callee's apparent read of it is not a real argument. TOC-switching linker
  stubs (`std r2,40(r1); addis/addi r2; b f`) take their arity from `f`
  (`arity_finder`).
- Call arity: `reads_before_writes` follows every path of the callee up to
  its first call (`register_use` decodes each instruction) and reports
  whether it met anything unknown. A complete scan is trusted, extended only
  by registers the caller set since the last loop boundary (`bfresh`) (callees often ignore a
  trailing argument such as 0 that callers still pass). An incomplete scan,
  or a system-library import stub, is extended by any consecutive registers
  the caller wrote since the last call.
  This fixed about 1,100 near-misses at once (`tools/nearmiss.py` sorts
  near-misses by their first differing instruction; run it on an unpruned
  check log).
- A member's address is written `&p->f_8`, not `(char*)p + 8`: offsets whose
  address is taken (`addressed`) get a byte member when nothing overlaps.
- Open near-misses that need real C++ classes: derived destructors that set
  the vtable pointer and call the base destructor (`fn_00bd5fbc`), and
  `fn_00bd5fbc` loads the vtable address into r11 before the prologue;
  `struct D : B { virtual ~D(); }; D::~D() {}` gives the same instructions
  but loads into r0 after it, with every -O level and scheduling flag tried.
- Open: about 400 near-misses start with `clrldi rX,r3,32` on an incoming
  argument used as a pointer (e.g. `fn_00017acc`). A `u32` parameter cast
  to a pointer gives no clrldi; an `int` one does, but the rest differs
  (the original reloads a field after a compare, which suggests a
  `min()`-style helper taking references). Not solved yet.
  (Non-virtual thunks, `addi r3,r3,-N; b f`, are done: gen_simple writes a
  class whose second base sits at offset N, with the override in its own
  section so the thunk's branch keeps a relocation. All 204 match.)
- Indexed (`lwzx` and family) and update-form (`lwzu`, `stwu`, ...)
  accesses lift through the shared `load`/`store`/`sum` helpers.
- Per file, every lifted function is kept: a function that calls another
  function of the file calls a placeholder `stub_X` (a different name from
  the callee's `fn_X`), and call targets are masked. (Until 2026-10-23 a
  function and one it called were never kept together, and functions whose
  callee declarations disagreed were dropped; alias declarations and this
  change recovered about 280 matches and more.) An external call (`bl; nop`)
  to a function the same file defines goes through an alias declaration,
  so its prototype cannot clash with the definition.
- Statement order: GCC schedules stores by its own rules, so the order the
  instructions appear in is often not the source order that produces them.
  `tools/permute.py` tries other orders of each run of consecutive
  assignments (all orders up to 4, then swaps and rotations; 48 variants a
  function), compiles every variant and keeps matches in `src/perm/`, which
  `lift.py` then skips. The first run matched 496 functions from 35,070
  variants in about 5 minutes. Inverting if/else (one at a time and all
  together, each with a few reorders) found only 36 more from 48,108 variants; swapping comparison operands
  found 1 from 67,044 (GCC puts compare operands in its own order).
  It also tries each function as `void` (`voided`): the lifter takes whatever
  r3 holds at a return for a result, but often the source returned nothing
  and r3 held a leftover (`fn_0001c828` went from 17% to 96% that way).
- C++ constructors: a store of a vtable pointer (a TOC word that is a
  vtable address from RTTI, build/vtables.json; the stored value is the
  listed address itself) into `this` at offset 0 marks a constructor.
  Written as a plain store it never matches; as a real constructor it does,
  because GCC schedules the vtable store with the member stores.
  `tools/classgen.py` turns the lifted function into `unk_X_a0::unk_X_a0()`
  of a generated class (`virtual void vf0();`, fields after the vtable
  pointer), tries member stores in offset order first (declaration order,
  as programmers write constructors) and other orders, compiles, and keeps
  matches; the symbol is the base-object constructor `C2`. A call on `this`
  before the vtable store becomes a base class (defined `noinline` in the
  file when the original call was local). Constructors that build member
  objects (stores through `this + N`) need embedded member structs, which
  the lifter does not model yet. Evidence: the game computes `this + N` once and
  stores through it (`FIELD(u32, (&a0->f_4), 12)` matches); flattening such
  accesses into the containing struct (`a0->f_10`) lost 51 matches, so the
  lifter keeps a member's address as its own base. Accesses through a local's
  address are flattened (`l.f_9`), which is neutral.
- Exceptions are on (the game is C++ with EH). A constructor whose later
  member construction can throw ends in a cleanup block that destroys the
  base and calls `_Unwind_Resume`; GCC emits the same block when the base
  class has a destructor, so classgen declares `virtual ~X_base();` when the
  original calls `_Unwind_Resume` (76 constructors match this way and
  without). Merging the cleanup tails into their functions (see Binary
  facts) was needed first; it cost 4 earlier matches whose tails had been
  cut off.
- Member objects: classgen also tries a form where each call on
  `&this->f_N` becomes a member of a class with that constructor (run
  implicitly, in declaration order) and plain member stores move to the
  initializer list (`with_members`). It matched nothing new yet. Hand test on
  `fn_000cb5ec` (base, a member with an out-of-line constructor, three 8-byte
  members zeroed inline, more u32 members): the right frame, calls and
  exception cleanup, 77% identical; only the scheduling of the last stores
  differs, and no ordering of initializer list, body and inline member
  constructors tried matched it.
- Destructors are still open: `struct D : B { virtual ~D(); }; D::~D() {}`
  gives the right instructions for `fn_00bd5fbc` but loads the vtable after
  the prologue (the game loads it into r11 before), with every flag tried.
- Function descriptors (OPD): 189 functions have several descriptors with
  different TOC bases; the right one is the base most of them give
  (`classgen.toc_bases`). lift.py's constant reader still takes the last.
- AltiVec (VMX) is on by default in this GCC (`__ALTIVEC__`); `wkc2.h`
  includes `<altivec.h>` and defines `vec` (`vector float`). The lifter keeps
  vector registers (`vr`, parameters `q0..` in v2.., results in v2), lifts
  `lvx`/`stvx` (a constant index register becomes a field offset, so struct
  members are `vec`), `stvewx` (`vec_ste`), and `vxor` (zero:
  `(vector float){0, 0, 0, 0}`), `vaddfp`, `vsubfp`, `vmaddfp`, `vnmsubfp`,
  `vsel`, `vperm`, `vsldoi`, `vspltw`, `vspltisw`, logic ops, min/max,
  `vrefp`, `vrsqrtefp`, vector copies (`vor`/`vsldoi` of one register),
  callee-saved vector saves (`li r0,N; stvx v31,r1,r0`) and vector call
  arguments (after the integer ones). Most remaining vector functions are
  larger matrix code that lifts but does not match yet.
- Tried and reverted: writing `(char*)(t0) + 16` for arithmetic on a call's
  result (so `t0 + 16` compiles when t0 becomes `void*`). It lost 13
  matches and made GCC 4.1 crash (tree-vrp.c:415) on `fn_0044dae0`.
- Thread-local variables: `addis rX,r13,ha; addi rY,rX,lo` is the address
  of a `__thread` variable defined in the same file (GCC then emits exactly
  that, with TPREL16_HA/LO relocations, which check.py masks). An
  `extern __thread` would use the GOT instead.
- Also lifted: callee-saved FPR saves/restores (`stfd/lfd f14-f31` on r1),
  `std` to memory (u64 fields), `stfiwx` (float-to-int stores, including
  the conversion through a local), `cntlzw` (`cntlzw; srwi 5` is `x == 0`),
  `trap` (`__builtin_trap()`), `subfic`.
- Not lifted yet, by size: about 500 entries of config/functions.txt that are pieces of
  another function (they read registers like r29 that their parent set).
- Return paths with different integer or pointer types share one return
  type (`int`, or `void*` for pointers and words), each return cast.
- Mixed integer and float parameters: a float takes the place of an integer
  register too, so integer parameters the code never reads (`read_args`) are
  where the floats go.
- Stack slots used with two types get no struct: the locals become
  `char l[N]` accessed by offset.
- `drop_dead_vars` tokenizes each line once (it was quadratic and took over
  10 minutes on large functions).
- Joins keep the comparisons every path made the same way (GCC reuses a
  condition field across a join); `merge()` takes any number of paths.
- `rlwinm` covers every form: shifts, masks, fields taken from the rotated-in
  bits (a right shift), wrap-around masks and real rotates
  (`((x << n) | (x >> 32 - n)) & mask`).
- The condition register save (`mfcr r12; stw r12,8(r1)` ... `lwz; mtcrf`) is
  ignored: it belongs to the prologue of functions that keep comparisons in
  cr2-cr4 across calls.
- Not lifted yet: 64-bit composition (`rldicr rX,rY,32,31; rldimi`, about
  470 functions: small structs passed in one register), varargs functions
  (the prologue stores r4-r10: "spill", about 700), `mftb` (time base),
  conditional traps (`tw`).
- Float comparisons `<=`/`>=` combine two condition bits:
  `fcmpu cr7,a,b; cror 4*cr7+eq,4*cr7+gt,4*cr7+eq; beq` is `a >= b`
  (`crbits`; a combined bit is used only while its comparison is current).
- Calls return floats in f1 (the call's result type becomes `float` when f1
  is read).
- Switch: when a case leaves the switch into the default's block, that block
  is the code after the switch (no default body).
- A callee called with different argument counts (in one function, or by
  two functions of one file) gets alias declarations `fn_X_2`, `fn_X_3`
  (`callee_name`): the call target is a masked relocation, so the name does
  not change the bytes. Call keys are the address, or (address, k) for an
  alias (`callee_address`).
- Bool bitfields: the game's flags are `bool x : 1` (a `u8` bitfield
  compiles differently). Reading one is `lbz; rldicl r0,r0,64-p,63`
  (`rldicl r0,r0,62,63` is bit 2 from the bottom); setting it is
  `lbz; ori r0,r0,1<<p; stb` and clearing it `lbz; li rX,~(1<<p); and; stb`.
  `wkc2.h` defines `BF8` (eight `bool` bitfields, most significant first;
  C++ only) and the lifter writes `FIELD(BF8, p, off).b2`. Found from the
  near-miss analysis: the original had `rldicl` where ours had `rlwinm` in
  about 700 near-misses. Wider containers (`lhz`/`lwz`) are not handled yet.
  It gained no matches by itself (those functions differ in other ways too;
  a bit test against 0 compiles the same either way), and bytes of stack
  locals are left alone.
- Prologue saves of r29 where ours saves r31 (about 700 near-misses): the
  original kept r30/r31 for variables the optimiser later removed; not
  reproducible from the machine code alone.
- Out-of-line loop blocks: GCC moves an if's then-block out of a loop to the
  end of the function; it jumps back into the loop (`fn_0001c828`). A
  backward branch from after a loop's latch into that loop is recorded in
  `outlined_ret` (not a loop; only between real loops, cycles found with
  `reach_all`: jumps back to a shared return tail are not loops); `successors()` follows it, and the loop's
  written registers include `outlined_regions`.
- Loops ending in `cmpwi rI,C; bne` with rI an induction register become
  `for (i = 0; i < N; i++)` like counted loops (`compare_trips`: N = C/S + 1
  when the compare reads rI before its step, C/S after). Before this the
  lifter tested the stepped value (`do { ...; v += S; } while (v != C)`),
  one iteration short when the game compares the old value.
- `loop()` tells a `bdnz` latch by its BO bits; it used to evaluate the
  latch condition at the loop's entry and fail ("condition field never set").
- Locals are padded to the original's size (`locals_size`: frame minus 112
  minus 8 per saved GPR/FPR in the prologue): buffers the code only passes
  by address were otherwise sized by the bytes it touches (`fn_00010d68`:
  1,200-byte frame, ours 192). Only shortfalls of 64 bytes or more are
  padded: GCC leaves gaps before the register saves, and padding every
  shortfall lost 86 matches.
- Call arguments: registers the caller filled after the callee's own
  argument count are still passed (an ignored trailing 0 is real), but not
  when the value is part of another argument: that is scratch from an
  address chain (`r9 = g->a; r10 = r9->b; r3 = r10->c`, `fn_006a130c`).
  Passing it kept the value alive across the call and saved an extra
  register. Functions that set only f1 before returning return `float`
  or `double` (`lfs f1,d(r2); blr`, `fn_00014bf8`); they used to lift as
  empty void functions.
- Stack locals read and written with two types at one offset (an integer
  store of 0 into a float that a callee fills, `fn_0014e6d8`) are a union
  member (`union { float v_float; u32 v_u32; } f_0;`, `l.f_0.v_u32`). The
  raw `char l[N]` fallback made GCC keep `&l` in a saved register; still
  used when slots of different offsets overlap.
- Float parameters passed on unchanged to a callee that reads them (the
  caller sets only f2: `lfs f2,..; b g`, `fn_0001ce7c`) are passed, after
  the integer arguments, but only those before a float the caller sets:
  passing trailing unchanged ones too lost 7 matches (they add parameters
  that shift the integer ones).
- `extsw` of an untouched parameter means the parameter is unsigned (`u32`;
  the ABI already sign-extends `int` parameters): `fn_0003a128`.
- Do not edit `tools/lift.py` while `permute_all.sh` runs: every step
  re-runs it, so the result mixes versions.
- Stopping a background `permute_all.sh` from the agent's task tool can
  leave its `sh` children running (Git Bash on Windows). Before starting
  another run, check for leftover `permute_all.sh`/`tools/*.py` processes
  and `ubuntu:14.04` containers you started; two runs at once wreck each
  other's objects (a collision showed 10,006 matches instead of 17,760).
- Values read from memory and kept in a callee-saved register (r14-r31,
  f14-f31) across a call were read before the call (it may change memory):
  `hold_loads()` makes them a variable assigned just before the call
  (`fn_0001ea70`). The lifter used to repeat the read after the call.
  This one change gained 870 matches (17,783 to 18,653). Virtual and
  function-pointer calls do the same, leaving the called object alone
  (`vcall` compares `this` with it by identity).
- Branch layout: GCC predicts a pointer tested against 0 to be non-null,
  which decides which block it lays out first (`fn_000163f4`). A cast to a
  pointer is folded away and does not count; `*(void**)&(a0->f_0) != 0`
  does. `permute.py` tries that form for every `if` testing a memory word
  against 0 (`pointer_tests`).
- Structs returned by value: the caller passes the result's address in
  r3, the function zero-extends it once (`clrldi r29,r3,32`) and returns
  it (`mr r3,r29`). Lifted as `void* f(S* a0, ...) { ...; return a0; }` it
  does not match; `S f(...) { S r_; S* a0 = &r_; ...; return r_; }` does
  (`fn_00417f58`). `permute.py` tries this form (`struct_return`) for
  functions that return their first, struct-pointer parameter.
- `permute.py` tries only the cheap whole-function forms (struct return,
  void, all pointer tests) on functions over 48 words that are at least
  20% alike (`BIG_FUZZY`); that gained 63. A full cycle takes about 35
  minutes.
- `permute.py` tries every order of runs of up to 5 assignments (120;
  before: up to 4) and puts the other forms first (`MAX_EXTRAS`). Net +10
  for about 5 more minutes per cycle: store order is rarely the last
  difference.
- `srawi t,x,31; xor y,t,x; subf z,t,y` is GCC's `abs(x)` (`subf z,y,t`
  is `-abs(x)`): lifted as `__builtin_abs(x)` (`fn_00018b44`,
  `fn_00b67514`). Written out as shifts and xor, GCC reorders the operands.
  About 880 near-misses contain `srawi x,31`; this gained 365.
- Float comparisons: the opposite of `a > b` is `!(a > b)`, not `a <= b`
  (NaN). GCC compiles `a <= b` with `cror` and `!(a > b)` as one branch, so
  `condition()` writes negated float tests as `!(...)` and `negate()` keeps
  them that way (`FLOAT_CONDS` holds the float comparison texts)
  (`fn_001293bc`, which then matches as `return a0->f_168 > 35.0f;`).
- Not solved yet: `(p[0]<<24)|(p[1]<<16)|(p[2]<<8)|p[3]` byte assembly
  (`fn_0003de80`): GCC reassociates our version whatever the types; the
  original ors the first three before loading the fourth byte. Destructor
  vtable stores (`fn_00f9dd40`): the original loads the vtable address
  before the prologue.
- Quick single-function test: `python tools/lift.py --one ADDR --file >
  build/try/x.cpp` then `python tools/try.py build/try/x.cpp -d`.
- Editing tools from the agent's shell: heredocs passed through the Bash
  tool can lose backslashes (`\n`, `\b` arrived as a newline and a
  backspace character). Use the file edit tool for lines with escapes, and
  check with `grep -c $'\x08'`.
- Near-miss tools: `build/check.log` holds only matches after the final
  pruning; for near-misses run the unpruned lift and check (first lines of
  `permute_all.sh`) and use `build/check_unpruned.log` before anything
  rebuilds the objects.
- Complete near-miss picture (2026-10-23, 15,000 near-misses with nothing
  hidden): the first difference is the prologue in about 3,600; the
  original saves more callee-saved registers in about 3,400 (variables the
  optimiser removed), fewer in about 2,100 (our code keeps values live
  across calls where the game reloads them), other registers in about
  1,600, and only the frame size differs in about 900.
- Calls through function pointers: `lwz r0,0(rX); std r2,40(r1); mtctr r0;
  lwz r2,4(rX); bctrl` with rX not a vtable slot is `((T (*)(...))(x))(...)`
  (rX points to the callee's descriptor). Weak-symbol tests (`if (&f)`) are
  folded away by GCC for ordinary globals; they would need `__attribute__((weak))`.
- Loops: writing the game's (already rotated) loops with goto does not give
  the same code as the original for loops: GCC rotates loops itself early.
  The remaining loop failures need real structuring (break/continue/early
  exits that GCC's jump threading moved).
- Tail calls (`b` to another function) lift as `return stub_X(...)`: GCC
  only tail-calls functions sharing the TOC, which means a static callee in
  the same file.
- Float and double literals that GCC keeps in the TOC (`lfs f0,d(r2)`) are
  read through the function's descriptor (OPD) and written as literals.
- `rldicl` bit-field forms on 32-bit values lift as shifts and masks.
- A step budget (16 x the function's length) stops inlined shared tails from
  multiplying without end (`fn_008e92c4` has a ladder of them).
- Open: about 870 small functions end in a call with no return and no stack
  frame (`clrldi r3,r3,32; bl f; ld r2,40(r1)`, e.g. `fn_0002ba70`). A
  `noreturn` callee still makes GCC 4.1 build a frame and save LR, so the
  source form is unknown yet.
- Stack locals: the frame's locals (offsets from 112, after the 48-byte
  linkage area and 64-byte parameter save area) become one local struct
  `l`. `addi rX,r1,112` is `&l`; loads and stores through r1 are `l.f_N`.
  A plain `int x = 0; f(a, b, &x);` compiles the same way.
- Still open for loops: bases that are globals need a struct type (an
  `extern char g[]` base picks other induction variables; see `fn_00a25abc`,
  which matches by hand in `src/game/auto_00a25abc.cpp`), pointer-stepping
  loops (a register that starts at a pointer), and indexed loads (`lwzx`).
- Lesson from loops: write what a programmer would write (`s->a[i]`, plain
  `for` loops, real structs with arrays). The lifter's byte-offset style
  (`FIELD(u32, (char*)a0 + v2, 896)`) compiles to different induction
  variables and rarely matches.
