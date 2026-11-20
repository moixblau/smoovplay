#!/bin/bash
# Build chiaki-ng with the frame tap on Linux, as a Flatpak installed beside your normal chiaki-ng:
#
#   bash chiaki_tap/build_flatpak.sh [work folder, default ~/build-chiaki-ng-frametap]
#
# Uses chiaki-ng's own Flatpak manifest (scripts/flatpak/chiaki4deck.yaml at the tag below), so it gets the same
# FFmpeg (VA-API/Vulkan decoding), libplacebo and SDL as the Flathub build, with two changes: the chiaki-ng source
# is a local checkout with apply_patch.py applied, and the Flatpak branch is "frametap", so it installs next to
# Flathub's (branch "chiaki4deck") instead of replacing it. Your PSN sign-in and registered consoles are shared,
# since both use the same app id.
#
# Needs: git, python3, flatpak and flatpak-builder (Fedora: sudo dnf install git python3 flatpak flatpak-builder).
# The first build takes 15 to 30 minutes; later ones reuse flatpak-builder's cache.
# Start it with chiaki_tap/run_chiaki_frametap.sh.
set -euo pipefail

CHIAKI_TAG="${CHIAKI_TAG:-v1.10.1}"
WORK="${1:-$HOME/build-chiaki-ng-frametap}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$WORK/chiaki-ng"
APP=io.github.streetpea.Chiaki4deck
BRANCH=frametap

step() { echo; echo "=== $*"; }
for tool in git python3 flatpak flatpak-builder; do
    command -v "$tool" >/dev/null || { echo "missing $tool (Fedora: sudo dnf install $tool)" >&2; exit 1; }
done
mkdir -p "$WORK"

step "chiaki-ng $CHIAKI_TAG source"
if [[ -d "$SRC/.git" ]]; then
    git -C "$SRC" fetch --depth 1 origin tag "$CHIAKI_TAG"
    git -C "$SRC" checkout -q -f "$CHIAKI_TAG"
    git -C "$SRC" clean -q -fd  # drops the tap files from an earlier run; apply_patch.py copies them in again
else
    git clone --depth 1 --branch "$CHIAKI_TAG" --recurse-submodules --shallow-submodules \
        https://github.com/chiaki-ng/chiaki-ng "$SRC"
fi
git -C "$SRC" submodule update --init --recursive --depth 1

step "frame tap patch"
python3 "$HERE/apply_patch.py" "$SRC"

step "Flatpak manifest"
MANIFEST_DIR="$WORK/manifest"
rm -rf "$MANIFEST_DIR" && mkdir -p "$MANIFEST_DIR"
cp -a "$SRC/scripts/flatpak/." "$MANIFEST_DIR/"
python3 - "$MANIFEST_DIR/chiaki4deck.yaml" "$SRC" "$BRANCH" <<'EOF'
import re, sys
path, src, branch = sys.argv[1:]
text = open(path).read()
# The chiaki-ng module's source: chiaki-ng's git branch -> the patched local checkout.
module = re.search(r"(  - name: chiaki-ng\n(?:    .*\n|\n)*?    sources:\n)((?:      .*\n)+)", text)
if not module:
    sys.exit("chiaki-ng moved its Flatpak manifest around: no 'name: chiaki-ng' module with sources found")
text = text[:module.start(2)] + f"      - type: dir\n        path: {src}\n" + text[module.end(2):]
text, n = re.subn(r"^branch: .*$", f"branch: {branch}", text, count=1, flags=re.M)
if n != 1:
    sys.exit("no top-level 'branch:' in the manifest")
# SmoovPlay runs outside the sandbox and meets chiaki-ng in /dev/shm: share it explicitly (--device=all already
# does on current Flatpak versions).
text = text.replace("  - --device=all\n", "  - --device=all\n  - --device=shm\n", 1)
open(path, "w").write(text)
EOF

step "Flatpak runtimes"
flatpak remote-add --user --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo
RUNTIME_VERSION="$(sed -n "s/^runtime-version: *'\{0,1\}\([^']*\)'\{0,1\}$/\1/p" "$MANIFEST_DIR/chiaki4deck.yaml")"
flatpak install --user -y --noninteractive flathub "org.kde.Platform//$RUNTIME_VERSION" "org.kde.Sdk//$RUNTIME_VERSION" \
    "io.qt.qtwebengine.BaseApp//$RUNTIME_VERSION"

step "build + install $APP//$BRANCH"
cd "$MANIFEST_DIR"
flatpak-builder --user --install --force-clean --state-dir="$WORK/.flatpak-builder" "$WORK/build" chiaki4deck.yaml

# The tap on by default, so starting chiaki-ng from the app menu feeds SmoovPlay too (Flathub's build, same app id,
# has no tap and ignores it).
flatpak override --user --env=CHIAKI_FRAMETAP=1 "$APP"

echo
echo "done: start it with  bash $HERE/run_chiaki_frametap.sh  (or from the app menu)"
