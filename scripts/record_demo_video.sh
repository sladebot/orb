#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-both}"                  # tui | dashboard | both
OUT_DIR="${2:-demos}"
DURATION="${DEMO_DURATION:-25}"    # seconds per clip
DISPLAY_ID="${DEMO_DISPLAY_ID:-}"  # macOS display index (optional)
PORT="${DEMO_DASHBOARD_PORT:-8080}"
QUERY="${DEMO_QUERY:-Build a tiny REST API with tests and explain design tradeoffs.}"
ORB_CMD="${ORB_CMD:-python -m orb.cli.main}"

if ! command -v screencapture >/dev/null 2>&1; then
  echo "error: macOS screencapture is required for video capture." >&2
  exit 1
fi

mkdir -p "$OUT_DIR"

record() {
  local output_file="$1"
  local run_cmd="$2"
  local rec_cmd=(screencapture -x -v -V"$DURATION")
  if [[ -n "$DISPLAY_ID" ]]; then
    rec_cmd+=(-D"$DISPLAY_ID")
  fi
  rec_cmd+=("$output_file")

  echo "Recording to: $output_file"
  # Start screen recording in the background.
  "${rec_cmd[@]}" &
  local rec_pid=$!
  # Give recorder a moment to initialize.
  sleep 1
  if ! kill -0 "$rec_pid" 2>/dev/null; then
    echo "error: screen recorder failed to start (check Screen Recording permission)." >&2
    return 1
  fi

  # Run demo command while recording.
  local run_status=0
  bash -lc "$run_cmd" || run_status=$?
  wait "$rec_pid"
  return "$run_status"
}

run_tui() {
  local out="$OUT_DIR/orb_tui_demo.mov"
  local cmd="$ORB_CMD --tui --timeout $DURATION --quiet -- \"$QUERY\""
  record "$out" "$cmd"
}

run_dashboard() {
  local out="$OUT_DIR/orb_dashboard_demo.mov"
  local cmd="$ORB_CMD --dashboard --dashboard-port $PORT --timeout $DURATION --quiet -- \"$QUERY\""
  record "$out" "$cmd"
}

case "$MODE" in
  tui)
    run_tui
    ;;
  dashboard)
    run_dashboard
    ;;
  both)
    run_tui
    run_dashboard
    ;;
  *)
    echo "usage: $0 [tui|dashboard|both] [output_dir]" >&2
    exit 1
    ;;
esac

echo "Done. Video files are in: $OUT_DIR"
