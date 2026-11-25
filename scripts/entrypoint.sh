#!/usr/bin/env bash
# Docker entrypoint for vcam.
#
# When the container is started with no arguments (or just "run") and no config
# file is present, a single demo camera is generated on the fly using ffmpeg's
# built-in testsrc pattern so the image works in one click:
#
#   docker run -p 8554:8554 vcam
#   # => rtsp://localhost:8554/demo
#
# Any explicit arguments are passed through unchanged:
#
#   docker run -p 8554:8554 -v $PWD/cameras.yaml:/vcam/cameras.yaml vcam run
#   docker run vcam run --source /vcam/videos/clip.mp4
#   docker run vcam init
set -euo pipefail

DEMO_DIR=/tmp/vcam-demo
DEMO_VIDEO=$DEMO_DIR/demo.mp4
CONFIG_NAMES=(cameras.yaml cameras.yml vcam.yaml vcam.yml)

# ---------------------------------------------------------------------------
# Detect whether demo mode should be activated.
# We only intercept "vcam run" (the default CMD) when:
#   - the subcommand is "run" (first positional arg)
#   - no --config / --source / --camera flag is present
#   - no well-known config file exists in the working directory
# ---------------------------------------------------------------------------
_is_demo_needed() {
    # Not the "run" subcommand → pass through
    [[ "${1:-run}" == "run" ]] || return 1

    # Explicit source/config flags present → pass through
    for arg in "$@"; do
        case "$arg" in
            --config|--config=*|-c|--source|--source=*|-s|--camera|--camera=*) return 1 ;;
        esac
    done

    # Config file found on disk → pass through
    for name in "${CONFIG_NAMES[@]}"; do
        [[ -f "$name" ]] && return 1
    done

    return 0
}

# ---------------------------------------------------------------------------
# Generate the demo video (only once; reused across container restarts).
# ---------------------------------------------------------------------------
_make_demo_video() {
    if [[ -f "$DEMO_VIDEO" ]]; then
        return
    fi
    echo "[vcam-demo] generating demo video (this runs once)..."
    mkdir -p "$DEMO_DIR"
    ffmpeg -hide_banner -loglevel error -y \
        -f lavfi -i "testsrc=size=1280x720:rate=25:duration=30" \
        -c:v libx264 -preset veryfast -pix_fmt yuv420p \
        -g 25 -keyint_min 25 -sc_threshold 0 \
        "$DEMO_VIDEO"
    echo "[vcam-demo] demo video ready: $DEMO_VIDEO"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if _is_demo_needed "$@"; then
    _make_demo_video
    echo "[vcam-demo] no config found — starting demo camera at rtsp://0.0.0.0:8554/demo"
    echo "[vcam-demo] mount a config file at /vcam/cameras.yaml to use your own cameras."
    if [[ "${1:-}" == run ]]; then shift; fi
    exec vcam run --source "$DEMO_VIDEO" --name demo "$@"
fi

exec vcam "$@"
