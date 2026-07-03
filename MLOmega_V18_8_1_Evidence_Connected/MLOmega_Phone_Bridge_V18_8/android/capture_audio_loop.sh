#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail
source "$(dirname "$0")/lib_mlomega_android.sh"

is_wav_file() {
  local f="$1"
  [ -f "$f" ] || return 1
  # WAV starts with RIFF....WAVE.  We only test the stable final temp file.
  local head
  head=$(dd if="$f" bs=12 count=1 2>/dev/null | od -An -tx1 | tr -d ' \n' || true)
  [[ "$head" == 52494646*57415645 ]]
}

write_audio_meta() {
  local meta="$1"
  local type="$2"
  local started="$3"
  local ended="$4"
  local file="$5"
  local encoder="$6"
  local bitrate="$7"
  local sample_rate="$8"
  local channels="$9"
  local format="${10}"
  local fallback_used="${11:-0}"
  local warning="${12:-}"
  local source_event_id="${13:-}"
  local duration="${AUDIO_SECONDS:-4}"
  local bytes="0"
  [ -f "$file" ] && bytes=$(wc -c < "$file" 2>/dev/null || echo 0)
  jq -n \
    --arg type "$type" \
    --arg timestamp_start "$started" \
    --arg timestamp_end "$ended" \
    --arg captured_at "$started" \
    --arg source "android_termux_microphone" \
    --arg source_device "android_phone" \
    --arg format "$format" \
    --arg encoder "$encoder" \
    --arg bitrate_kbps "$bitrate" \
    --arg sample_rate "$sample_rate" \
    --arg channels "$channels" \
    --arg duration_s "$duration" \
    --arg bytes "$bytes" \
    --arg fallback_used "$fallback_used" \
    --arg warning "$warning" \
    --arg source_event_id "$source_event_id" \
    '{
      type:$type,
      timestamp_start:$timestamp_start,
      timestamp_end:$timestamp_end,
      started_at:$timestamp_start,
      ended_at:$timestamp_end,
      captured_at:$captured_at,
      source:$source,
      source_event_id:$source_event_id,
      source_device:$source_device,
      capture_profile:"quality_voice_v17_5",
      media_kind:"audio",
      format:$format,
      encoder:$encoder,
      bitrate_kbps:(if $bitrate_kbps == "" then null else ($bitrate_kbps|tonumber) end),
      sample_rate_hz:($sample_rate|tonumber),
      channels:($channels|tonumber),
      duration_s:($duration_s|tonumber),
      bytes:($bytes|tonumber),
      audio_priority:"quality_first",
      expected_downstream:["whisper_asr","speechbrain_voice_match","brainlive_turns","brain2_offline_assembly"],
      fallback_used:($fallback_used == "1"),
      quality_warning:(if $warning == "" then null else $warning end)
    }' > "$meta"
}

capture_with_termux() {
  local out="$1"
  local encoder="$2"
  local bitrate="$3"
  termux-microphone-record \
    -f "$out" \
    -l "${AUDIO_SECONDS:-4}" \
    -e "$encoder" \
    -b "$bitrate" \
    -r "${AUDIO_SAMPLE_RATE:-16000}" \
    -c "${AUDIO_CHANNELS:-1}" >/dev/null 2>>"$LOGS/audio.err"
}

log "audio loop start AUDIO_SECONDS=${AUDIO_SECONDS:-4} format=${AUDIO_FORMAT:-wav} sample_rate=${AUDIO_SAMPLE_RATE:-16000} channels=${AUDIO_CHANNELS:-1}"
while true; do
  if [ "${ENABLE_AUDIO:-1}" != "1" ]; then
    sleep 5
    continue
  fi

  if ! storage_ok_for_audio; then
    log "audio capture paused: low storage or too much audio backlog free_mb=$(free_mb) pending=$(pending_count "$SPOOL/audio_pending")"
    sleep 5
    continue
  fi

  id="audio_$(now_id)_${RANDOM}"
  wanted_format="${AUDIO_FORMAT:-wav}"
  started="$(date -Iseconds)"
  start_epoch=$(date +%s)

  final_ext=".$wanted_format"
  [ "$wanted_format" = "wav" ] && final_ext=".wav"
  [ "$wanted_format" = "m4a" ] && final_ext=".m4a"
  tmp="$SPOOL/audio_pending/${id}${final_ext}.tmp"
  final="$SPOOL/audio_pending/${id}${final_ext}"
  meta="$SPOOL/audio_pending/${id}.json"

  ok=0
  fallback_used=0
  warning=""
  encoder="${AUDIO_ENCODER:-wav}"
  bitrate="${AUDIO_BITRATE_KBPS:-256}"
  format="$wanted_format"

  # Quality-first path: try true WAV/PCM if configured.  Some Termux:API builds
  # do not produce real RIFF/WAVE even when the file extension is .wav, so we
  # validate the header before accepting it.
  if [ "$wanted_format" = "wav" ]; then
    if capture_with_termux "$tmp" "${AUDIO_WAV_ENCODER:-wav}" "${AUDIO_BITRATE_KBPS:-256}"; then
      end_call_epoch=$(date +%s)
      elapsed=$((end_call_epoch - start_epoch))
      remaining=$(( ${AUDIO_SECONDS:-4} - elapsed ))
      [ "$remaining" -gt 0 ] && sleep "$remaining"
      sleep "${AUDIO_SETTLE_SECONDS:-0.25}"
      if wait_file_ready "$tmp" "${AUDIO_MIN_BYTES:-12000}" 8 && is_wav_file "$tmp"; then
        ok=1
        encoder="pcm_s16le"
        bitrate=""
        format="wav"
      else
        rm -f "$tmp"
        warning="termux_wav_not_available_fallback_used"
      fi
    else
      rm -f "$tmp"
      warning="termux_wav_capture_failed_fallback_used"
    fi

    if [ "$ok" != "1" ]; then
      if [ "${AUDIO_REQUIRE_WAV:-0}" = "1" ]; then
        log "audio WAV required but unavailable; skipping chunk"
        rm -f "$tmp" "$meta"
        sleep 1
        continue
      fi
      # Fallback keeps the bridge alive on Android builds where Termux:API only
      # exposes MediaRecorder encoders.  Use high bitrate AAC to preserve voice
      # quality as much as possible; PC/BrainLive can decode it if needed.
      final_ext=".m4a"
      tmp="$SPOOL/audio_pending/${id}${final_ext}.tmp"
      final="$SPOOL/audio_pending/${id}${final_ext}"
      encoder="${AUDIO_FALLBACK_ENCODER:-aac}"
      bitrate="${AUDIO_FALLBACK_BITRATE_KBPS:-256}"
      format="m4a"
      fallback_used=1
      started="$(date -Iseconds)"
      start_epoch=$(date +%s)
      if capture_with_termux "$tmp" "$encoder" "$bitrate"; then
        end_call_epoch=$(date +%s)
        elapsed=$((end_call_epoch - start_epoch))
        remaining=$(( ${AUDIO_SECONDS:-4} - elapsed ))
        [ "$remaining" -gt 0 ] && sleep "$remaining"
        sleep "${AUDIO_SETTLE_SECONDS:-0.25}"
        if wait_file_ready "$tmp" "${AUDIO_MIN_BYTES:-12000}" 8; then
          ok=1
        fi
      fi
    fi
  else
    if capture_with_termux "$tmp" "$encoder" "$bitrate"; then
      end_call_epoch=$(date +%s)
      elapsed=$((end_call_epoch - start_epoch))
      remaining=$(( ${AUDIO_SECONDS:-4} - elapsed ))
      [ "$remaining" -gt 0 ] && sleep "$remaining"
      sleep "${AUDIO_SETTLE_SECONDS:-0.25}"
      if wait_file_ready "$tmp" "${AUDIO_MIN_BYTES:-12000}" 8; then
        ok=1
      fi
    fi
  fi

  if [ "$ok" = "1" ]; then
    ended="$(date -Iseconds)"
    write_audio_meta "$meta" "audio" "$started" "$ended" "$tmp" "$encoder" "$bitrate" "${AUDIO_SAMPLE_RATE:-16000}" "${AUDIO_CHANNELS:-1}" "$format" "$fallback_used" "$warning" "$(capture_source_event_id audio "$id")"
    mv "$tmp" "$final"
  else
    rm -f "$tmp" "$meta"
    log "audio capture produced no stable usable file"
    termux-microphone-record -q >/dev/null 2>&1 || true
    sleep 1
  fi
  # Pas de sleep volontaire: on relance directement le chunk suivant pour limiter les trous.
done
