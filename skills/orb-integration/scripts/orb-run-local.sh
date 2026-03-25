#!/usr/bin/env bash
set -euo pipefail
if [ $# -lt 1 ]; then
  echo "Usage: $0 \"task text\" [ollama-model]" >&2
  exit 1
fi
TASK="$1"
MODEL="${2:-}"
if [ -n "$MODEL" ]; then
  orb --local-only --ollama-model "$MODEL" "$TASK"
else
  orb --local-only "$TASK"
fi
