#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail
source "$(dirname "$0")/lib_mlomega_android.sh"

mark_sent_or_delete() {
  local file="$1"
  local meta="${2:-}"
  if [ "${KEEP_SENT_FILES:-0}" = "1" ]; then
    mv "$file" "$SPOOL/sent/$(basename "$file")" 2>/dev/null || rm -f "$file"
    [ -n "$meta" ] && [ -f "$meta" ] && mv "$meta" "$SPOOL/sent/$(basename "$meta")" 2>/dev/null || true
  else
    rm -f "$file"
    [ -n "$meta" ] && rm -f "$meta" || true
  fi
}

upload_audio_file() {
  local file="$1"
  [ -f "$file" ] || return 0
  case "$file" in *.tmp) return 0 ;; esac
  if api_post_file_fast "/upload/audio" "$file" "${file%.*}.json" >>"$LOGS/upload_audio.out" 2>>"$LOGS/upload_audio.err"; then
    log "uploaded audio $(basename "$file")"
    mark_sent_or_delete "$file" "${file%.*}.json"
  else
    log "upload audio failed $(basename "$file")"
    sleep "${AUDIO_UPLOAD_FAIL_SLEEP_SECONDS:-1}"
  fi
}

log "audio upload loop start API_BASE=${API_BASE}"
while true; do
  # Audio prioritaire: boucle dédiée, ne dépend ni des images ni du GPS.
  for f in "$SPOOL"/audio_pending/*.m4a "$SPOOL"/audio_pending/*.wav "$SPOOL"/audio_pending/*.mp3 "$SPOOL"/audio_pending/*.ogg "$SPOOL"/audio_pending/*.aac; do
    [ -e "$f" ] || continue
    upload_audio_file "$f"
  done
  sleep "${AUDIO_UPLOAD_SLEEP_SECONDS:-0.3}"
done
