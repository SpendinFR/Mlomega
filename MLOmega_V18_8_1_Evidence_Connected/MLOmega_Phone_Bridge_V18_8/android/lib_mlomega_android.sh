#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

CONFIG="$HOME/mlomega_android_config.env"
if [ -f "$CONFIG" ]; then
  # shellcheck disable=SC1090
  source "$CONFIG"
else
  echo "Config absente: $CONFIG" >&2
  exit 1
fi

BASE="$HOME/mlomega_phone_bridge"
SPOOL="$BASE/spool"
RUN="$BASE/run"
LOGS="$BASE/logs"
mkdir -p "$SPOOL/audio_pending" "$SPOOL/image_pending" "$SPOOL/gps_pending" "$SPOOL/sent" "$SPOOL/failed" "$RUN" "$LOGS"

log() {
  local msg="$1"
  local line
  line="$(date -Iseconds) $msg"
  echo "$line" | tee -a "$LOGS/bridge.log" >/dev/null
}

now_id() {
  date +"%Y%m%d_%H%M%S"
}

capture_source_event_id() {
  # Generated at capture time, persisted in the sidecar and therefore stable
  # across every curl retry and receiver restart for this one physical capture.
  local kind="$1"
  local capture_id="$2"
  local device="${MLOMEGA_DEVICE_ID:-android_phone}"
  printf '%s:%s:%s' "$device" "$kind" "$capture_id"
}

api_post_json() {
  local endpoint="$1"
  local file="$2"
  curl -sS -f --retry "${CURL_RETRY:-2}" --retry-delay "${CURL_RETRY_DELAY:-2}" --connect-timeout "${CURL_CONNECT_TIMEOUT:-8}" --max-time "${CURL_MAX_TIME:-120}" \
    -H "X-MLomega-Token: ${TOKEN}" \
    -H "Content-Type: application/json" \
    --data-binary "@$file" \
    "${API_BASE}${endpoint}"
}

api_post_file() {
  local endpoint="$1"
  local file="$2"
  local meta_file="${3:-}"
  if [ -n "$meta_file" ] && [ -f "$meta_file" ]; then
    curl -sS -f --retry "${CURL_RETRY:-2}" --retry-delay "${CURL_RETRY_DELAY:-2}" --connect-timeout "${CURL_CONNECT_TIMEOUT:-8}" --max-time "${CURL_MAX_TIME:-120}" \
      -H "X-MLomega-Token: ${TOKEN}" \
      -F "file=@${file}" \
      --form-string "meta=$(cat "$meta_file")" \
      "${API_BASE}${endpoint}"
  else
    curl -sS -f --retry "${CURL_RETRY:-2}" --retry-delay "${CURL_RETRY_DELAY:-2}" --connect-timeout "${CURL_CONNECT_TIMEOUT:-8}" --max-time "${CURL_MAX_TIME:-120}" \
      -H "X-MLomega-Token: ${TOKEN}" \
      -F "file=@${file}" \
      "${API_BASE}${endpoint}"
  fi
}

api_post_json_fast() {
  local endpoint="$1"
  local file="$2"
  curl -sS -f --retry "${CURL_RETRY:-2}" --retry-delay "${CURL_RETRY_DELAY:-2}" --connect-timeout "${FAST_CURL_CONNECT_TIMEOUT:-${CURL_CONNECT_TIMEOUT:-5}}" --max-time "${FAST_CURL_MAX_TIME:-30}" \
    -H "X-MLomega-Token: ${TOKEN}" \
    -H "Content-Type: application/json" \
    --data-binary "@$file" \
    "${API_BASE}${endpoint}"
}

api_post_file_fast() {
  local endpoint="$1"
  local file="$2"
  local meta_file="${3:-}"
  if [ -n "$meta_file" ] && [ -f "$meta_file" ]; then
    curl -sS -f --retry "${CURL_RETRY:-2}" --retry-delay "${CURL_RETRY_DELAY:-2}" --connect-timeout "${FAST_CURL_CONNECT_TIMEOUT:-${CURL_CONNECT_TIMEOUT:-5}}" --max-time "${AUDIO_CURL_MAX_TIME:-35}" \
      -H "X-MLomega-Token: ${TOKEN}" \
      -F "file=@${file}" \
      --form-string "meta=$(cat "$meta_file")" \
      "${API_BASE}${endpoint}"
  else
    curl -sS -f --retry "${CURL_RETRY:-2}" --retry-delay "${CURL_RETRY_DELAY:-2}" --connect-timeout "${FAST_CURL_CONNECT_TIMEOUT:-${CURL_CONNECT_TIMEOUT:-5}}" --max-time "${AUDIO_CURL_MAX_TIME:-35}" \
      -H "X-MLomega-Token: ${TOKEN}" \
      -F "file=@${file}" \
      "${API_BASE}${endpoint}"
  fi
}

api_post_file_slow() {
  local endpoint="$1"
  local file="$2"
  local meta_file="${3:-}"
  if [ -n "$meta_file" ] && [ -f "$meta_file" ]; then
    curl -sS -f --retry "${CURL_RETRY:-2}" --retry-delay "${CURL_RETRY_DELAY:-2}" --connect-timeout "${CURL_CONNECT_TIMEOUT:-8}" --max-time "${IMAGE_CURL_MAX_TIME:-180}" \
      -H "X-MLomega-Token: ${TOKEN}" \
      -F "file=@${file}" \
      --form-string "meta=$(cat "$meta_file")" \
      "${API_BASE}${endpoint}"
  else
    curl -sS -f --retry "${CURL_RETRY:-2}" --retry-delay "${CURL_RETRY_DELAY:-2}" --connect-timeout "${CURL_CONNECT_TIMEOUT:-8}" --max-time "${IMAGE_CURL_MAX_TIME:-180}" \
      -H "X-MLomega-Token: ${TOKEN}" \
      -F "file=@${file}" \
      "${API_BASE}${endpoint}"
  fi
}

last_gps_file() {
  echo "$SPOOL/latest_gps.json"
}

maps_url() {
  local lat="$1"
  local lon="$2"
  echo "https://maps.google.com/?q=${lat},${lon}"
}

haversine_m() {
  local lat1="$1" lon1="$2" lat2="$3" lon2="$4"
  awk -v lat1="$lat1" -v lon1="$lon1" -v lat2="$lat2" -v lon2="$lon2" 'BEGIN {
    pi=atan2(0,-1); r=6371000;
    p1=lat1*pi/180; p2=lat2*pi/180; dp=(lat2-lat1)*pi/180; dl=(lon2-lon1)*pi/180;
    a=sin(dp/2)^2 + cos(p1)*cos(p2)*sin(dl/2)^2;
    c=2*atan2(sqrt(a), sqrt(1-a));
    printf "%.0f", r*c;
  }'
}

should_reverse_geocode() {
  [ "${REVERSE_GEOCODE:-0}" = "1" ] || return 1
  local lat="$1" lon="$2"
  local cache="$SPOOL/last_address.json"
  [ -f "$cache" ] || return 0
  local last_ts last_lat last_lon now_s age dist
  last_ts=$(jq -r '.address_checked_epoch // 0' "$cache" 2>/dev/null || echo 0)
  last_lat=$(jq -r '.lat // empty' "$cache" 2>/dev/null || true)
  last_lon=$(jq -r '.lon // empty' "$cache" 2>/dev/null || true)
  now_s=$(date +%s)
  age=$(( now_s - ${last_ts:-0} ))
  if [ "$age" -ge "${ADDRESS_SECONDS:-300}" ]; then
    return 0
  fi
  if [ -n "$last_lat" ] && [ -n "$last_lon" ]; then
    dist=$(haversine_m "$lat" "$lon" "$last_lat" "$last_lon")
    if [ "$dist" -ge "${ADDRESS_MIN_MOVE_METERS:-80}" ]; then
      return 0
    fi
  fi
  return 1
}

reverse_geocode_or_cache() {
  local lat="$1" lon="$2"
  local cache="$SPOOL/last_address.json"
  if should_reverse_geocode "$lat" "$lon"; then
    local tmp="$SPOOL/reverse_$(now_id).json.tmp"
    # Service externe facultatif: donne une adresse lisible sans clé API.
    if curl -sS --connect-timeout 8 --max-time 20 \
      -H "User-Agent: MLOmegaPhoneBridge/17.4 Android personal use" \
      "https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat=${lat}&lon=${lon}&zoom=18&addressdetails=1" > "$tmp"; then
      local display name
      display=$(jq -r '.display_name // empty' "$tmp" 2>/dev/null || true)
      name=$(jq -r '.name // .address.amenity // .address.building // .address.road // empty' "$tmp" 2>/dev/null || true)
      jq -n \
        --arg lat "$lat" --arg lon "$lon" \
        --arg display "$display" --arg name "$name" \
        --argjson epoch "$(date +%s)" \
        '{lat:($lat|tonumber), lon:($lon|tonumber), place_name:$name, address:$display, address_checked_epoch:$epoch, geocoder:"nominatim"}' > "$cache"
    fi
    rm -f "$tmp"
  fi
  if [ -f "$cache" ]; then
    cat "$cache"
  else
    jq -n --arg lat "$lat" --arg lon "$lon" '{lat:($lat|tonumber), lon:($lon|tonumber)}'
  fi
}

make_gps_payload() {
  local raw="$1"
  local out="$2"
  local capture_id="${3:-gps_$(now_id)}"
  local source_event_id
  source_event_id="$(capture_source_event_id gps "$capture_id")"
  local lat lon acc provider altitude speed bearing
  lat=$(jq -r '.latitude // empty' "$raw" 2>/dev/null || true)
  lon=$(jq -r '.longitude // empty' "$raw" 2>/dev/null || true)
  [ -n "$lat" ] && [ -n "$lon" ] || return 1
  acc=$(jq -r '.accuracy // empty' "$raw" 2>/dev/null || true)
  provider=$(jq -r '.provider // empty' "$raw" 2>/dev/null || true)
  altitude=$(jq -r '.altitude // empty' "$raw" 2>/dev/null || true)
  speed=$(jq -r '.speed // empty' "$raw" 2>/dev/null || true)
  bearing=$(jq -r '.bearing // empty' "$raw" 2>/dev/null || true)

  local addr_json="$SPOOL/current_address_work.json"
  reverse_geocode_or_cache "$lat" "$lon" > "$addr_json" || true
  local maps
  maps=$(maps_url "$lat" "$lon")

  local captured_at
  captured_at="$(date -Iseconds)"
  jq -n \
    --arg captured_at "$captured_at" \
    --arg source_event_id "$source_event_id" \
    --arg source "android_termux_location" \
    --arg lat "$lat" --arg lon "$lon" \
    --arg accuracy "$acc" --arg provider "$provider" \
    --arg altitude "$altitude" --arg speed "$speed" --arg bearing "$bearing" \
    --arg maps_url "$maps" \
    --slurpfile addr "$addr_json" \
    '{
      type:"gps",
      media_kind:"gps",
      captured_at:$captured_at,
      timestamp_start:$captured_at,
      timestamp_end:$captured_at,
      source:$source,
      source_event_id:$source_event_id,
      source_device:"android_phone",
      capture_profile:"location_context_v17_5",
      lat:($lat|tonumber),
      lon:($lon|tonumber),
      label: (($addr[0].place_name // $addr[0].address // "android_location") | tostring),
      address: ($addr[0].address // null),
      place_name: ($addr[0].place_name // null),
      maps_url:$maps_url,
      accuracy_m: (if $accuracy == "" then null else ($accuracy|tonumber) end),
      provider: (if $provider == "" then null else $provider end),
      altitude: (if $altitude == "" then null else ($altitude|tonumber) end),
      speed: (if $speed == "" then null else ($speed|tonumber) end),
      bearing: (if $bearing == "" then null else ($bearing|tonumber) end),
      confidence: 0.9
    }' > "$out"
}

free_mb() {
  df -Pm "$BASE" 2>/dev/null | awk 'NR==2 {print $4+0}'
}

pending_count() {
  local dir="$1"
  find "$dir" -maxdepth 1 -type f 2>/dev/null | wc -l | tr -d ' '
}

storage_ok_for_audio() {
  local mb
  mb=$(free_mb)
  [ "${mb:-0}" -ge "${MIN_FREE_MB_FOR_AUDIO:-512}" ] || return 1
  local n
  n=$(pending_count "$SPOOL/audio_pending")
  [ "${n:-0}" -lt "${MAX_AUDIO_PENDING:-5000}" ] || return 1
  return 0
}

storage_ok_for_images() {
  local mb
  mb=$(free_mb)
  [ "${mb:-0}" -ge "${MIN_FREE_MB_FOR_IMAGES:-2048}" ] || return 1
  local n
  n=$(pending_count "$SPOOL/image_pending")
  [ "${n:-0}" -lt "${MAX_IMAGE_PENDING:-300}" ] || return 1
  return 0
}

wait_file_ready() {
  local file="$1"
  local min_bytes="${2:-1}"
  local max_wait="${3:-10}"
  local waited=0
  local last_size=-1
  while [ "$waited" -lt "$max_wait" ]; do
    if [ -f "$file" ]; then
      local size
      size=$(wc -c < "$file" 2>/dev/null || echo 0)
      if [ "$size" -ge "$min_bytes" ] && [ "$size" = "$last_size" ]; then
        return 0
      fi
      last_size="$size"
    fi
    sleep 1
    waited=$((waited + 1))
  done
  [ -f "$file" ] && [ "$(wc -c < "$file" 2>/dev/null || echo 0)" -ge "$min_bytes" ]
}
