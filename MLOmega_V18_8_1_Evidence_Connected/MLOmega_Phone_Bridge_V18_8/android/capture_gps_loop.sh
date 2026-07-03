#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail
source "$(dirname "$0")/lib_mlomega_android.sh"

log "gps loop start GPS_SECONDS=${GPS_SECONDS:-60} REVERSE_GEOCODE=${REVERSE_GEOCODE:-0}"
while true; do
  if [ "${ENABLE_GPS:-1}" != "1" ]; then
    sleep 5
    continue
  fi
  id="gps_$(now_id)_${RANDOM}"
  raw="$SPOOL/gps_pending/${id}.raw.json"
  final="$SPOOL/gps_pending/${id}.json"
  latest="$(last_gps_file)"

  # GPS d'abord, puis fallback réseau. Le timeout évite que la localisation bloque la boucle.
  if timeout 25 termux-location -p gps -r once > "$raw" 2>>"$LOGS/gps.err" || timeout 15 termux-location -p network -r once > "$raw" 2>>"$LOGS/gps.err"; then
    if make_gps_payload "$raw" "$final" "$id"; then
      cp "$final" "$latest"
    else
      rm -f "$final"
      log "gps payload invalid"
    fi
  else
    log "gps capture failed"
  fi
  rm -f "$raw"
  sleep "${GPS_SECONDS:-60}"
done
