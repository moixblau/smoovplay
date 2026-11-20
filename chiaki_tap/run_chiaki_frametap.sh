#!/bin/bash
# Start chiaki-ng with the frame tap on (Linux), so SmoovPlay's "run.py --chiaki" receives every video frame.
# Runs the Flatpak that build_flatpak.sh made; set CHIAKI_BIN to a chiaki binary of your own build instead.
# Without CHIAKI_FRAMETAP=1 that build behaves exactly like stock chiaki-ng.
set -euo pipefail
if [[ -n "${CHIAKI_BIN:-}" ]]; then
    CHIAKI_FRAMETAP=1 exec "$CHIAKI_BIN" "$@"
fi
exec flatpak run --env=CHIAKI_FRAMETAP=1 ${CHIAKI_FRAMETAP_RENDERER:+--env=CHIAKI_FRAMETAP_RENDERER=$CHIAKI_FRAMETAP_RENDERER} \
    ${CHIAKI_FRAMETAP_QUIT_ON_EXIT:+--env=CHIAKI_FRAMETAP_QUIT_ON_EXIT=$CHIAKI_FRAMETAP_QUIT_ON_EXIT} \
    io.github.streetpea.Chiaki4deck//frametap "$@"
