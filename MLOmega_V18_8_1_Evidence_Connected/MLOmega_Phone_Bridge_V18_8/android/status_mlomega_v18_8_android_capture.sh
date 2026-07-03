#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
source "$DIR/lib_mlomega_android.sh"

echo "== PIDs =="
for p in "$RUN"/*.pid; do
  [ -e "$p" ] || continue
  pid="$(cat "$p" 2>/dev/null || true)"
  name="$(basename "$p" .pid)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    echo "$name: RUNNING pid=$pid"
  else
    echo "$name: DEAD"
  fi
done

echo "== Spool =="
echo "audio_pending: $(find "$SPOOL/audio_pending" -type f 2>/dev/null | wc -l)"
echo "image_pending: $(find "$SPOOL/image_pending" -type f 2>/dev/null | wc -l)"
echo "gps_pending:   $(find "$SPOOL/gps_pending" -type f 2>/dev/null | wc -l)"

echo "== PC status =="
curl -sS -H "X-MLomega-Token: ${TOKEN}" "${API_BASE}/status" | jq . || true
