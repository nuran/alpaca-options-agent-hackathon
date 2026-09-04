#!/usr/bin/env bash
# Render the deck to PDF with headless Chrome -- the same HTML -> Chromium -> PDF path
# that produced the original. No network: Inter resolves from the system font list and
# the serif falls back to Georgia, both of which embed into the PDF.
#
# Chrome writes the PDF and then does not always exit, so it is backgrounded and killed
# once the file stops growing. Waiting on the process instead would hang the build.
set -euo pipefail
cd "$(dirname "$0")"

CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
[ -x "$CHROME" ] || { echo "Chrome not found at: $CHROME (set CHROME=...)" >&2; exit 1; }

# Numbers first: a deck that has drifted from source must not render at all.
python3 "$PWD/verify_numbers.py"

OUT="$PWD/Live_Pipeline_Agent_Deck.pdf"
PROFILE="$(mktemp -d)"
rm -f "$OUT"

"$CHROME" --headless --disable-gpu --no-sandbox --no-pdf-header-footer \
  --user-data-dir="$PROFILE" --print-to-pdf="$OUT" \
  "file://$PWD/index.html" >/dev/null 2>&1 &
CHROME_PID=$!

for _ in $(seq 1 60); do
  sleep 0.5
  if [ -s "$OUT" ]; then
    a=$(wc -c < "$OUT"); sleep 0.5; b=$(wc -c < "$OUT")
    [ "$a" = "$b" ] && break
  fi
done
kill "$CHROME_PID" 2>/dev/null || true
wait "$CHROME_PID" 2>/dev/null || true
rm -rf "$PROFILE"

[ -s "$OUT" ] || { echo "render produced nothing" >&2; exit 1; }
command -v pdfinfo >/dev/null && pdfinfo "$OUT" | grep -E '^(Pages|Page size)'
echo "wrote $OUT"
