#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail
source "$(dirname "$0")/lib_mlomega_android.sh"

mark_sent_or_delete() {
  local file="$1"
  if [ "${KEEP_SENT_FILES:-0}" = "1" ]; then
    mv "$file" "$SPOOL/sent/$(basename "$file")" 2>/dev/null || rm -f "$file"
  else
    rm -f "$file"
  fi
}

upload_gps_json() {
  local file="$1"
  [ -f "$file" ] || return 0
  case "$file" in *.tmp|*.raw.json) return 0 ;; esac
  if api_post_json_fast "/gps" "$file" >>"$LOGS/upload_gps.out" 2>>"$LOGS/upload_gps.err"; then
    log "uploaded gps $(basename "$file")"
    mark_sent_or_delete "$file"
  else
    log "upload gps failed $(basename "$file")"
    sleep "${GPS_UPLOAD_FAIL_SLEEP_SECONDS:-1}"
  fi
}

log "gps upload loop start API_BASE=${API_BASE}"
while true; do
  # GPS est séparé pour ne jamais bloquer l'audio. C'est petit, mais indépendant.
  for f in "$SPOOL"/gps_pending/*.json; do
    [ -e "$f" ] || continue
    upload_gps_json "$f"
  done
  sleep "${GPS_UPLOAD_SLEEP_SECONDS:-1}"
done
