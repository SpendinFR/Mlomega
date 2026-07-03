#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail
source "$(dirname "$0")/lib_mlomega_android.sh"

optimize_image_if_possible() {
  local src="$1"
  local dst="$2"
  if [ "${IMAGE_OPTIMIZE:-1}" != "1" ]; then
    mv "$src" "$dst"
    return 0
  fi
  local tool=""
  if command -v magick >/dev/null 2>&1; then
    tool="magick"
  elif command -v convert >/dev/null 2>&1; then
    tool="convert"
  fi
  if [ -n "$tool" ]; then
    # Enough detail for Moondream live and Qwen/Qwen-VL offline, without
    # flooding the network with full multi-megapixel phone photos.
    if "$tool" "$src" -auto-orient -strip -resize "${IMAGE_MAX_DIM:-1600}x${IMAGE_MAX_DIM:-1600}>" -quality "${IMAGE_JPEG_QUALITY:-88}" "$dst" 2>>"$LOGS/image.err"; then
      rm -f "$src"
      return 0
    fi
  fi
  # Fallback: keep original camera JPEG if ImageMagick is missing/fails.
  mv "$src" "$dst"
}

image_bytes() {
  local f="$1"
  [ -f "$f" ] && wc -c < "$f" 2>/dev/null || echo 0
}

log "image loop start IMAGE_SECONDS=${IMAGE_SECONDS:-30} CAMERA_ID=${CAMERA_ID:-0} max_dim=${IMAGE_MAX_DIM:-1600} quality=${IMAGE_JPEG_QUALITY:-88}"
while true; do
  if [ "${ENABLE_IMAGES:-1}" != "1" ]; then
    sleep 5
    continue
  fi
  if ! storage_ok_for_images; then
    log "image capture skipped: low storage or too much image backlog free_mb=$(free_mb) pending=$(pending_count "$SPOOL/image_pending")"
    sleep "${IMAGE_SECONDS:-30}"
    continue
  fi

  id="image_$(now_id)_${RANDOM}"
  raw="$SPOOL/image_pending/${id}.raw.jpg.tmp"
  final_tmp="$SPOOL/image_pending/${id}.jpg.tmp"
  final="$SPOOL/image_pending/${id}.jpg"
  meta="$SPOOL/image_pending/${id}.json"
  captured="$(date -Iseconds)"
  source_event_id="$(capture_source_event_id image "$id")"
  if termux-camera-photo -c "${CAMERA_ID:-0}" "$raw" >/dev/null 2>>"$LOGS/image.err"; then
    if wait_file_ready "$raw" "${IMAGE_MIN_BYTES:-10000}" 8 && optimize_image_if_possible "$raw" "$final_tmp" && wait_file_ready "$final_tmp" "${IMAGE_MIN_BYTES:-10000}" 8; then
      gps_file="$(last_gps_file)"
      common_jq='{
        type:$type,
        media_kind:"image",
        captured_at:$captured_at,
        timestamp_start:$captured_at,
        timestamp_end:$captured_at,
        source:$source,
        source_event_id:$source_event_id,
        source_device:"android_phone",
        capture_profile:"vlm_scene_v17_5",
        camera_id:$camera_id,
        image_priority:"scene_understanding",
        max_dim_px:$max_dim,
        jpeg_quality:$jpeg_quality,
        bytes:$bytes,
        expected_downstream:["moondream_live_vlm","brain2_offline_deep_vlm","scene_context","place_context"]
      }'
      if [ -f "$gps_file" ]; then
        jq -n \
          --arg type "image" \
          --arg captured_at "$captured" \
          --arg source "android_termux_camera" \
          --arg source_event_id "$source_event_id" \
          --arg camera_id "${CAMERA_ID:-0}" \
          --argjson max_dim "${IMAGE_MAX_DIM:-1600}" \
          --argjson jpeg_quality "${IMAGE_JPEG_QUALITY:-88}" \
          --argjson bytes "$(image_bytes "$final_tmp")" \
          --slurpfile gps "$gps_file" \
          "$common_jq + {gps:(\$gps[0] // null)}" > "$meta"
      else
        jq -n \
          --arg type "image" \
          --arg captured_at "$captured" \
          --arg source "android_termux_camera" \
          --arg source_event_id "$source_event_id" \
          --arg camera_id "${CAMERA_ID:-0}" \
          --argjson max_dim "${IMAGE_MAX_DIM:-1600}" \
          --argjson jpeg_quality "${IMAGE_JPEG_QUALITY:-88}" \
          --argjson bytes "$(image_bytes "$final_tmp")" \
          "$common_jq" > "$meta"
      fi
      mv "$final_tmp" "$final"
    else
      rm -f "$raw" "$final_tmp" "$meta"
      log "image capture produced no stable file"
    fi
  else
    rm -f "$raw" "$final_tmp" "$meta"
    log "image capture failed"
  fi
  sleep "${IMAGE_SECONDS:-30}"
done
