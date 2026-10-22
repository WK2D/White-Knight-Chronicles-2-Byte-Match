#!/bin/sh
# Build Sony's PPU toolchain (GCC 4.1.1 + binutils 2.17, GPL sources from
# sagemono/ps3gcc) far enough to compile and assemble: as, cc1, cc1plus and
# the gcc/g++ drivers. No libgcc or C++ runtime (those need the SDK's target
# headers, and matching only needs object files).
# usage (inside the build container, see build_compiler.sh): build_ppu_inner.sh <source dir> <install prefix>
set -e
SRC=$1; PREFIX=$2
JOBS=$(nproc)
# GCC 4.1 predates C99 inline semantics; newer host GCCs need gnu89 inline.
# It goes in CC, not CFLAGS: CFLAGS also reaches the new compiler's own
# target files, and GCC 4.1.1 does not know the option.
export CC="gcc -fgnu89-inline" CFLAGS="-O2 -w"
mkdir -p /build/binutils /build/gcc
cd /build/binutils
[ -x "$PREFIX/bin/ppu-lv2-as" ] || "$SRC/binutils/configure" --target=ppu-lv2 --prefix="$PREFIX" --disable-nls --disable-werror >/build/binutils.configure.log 2>&1
[ -x "$PREFIX/bin/ppu-lv2-as" ] || make -j"$JOBS" MAKEINFO=true >/build/binutils.make.log 2>&1
[ -x "$PREFIX/bin/ppu-lv2-as" ] || make install MAKEINFO=true >/build/binutils.install.log 2>&1
export PATH="$PREFIX/bin:$PATH"
cd /build/gcc
"$SRC/gcc/configure" --target=ppu-lv2 --prefix="$PREFIX" --disable-shared --disable-nls \
    --enable-languages=c,c++ --without-headers --disable-libssp --disable-threads \
    >/build/gcc.configure.log 2>&1
make -j"$JOBS" all-build all-libcpp all-libiberty configure-gcc MAKEINFO=true >/build/gcc.make.log 2>&1 || true
# Only the compiler programs and GCC's own headers: libgcc needs target headers.
make -C gcc -j"$JOBS" xgcc cpp cc1 cc1plus g++ stmp-int-hdrs MAKEINFO=true >>/build/gcc.make.log 2>&1
LIBEXEC="$PREFIX/libexec/gcc/ppu-lv2/4.1.1"; LIB="$PREFIX/lib/gcc/ppu-lv2/4.1.1"
mkdir -p "$PREFIX/bin" "$LIBEXEC" "$LIB"
cp gcc/xgcc "$PREFIX/bin/ppu-lv2-gcc"; cp gcc/g++ "$PREFIX/bin/ppu-lv2-g++"; cp gcc/cpp "$PREFIX/bin/ppu-lv2-cpp"
cp gcc/cc1 gcc/cc1plus "$LIBEXEC/"
rm -rf "$LIB/include"; cp -r gcc/include "$LIB/include"
"$PREFIX/bin/ppu-lv2-gcc" --version | head -1
