"""Shared helpers: project paths, configuration, the original EBOOT and ELF objects."""
import hashlib
import os
import struct
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ORIG = ROOT / 'orig' / 'EBOOT.elf'
ORIG_SHA1 = '92198149c562c676815acb560e7b9861b894abd6'   # BLUS30784 v01.00, decrypted
BUILD = ROOT / 'build'
COMPILER = ROOT / 'tools' / 'ppu-gcc'                     # built by tools/build_compiler.sh
# Code generation options: natural C loops match with plain -O2. Files that
# need other flags are listed in config/flags.txt.
CFLAGS = os.environ.get('WKC2_CFLAGS', '-O2').split()   # override for experiments; per file: config/flags.txt


def load_functions():
    """{address: size} for every function in the original .text, without the
    linker's TOC stubs (config/linker_stubs.txt), which no source produces."""
    stubs = set()
    path = ROOT / 'config' / 'linker_stubs.txt'
    if path.exists():
        stubs = {int(l.split()[0], 16) for l in path.read_text().splitlines() if l and not l.startswith('#')}
    out = {}
    for line in (ROOT / 'config' / 'functions.txt').read_text().splitlines():
        if line and not line.startswith('#'):
            a, s = line.split()
            if int(a, 16) not in stubs:
                out[int(a, 16)] = int(s, 16)
    return out


def load_flags():
    """[(source path pattern, flags)] from config/flags.txt: files built with
    other flags than CFLAGS. The first matching pattern wins."""
    out = []
    path = ROOT / 'config' / 'flags.txt'
    if path.exists():
        for line in path.read_text().splitlines():
            words = line.split('#', 1)[0].split()
            if words:
                out.append((words[0], words[1:]))
    return out


def load_units():
    """[(name, start, end, category)] in address order."""
    units = []
    for line in (ROOT / 'config' / 'units.txt').read_text().splitlines():
        if line and not line.startswith('#'):
            name, start, end, category = line.split()
            units.append((name, int(start, 16), int(end, 16), category))
    return units


def load_symbols():
    """{object symbol: address}: which compiled function stands for which
    original. config/symbols.txt is written by hand, symbols_gen.txt by
    tools/gen_simple.py."""
    out = {}
    for path in (ROOT / 'config' / 'symbols.txt', ROOT / 'config' / 'symbols_gen.txt'):
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.split('#', 1)[0].strip()
            if line:
                a, name = line.split()
                out[name] = int(a, 16)
    return out


class Original:
    """The user's decrypted EBOOT, checked against the expected SHA-1."""

    def __init__(self, path=ORIG):
        if not path.exists():
            raise SystemExit(f'{path} is missing: copy your decrypted EBOOT.elf there (see orig/README.md)')
        self.data = path.read_bytes()
        digest = hashlib.sha1(self.data).hexdigest()
        if digest != ORIG_SHA1:
            raise SystemExit(f'{path} has SHA-1 {digest}, expected {ORIG_SHA1}')
        d = self.data
        phoff, = struct.unpack_from('>Q', d, 0x20)
        phnum, = struct.unpack_from('>H', d, 0x38)
        self.segments = []
        for i in range(phnum):
            p_type, _, off, va, _, filesz, _, _ = struct.unpack_from('>IIQQQQQQ', d, phoff + i * 56)
            if p_type == 1 and filesz:
                self.segments.append((va, off, filesz))

    def read(self, address, size):
        for va, off, filesz in self.segments:
            if va <= address and address + size <= va + filesz:
                return self.data[off + address - va:off + address - va + size]
        raise ValueError(f'{address:#x}+{size:#x} is not in a loaded segment')


class Object:
    """The parts of a big-endian ELF64 relocatable object the checker needs."""

    def __init__(self, path):
        d = self.data = Path(path).read_bytes()
        shoff, = struct.unpack_from('>Q', d, 0x28)
        shnum, shstrndx = struct.unpack_from('>HH', d, 0x3C)
        raw = [struct.unpack_from('>IIQQQQIIQQ', d, shoff + i * 64) for i in range(shnum)]
        names = raw[shstrndx]
        def string(table, offset):
            start = table[4] + offset
            return d[start:d.index(b'\0', start)].decode()
        self.sections = [dict(name=string(names, s[0]), type=s[1], offset=s[4], size=s[5],
                              link=s[6], info=s[7]) for s in raw]
        self.symbols = []
        for s in self.sections:
            if s['type'] == 2:   # SHT_SYMTAB
                strtab = raw[s['link']]
                for i in range(s['size'] // 24):
                    st_name, st_info, _, st_shndx, st_value, st_size = struct.unpack_from(
                        '>IBBHQQ', d, s['offset'] + i * 24)
                    self.symbols.append(dict(name=string(strtab, st_name), type=st_info & 15,
                                             section=st_shndx, value=st_value, size=st_size))
        self.relocations = {}   # section index -> [(offset, type, symbol)]
        for s in self.sections:
            if s['type'] == 4:   # SHT_RELA
                rel = self.relocations.setdefault(s['info'], [])
                for i in range(s['size'] // 24):
                    r_offset, r_info, _ = struct.unpack_from('>QQq', d, s['offset'] + i * 24)
                    rel.append((r_offset, r_info & 0xFFFFFFFF, self.symbols[r_info >> 32]['name']))

    def functions(self):
        """[(name, section index, offset, size)] for code entry symbols (".name")."""
        return [(s['name'], s['section'], s['value'], s['size']) for s in self.symbols
                if s['type'] == 2 and s['name'].startswith('.') and s['size']]

    def section_bytes(self, index):
        s = self.sections[index]
        return self.data[s['offset']:s['offset'] + s['size']]


def compiler_command():
    """How to run the PPU compiler: natively on Linux, through Docker elsewhere."""
    gxx = COMPILER / 'bin' / 'ppu-lv2-g++'
    base = ['-B' + str(COMPILER / 'libexec' / 'gcc' / 'ppu-lv2' / '4.1.1') + '/', '-B' + str(COMPILER / 'bin' / 'ppu-lv2-')]
    if os.name != 'nt':
        return lambda args: [str(gxx)] + base + args
    image = os.environ.get('WKC2_CC_IMAGE', 'ubuntu:14.04')
    def docker(args):
        inner = ['/c/bin/ppu-lv2-g++', '-B/c/libexec/gcc/ppu-lv2/4.1.1/', '-B/c/bin/ppu-lv2-'] + args
        return ['docker', 'run', '--rm', '-v', f'{COMPILER}:/c:ro', '-v', f'{ROOT}:/w', '-w', '/w', image] + inner
    return docker


def run_compiler(source, output, flags):
    """Compile one source file (paths relative to the repository root)."""
    cmd = compiler_command()([*flags, '-Iinclude', '-c', source.as_posix(), '-o', output.as_posix()])
    env = dict(os.environ, MSYS_NO_PATHCONV='1')
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env=env)
    return result.returncode, result.stdout + result.stderr
