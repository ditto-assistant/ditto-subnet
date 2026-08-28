#!/usr/bin/env zsh
# One bounded backroom review fire every INTERVAL seconds, driven by
# `opencode run` with the standing-authority fire prompt.
#
# Usage: review-loop.sh [interval-seconds] [fire-prompt.md]
#   interval defaults to 900 (15 minutes); the prompt defaults to
#   review-loop-prompt.md next to this script.
# Stop: touch "$LOOP_DIR/stop"
# Logs: "$LOOP_DIR/loop.log" and one "$LOOP_DIR/fire-<ts>.log" per fire.

set -u

INTERVAL="${1:-900}"
SCRIPT_DIR="${0:A:h}"
PROMPT_FILE="${2:-$SCRIPT_DIR/review-loop-prompt.md}"
REPO="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
LOOP_DIR="${BACKROOM_REVIEW_LOOP_DIR:-$HOME/.local/share/opencode/backroom-review-loop}"

if [[ -z "$REPO" ]]; then
  print -u2 "review-loop: must run from inside the ditto-subnet repository"
  exit 1
fi
if [[ ! -f "$PROMPT_FILE" ]]; then
  print -u2 "review-loop: fire prompt not found at $PROMPT_FILE"
  exit 1
fi

mkdir -p "$LOOP_DIR"
echo "$(date -u +%FT%TZ) loop started (pid $$, interval ${INTERVAL}s, prompt $PROMPT_FILE)" >> "$LOOP_DIR/loop.log"

while true; do
  if [[ -f "$LOOP_DIR/stop" ]]; then
    echo "$(date -u +%FT%TZ) stop file present, exiting" >> "$LOOP_DIR/loop.log"
    rm -f "$LOOP_DIR/stop"
    exit 0
  fi

  ts=$(date -u +%Y%m%dT%H%M%SZ)
  if mkdir "$LOOP_DIR/.lock" 2>/dev/null; then
    echo "$(date -u +%FT%TZ) fire start $ts" >> "$LOOP_DIR/loop.log"
    (
      cd "$REPO" || exit 1
      opencode run "$(cat "$PROMPT_FILE")"
    ) >> "$LOOP_DIR/fire-$ts.log" 2>&1
    rmdir "$LOOP_DIR/.lock"
    echo "$(date -u +%FT%TZ) fire end $ts" >> "$LOOP_DIR/loop.log"
  else
    echo "$(date -u +%FT%TZ) previous fire still running, skipped $ts" >> "$LOOP_DIR/loop.log"
  fi

  sleep "$INTERVAL"
done
