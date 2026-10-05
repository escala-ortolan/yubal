#!/usr/bin/env bash
# Start the intake API on an isolated local port with the download worker running.
# Nothing here touches a live library, a real device, or any other service.
set -euo pipefail

ROOT="${YUBAL_REPO:-/srv/dev/repos/yubal}"
RUN="${YUBAL_RUN_DIR:-/tmp/opencode/yubal-run}"
VENV="${YUBAL_VENV:-/tmp/opencode/yubal-venv}"
FFMPEG_BIN="${YUBAL_FFMPEG_BIN:-/tmp/opencode/yubal-ffmpeg-bin}"
PORT="${YUBAL_PORT:-8011}"

if [[ ! -x "$VENV/bin/python" ]]; then
  echo "No dev environment at $VENV. See docs/DEVELOPMENT.md." >&2
  exit 1
fi

mkdir -p "$RUN/music" "$RUN/staging" "$RUN/config"

export PATH="$FFMPEG_BIN:$PATH"
export YUBAL_ROOT="$ROOT"
export YUBAL_CONFIG="$RUN/config"
# YUBAL_DATA is the music library. Staging must live outside it, by design.
export YUBAL_DATA="$RUN/music"
export YUBAL_INTAKE_ONLY=true
export YUBAL_INTAKE_WORKER_ENABLED=true
export YUBAL_INTAKE_STAGING="$RUN/staging"
export YUBAL_SCHEDULER_ENABLED=false
export YUBAL_HOST=127.0.0.1
export YUBAL_PORT="$PORT"

echo "Database:  $RUN/config/yubal/yubal.db"
echo "Staging:   $RUN/staging"
echo "Music:     $RUN/music   (pass to scripts/file_intake_sources.py)"
echo "Listening: http://127.0.0.1:$PORT"
echo
echo "If no device exists yet, provision one in another terminal:"
echo "  $VENV/bin/python $ROOT/scripts/intake_devices.py provision"
echo

exec "$VENV/bin/python" -m yubal_api