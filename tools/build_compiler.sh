#!/bin/sh
# Build the PS3 PPU compiler the game was built with: GCC 4.1.1 + binutils
# 2.17 from Sony's GPL toolchain source package for SDK 3.1.0, preserved at
# https://github.com/sagemono/ps3gcc (branch toolchain-src-3.1.0-GCC411).
# The game's own compiler reports "4.1.1 (SDK310, $Rev: 3320 $)"; this
# package is $Rev: 3364 $ of the same SDK release.
#
# Installs into tools/ppu-gcc. Needs Docker (any OS); the build runs in an
# Ubuntu 14.04 container because GCC 4.1.1 needs an old host compiler.
set -e
cd "$(dirname "$0")/.."
ROOT=$(pwd)
SRC="$ROOT/build/ps3gcc-src"
if [ ! -d "$SRC/gcc" ]; then
    git -c core.autocrlf=false clone --depth 1 --single-branch \
        --branch toolchain-src-3.1.0-GCC411 https://github.com/sagemono/ps3gcc.git "$SRC"
fi
mkdir -p "$ROOT/tools/ppu-gcc"
MSYS_NO_PATHCONV=1 docker run --rm \
    -v "$SRC:/src:ro" -v "$ROOT/tools/build_ppu_inner.sh:/build.sh:ro" \
    -v "$ROOT/tools/ppu-gcc:/opt/ppu" ubuntu:14.04 sh -c \
    "apt-get update >/dev/null && apt-get install -y build-essential bison flex m4 >/dev/null && sh /build.sh /src /opt/ppu"
