#!/usr/bin/env bash
# 反复交替 run-book-cycle + run-generation-worker，直到 chapter 完成或超步数。
set -euo pipefail
BOOK_ID="${1:-3}"
CHAP="${2:-1}"
MAX_ROUNDS="${3:-25}"
LOG="logs/baseline/drive-ch${CHAP}.log"
mkdir -p logs/baseline

echo "=== drive book=$BOOK_ID chapter=$CHAP starting $(date +%H:%M:%S) ==="
for i in $(seq 1 "$MAX_ROUNDS"); do
  echo "--- round $i ---" | tee -a "$LOG"
  venv/bin/python -m app.cli run-book-cycle --book-id "$BOOK_ID" --start "$CHAP" --count 1 --max-steps 8 --platform "番茄小说" 2>&1 | tee -a "$LOG"
  venv/bin/python -m app.cli run-generation-worker --max-loops 6 --sleep-seconds 3 --max-tasks-per-loop 2 --recover-stale-before-run --task-timeout-seconds 3600 2>&1 | tee -a "$LOG" | tail -5
  STATE=$(venv/bin/python -m app.cli plan-chapters --book-id "$BOOK_ID" --start "$CHAP" --count 1 2>&1 | tail -1)
  echo "STATE: $STATE" | tee -a "$LOG"
  echo "$STATE" | grep -qE "next_action=done|next_action=publish_" && { echo "chapter $CHAP reached terminal state"; break; }
done
echo "=== done $(date +%H:%M:%S) ==="
