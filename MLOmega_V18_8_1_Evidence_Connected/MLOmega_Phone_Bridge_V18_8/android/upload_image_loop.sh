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

upload_image_file() {
  local file="$1"
  [ -f "$file" ] || return 0
  case "$file" in *.tmp) return 0 ;; esac
  if api_post_file_slow "/upload/image" "$file" "${file%.*}.json" >>"$LOGS/upload_image.out" 2>>"$LOGS/upload_image.err"; then
    log "uploaded image $(basename "$file")"
    mark_sent_or_delete "$file" "${file%.*}.json"
  else
    log "upload image failed $(basename "$file")"
    sleep "${IMAGE_UPLOAD_FAIL_SLEEP_SECONDS:-3}"
  fi
}

log "image upload loop start API_BASE=${API_BASE}"
while true; do
  # Images: boucle dédiée lente. Si une image met longtemps, l'audio continue via son uploader séparé.
  for f in "$SPOOL"/image_pending/*.jpg "$SPOOL"/image_pending/*.jpeg "$SPOOL"/image_pending/*.png "$SPOOL"/image_pending/*.webp; do
    [ -e "$f" ] || continue
    upload_image_file "$f"
  done
  sleep "${IMAGE_UPLOAD_SLEEP_SECONDS:-1}"
done
