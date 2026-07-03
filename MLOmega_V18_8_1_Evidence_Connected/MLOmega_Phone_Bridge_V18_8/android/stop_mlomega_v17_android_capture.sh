#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
source "$DIR/lib_mlomega_android.sh"

stop_pid_name() {
  local name="$1"
  local p="$RUN/${name}.pid"
  [ -f "$p" ] || return 0
  local pid
  pid="$(cat "$p" 2>/dev/null || true)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    echo "Arrêt $name pid=$pid"
    kill "$pid" 2>/dev/null || true
  fi
  rm -f "$p"
}

pending_total() {
  local n=0
  n=$(( n + $(find "$SPOOL/audio_pending" -maxdepth 1 -type f ! -name '*.tmp' 2>/dev/null | wc -l | tr -d ' ') ))
  n=$(( n + $(find "$SPOOL/image_pending" -maxdepth 1 -type f ! -name '*.tmp' 2>/dev/null | wc -l | tr -d ' ') ))
  n=$(( n + $(find "$SPOOL/gps_pending" -maxdepth 1 -type f ! -name '*.tmp' ! -name '*.raw.json' 2>/dev/null | wc -l | tr -d ' ') ))
  echo "$n"
}

# 1) Stop capture producers first so no new files are created.
stop_pid_name audio
stop_pid_name gps
stop_pid_name image

# 2) Let uploaders drain local phone spool before telling PC to run post-stop.
# This prevents Brain2 from closing the day while the last chunks are still on Android.
if [ "${DRAIN_UPLOADS_ON_STOP:-1}" = "1" ]; then
  deadline=$(( $(date +%s) + ${DRAIN_UPLOADS_TIMEOUT_SECONDS:-180} ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    left="$(pending_total)"
    [ "${left:-0}" -eq 0 ] && break
    echo "Attente upload avant stop: pending=$left"
    sleep 2
  done
fi

# 3) Stop uploaders after the drain window.
stop_pid_name upload_audio
stop_pid_name upload_gps
stop_pid_name upload_image

if [ "${POST_SESSION_STOP:-1}" = "1" ]; then
  curl -sS -f --connect-timeout 8 --max-time 20 \
    -H "X-MLomega-Token: ${TOKEN}" -H "Content-Type: application/json" \
    --data "{\"event\":\"android_capture_stop\",\"source_event_id\":\"$(capture_source_event_id session "stop_$(now_id)")\",\"stopped_at\":\"$(date -Iseconds)\",\"phone_pending_after_drain\":$(pending_total)}" \
    "${API_BASE}/session/stop" >>"$LOGS/session.out" 2>>"$LOGS/session.err" || true
fi

termux-wake-unlock >/dev/null 2>&1 || true

echo "OK capture Android arrêtée. Les fichiers non envoyés restent dans le spool et partiront au prochain start."
