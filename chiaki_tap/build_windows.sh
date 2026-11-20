#!/bin/bash
# Build chiaki-ng with the frame tap on Windows. Run in an MSYS2 MINGW64 shell:
#
#   bash chiaki_tap/build_windows.sh <chiaki-ng source> <output folder>
#
# Both as MSYS2 paths: C:\src\chiaki-ng is /c/src/chiaki-ng.
#
# Mirrors chiaki-ng's own Windows CI (.github/workflows/build-msys2.yml): same packages, libplacebo commit,
# FFmpeg build, SDL3 + sdl2-compat and deploy script, plus one step: apply_patch.py before compiling.
# Each dependency step is skipped if already done, so re-running only rebuilds chiaki-ng.
# The result is a portable folder; start it with run_chiaki_frametap.bat (copied into it).
set -euo pipefail

SRC="${1:?usage: build_windows.sh <chiaki-ng source> <output folder>}"
OUT="${2:?usage: build_windows.sh <chiaki-ng source> <output folder>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export LIBPLACEBO_VERSION="${LIBPLACEBO_VERSION:-3be579e6c7f6b421d9ac3ab4860edc437c50b3eb}"
FFMPEG_ZIP=ffmpeg-n9.0-latest-win64-gpl-shared-9.0

if [[ "${MSYSTEM:-}" != "MINGW64" ]]; then
    echo "run this from an MSYS2 MINGW64 shell" >&2
    exit 1
fi

step() { echo; echo "=== $*"; }

step "packages"
pacman -S --needed --noconfirm git make unzip zip diffutils \
    mingw-w64-x86_64-{ca-certificates,cc,cmake,curl,fast_float,fftw,json-c,libevent,lcms2,libdovi,meson,miniupnpc,gcc,nasm,ninja,openssl,opus,pkgconf,protobuf,python,python-protobuf,python-psutil,python-glad,python-jinja,python-pip,qt6-base,qt6-declarative,qt6-svg,shaderc,speexdsp,spirv-cross,vulkan,vulkan-headers}

cd "$SRC"

step "libplacebo $LIBPLACEBO_VERSION"
if [[ -f /mingw64/lib/pkgconfig/libplacebo.pc ]]; then
    echo "already installed"
else
    scripts/build-libplacebo-windows.sh
fi

step "FFmpeg ($FFMPEG_ZIP)"
if [[ -f /mingw64/include/libavcodec/avcodec.h ]]; then
    echo "already installed"
else
    [[ -f "$FFMPEG_ZIP.zip" ]] || curl -fLO "https://github.com/streetpea/FFmpeg-Builds/releases/download/latest/$FFMPEG_ZIP.zip"
    rm -rf "$FFMPEG_ZIP" && unzip -q "$FFMPEG_ZIP.zip"
    cp -a "$FFMPEG_ZIP/bin/." /mingw64/bin
    cp -a "$FFMPEG_ZIP/include/." /mingw64/include
    cp -a "$FFMPEG_ZIP/lib/." /mingw64/lib
fi

step "SDL3 + sdl2-compat, hidapi"
if [[ -f /mingw64/lib/pkgconfig/sdl2.pc ]]; then
    echo "already installed"
else
    INSTALL_PREFIX=/mingw64 scripts/build-sdl2-compat.sh .
    INSTALL_PREFIX=/mingw64 scripts/build-hidapi.sh .
    cp /mingw64/lib/pkgconfig/sdl2-compat.pc /mingw64/lib/pkgconfig/sdl2.pc
fi

step "frame tap patch"
python "$HERE/apply_patch.py" "$SRC"

step "configure + build chiaki-ng"
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DCHIAKI_ENABLE_CLI=OFF
cmake --build build --config Release --target chiaki

step "deploy to $OUT"
rm -rf "$OUT"
./scripts/deploy-windows-msys2.sh "$OUT" build/gui/chiaki.exe "$SRC/build/third-party/cpp-steam-tools" /mingw64 gui/src/qml
cp "$HERE/run_chiaki_frametap.bat" "$OUT/"
echo
echo "done: $OUT/run_chiaki_frametap.bat"
