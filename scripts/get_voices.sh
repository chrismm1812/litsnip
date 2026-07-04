#!/bin/sh
# Download the Piper neural voices litsnip uses (~300 MB total).
# Voices are not committed to git; run this once after cloning.
set -e
cd "$(dirname "$0")/.." && mkdir -p voices && cd voices
base="https://huggingface.co/rhasspy/piper-voices/resolve/main"
for spec in \
  "fr/fr_FR/siwis/medium/fr_FR-siwis-medium" \
  "de/de_DE/thorsten/medium/de_DE-thorsten-medium" \
  "el/el_GR/rapunzelina/low/el_GR-rapunzelina-low" \
  "it/it_IT/paola/medium/it_IT-paola-medium" \
  "en/en_US/lessac/medium/en_US-lessac-medium"; do
  name=$(basename "$spec")
  [ -f "$name.onnx" ] && { echo "$name: already present"; continue; }
  echo "fetching $name…"
  curl -sL "$base/$spec.onnx" -o "$name.onnx"
  curl -sL "$base/$spec.onnx.json" -o "$name.onnx.json"
done
echo "voices ready."
