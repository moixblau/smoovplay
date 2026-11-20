#!/bin/bash
# chiaki-ng with the frame tap, and SmoovPlay waiting beside it (Linux): connect to the PS5 in chiaki-ng and
# SmoovPlay's window opens with the stream, while chiaki-ng stops drawing the video and minimises itself
# (CHIAKI_FRAMETAP_NO_VIDEO, set by build_flatpak.sh). SmoovPlay waits again after each stream, until chiaki-ng is
# closed; if you close SmoovPlay mid-stream, chiaki-ng shows the game itself until that stream ends.
# The "SmoovPlay (PS5)" app-menu entry runs this. SmoovPlay's log: ~/.cache/smoovplay-run.log
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE/.."
live() { [[ "$(od -A n -t x4 -N 4 /dev/shm/chiaki-frametap 2>/dev/null | tr -d ' ')" == 50544643 ]]; }

bash "$HERE/run_chiaki_frametap.sh" "$@" &
chiaki=$!
while kill -0 "$chiaki" 2>/dev/null; do
    .venv/bin/python run.py --chiaki --display --fullscreen >> "$HOME/.cache/smoovplay-run.log" 2>&1 &
    py=$!
    while kill -0 "$py" 2>/dev/null && kill -0 "$chiaki" 2>/dev/null; do sleep 1; done
    kill "$py" 2>/dev/null; wait "$py" 2>/dev/null
    while live && kill -0 "$chiaki" 2>/dev/null; do sleep 1; done  # closed mid-stream: not again until it ends
    sleep 2
done
