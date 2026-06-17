#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/frank/ai-novel-system-v2"
cd "$ROOT"

"$ROOT/venv/bin/python" "$ROOT/scripts/run_regressions.py" --skip-smoke --trash-after-pass --trash-label finish-iteration
