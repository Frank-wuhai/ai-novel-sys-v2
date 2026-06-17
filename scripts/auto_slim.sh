#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/frank/ai-novel-system-v2"
cd "$ROOT"

"$ROOT/venv/bin/python" "$ROOT/scripts/system_trash.py" \
  --auto-slim \
  --apply \
  --label scheduled-auto-slim \
  --max-workspace-mb 450 \
  --backup-keep-latest 5 \
  --backup-retention-days 3 \
  --trash-retention-days 2 \
  --log-retention-days 7
