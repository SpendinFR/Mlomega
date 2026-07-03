#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

pkg update -y
pkg install -y termux-api curl jq coreutils procps findutils imagemagick
termux-setup-storage || true

BASE="$HOME/mlomega_phone_bridge"
mkdir -p "$BASE" "$BASE/spool/audio_pending" "$BASE/spool/image_pending" "$BASE/spool/gps_pending" "$BASE/spool/sent" "$BASE/spool/failed" "$BASE/run" "$BASE/logs"

if [ ! -f "$HOME/mlomega_android_config.env" ]; then
  cp ./mlomega_android_config.env.example "$HOME/mlomega_android_config.env"
  echo "Config créée: $HOME/mlomega_android_config.env"
  echo "Modifie API_BASE et TOKEN dedans."
else
  echo "Config déjà présente: $HOME/mlomega_android_config.env"
fi

echo "OK installation Termux. Pense à désactiver l'optimisation batterie pour Termux, Termux:API et Tailscale."
