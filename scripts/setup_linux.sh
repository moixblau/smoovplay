#!/bin/bash
# SmoovPlay on Linux with an AMD Radeon card (RX 7000 / 9000 series): the Python side, in .venv next to run.py.
#
#   bash scripts/setup_linux.sh
#
# Installs PyTorch's ROCm build (torch.cuda then drives the AMD card through HIP), the rest of requirements.txt,
# and RIFE's weights, then checks the card. chiaki-ng with the frame tap is built separately:
# bash chiaki_tap/build_flatpak.sh. See docs/LINUX.md.
#
# ROCM_INDEX picks PyTorch's ROCm wheel index (default below; RX 9000 / gfx1201 needs ROCm 6.4 or newer).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROCM_INDEX="${ROCM_INDEX:-https://download.pytorch.org/whl/rocm6.4}"
PY="${PYTHON:-python3}"

step() { echo; echo "=== $*"; }

step "system packages"
if command -v dnf >/dev/null; then
    echo "Fedora: if anything below is missing, install it with"
    echo "  sudo dnf install python3 python3-devel portaudio libva-utils mesa-va-drivers flatpak flatpak-builder"
    echo "and for H.264/H.265 hardware decoding, Mesa's full VA-API driver from RPM Fusion:"
    echo "  sudo dnf swap mesa-va-drivers mesa-va-drivers-freeworld"
fi
if ! id -nG | grep -qw render; then
    echo "WARNING: you're not in the 'render' group, which ROCm needs: sudo usermod -aG render,video \$USER, then log in again"
fi

step "Python environment ($ROOT/.venv)"
"$PY" -m venv "$ROOT/.venv"
VPY="$ROOT/.venv/bin/python"
"$VPY" -m pip install -q --upgrade pip
"$VPY" -m pip install torch --index-url "$ROCM_INDEX"
grep -v '^torch' "$ROOT/requirements.txt" | grep -v '^#' | grep -v '^$' | xargs "$VPY" -m pip install -q

step "the graphics card"
"$VPY" - <<'EOF'
import torch
hip = getattr(torch.version, "hip", None)
if not hip:
    raise SystemExit("this PyTorch isn't the ROCm build; check ROCM_INDEX")
if not torch.cuda.is_available():
    raise SystemExit("PyTorch ROCm can't see the card: check the 'render' group and that amdgpu is loaded")
print(f"PyTorch {torch.__version__}, HIP {hip}, {torch.cuda.get_device_name(0)}")
x = torch.randn(1, 3, 1088, 1920, device="cuda", dtype=torch.half)
print("fp16 on the card:", float((x * 2).sum()) is not None)
EOF

step "RIFE weights"
"$VPY" -c "from framegen.interpolator import ensure_weights; print(ensure_weights('4.22.lite'))"

if command -v vainfo >/dev/null; then
    step "VA-API (hardware decoding of the Remote Play stream)"
    vainfo 2>/dev/null | grep -E "VAProfileHEVCMain|VAProfileH264" | grep -i VLD \
        || echo "no H.264/H.265 VA-API decoding: Remote Play is decoded on the CPU (works; for the GPU, see above)"
fi

echo
echo "done. Benchmark:  .venv/bin/python scripts/benchmark.py   Play:  see docs/LINUX.md"
