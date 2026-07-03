#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
source "$DIR/lib_mlomega_android.sh"

# Nettoyage de vieux PIDs morts.
for p in "$RUN"/*.pid; do
  [ -e "$p" ] || continue
  pid="$(cat "$p" 2>/dev/null || true)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    echo "Déjà lancé: $(basename "$p") pid=$pid"
  else
    rm -f "$p"
  fi
done

if [[ "${API_BASE}" == *"IP_TAILSCALE_DU_PC"* || "${TOKEN}" == "TON_GROS_SECRET" ]]; then
  echo "ERREUR: modifie API_BASE et TOKEN dans ~/mlomega_android_config.env avant de lancer." >&2
  exit 1
fi

termux-wake-lock >/dev/null 2>&1 || true

curl -sS -f --connect-timeout 8 --max-time 15 \
  -H "X-MLomega-Token: ${TOKEN}" -H "Content-Type: application/json" \
  --data "{\"event\":\"android_capture_start\",\"source_event_id\":\"$(capture_source_event_id session "start_$(now_id)")\",\"started_at\":\"$(date -Iseconds)\"}" \
  "${API_BASE}/session/start" >>"$LOGS/session.out" 2>>"$LOGS/session.err" || true

start_loop() {
  local name="$1"
  local script="$2"
  if [ -f "$RUN/${name}.pid" ]; then
    local pid
    pid="$(cat "$RUN/${name}.pid" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      echo "$name déjà lancé pid=$pid"
      return
    fi
  fi
  nohup "$DIR/$script" >>"$LOGS/${name}.log" 2>&1 &
  echo $! > "$RUN/${name}.pid"
  echo "$name lancé pid=$!"
}

start_loop audio capture_audio_loop.sh
start_loop gps capture_gps_loop.sh
start_loop image capture_image_loop.sh
start_loop upload_audio upload_audio_loop.sh
start_loop upload_gps upload_gps_loop.sh
start_loop upload_image upload_image_loop.sh

echo "OK capture Android lancée."
echo "Audio: ${AUDIO_SECONDS:-4}s | Image: ${IMAGE_SECONDS:-30}s | GPS: ${GPS_SECONDS:-60}s"
echo "Uploaders séparés: audio/gps/image. Une image lente ne bloque pas l’audio."
echo "Logs: $LOGS"
