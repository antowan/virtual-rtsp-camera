#!/usr/bin/env bash
# Run inside a disposable runtime image with --network none, not on a shared host.
set -euo pipefail
[[ "$(id -u)" != 0 ]]
work="$(mktemp -d)"
cd "$work"
export VCAM_STATE_DIR="$work/state"
cat > cameras.yaml <<'YAML'
# keep this comment
control: {enabled: true, port: 0}
server: {host: 127.0.0.1, rtsp_port: 18554}
cameras: []
YAML
ffmpeg -nostdin -v error -f lavfi -i testsrc2=size=160x120:rate=12 \
    -t 2 -c:v libx264 clip.mp4
/opt/vcam/entrypoint.sh run -c cameras.yaml >engine.log 2>&1 &
engine=$!
cleanup() {
    kill -TERM "$engine" 2>/dev/null || true
    wait "$engine" || true
    cd /
    rm -rf "$work"
}
trap cleanup EXIT
trap 'cat engine.log >&2' ERR

timeout 20 bash -c 'until vcam status --json > status.json 2>/dev/null &&
    grep -Eq "\"updated_at\": *[0-9]" status.json; do sleep 0.2; done'
vcam add clip.mp4 --name live --json > operation.json
python -c 'import json; o = json.load(open("operation.json")); assert o["status"] == "succeeded" and o["config_persisted"] and o["applied"], o'
grep -Fq '# keep this comment' cameras.yaml
grep -Fq 'name: live' cameras.yaml
timeout 20 bash -c 'until ffmpeg -nostdin -v error -rtsp_transport tcp -timeout 2000000 \
    -i rtsp://127.0.0.1:18554/live -map 0:v:0 -frames:v 1 -f null -; do sleep 0.2; done'
vcam engine stop --allow-disruption
vcam status --json > status.json
python -c 'import json, sys; s = json.load(open("status.json")); assert s["engine"]["state"] == "stopped" and s["pid"] == int(sys.argv[1]), s' "$engine"
echo 'OK: packaged non-root engine added and decoded a stream; the same daemon survived stream stop.'
