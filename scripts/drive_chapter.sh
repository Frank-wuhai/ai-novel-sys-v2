#!/usr/bin/env bash
# 反复交替 run-book-cycle + run-generation-worker，直到 chapter 完成或超步数。
# Worker 现在按 --book-id 过滤避免抢别的 book 的任务。
# Phase E.3: worker 阻塞式跑到 book 没有 running/pending task 才回 book-cycle，
# 避免长任务（rebuild_chapter_candidates）在 worker 生命周期外变成孤儿 running。
set -euo pipefail
BOOK_ID="${1:-3}"
CHAP="${2:-1}"
MAX_ROUNDS="${3:-25}"
# Worker 每轮上限：足够跑完一批 rebuild_candidates（每候选 ~10min × 3 = 30min），设 60min 兜底
WORKER_MAX_LOOPS="${WORKER_MAX_LOOPS:-360}"
WORKER_SLEEP="${WORKER_SLEEP:-10}"
LOG="logs/baseline/drive-book${BOOK_ID}-ch${CHAP}.log"
mkdir -p logs/baseline

echo "=== drive book=$BOOK_ID chapter=$CHAP starting $(date +%H:%M:%S) ===" | tee -a "$LOG"
for i in $(seq 1 "$MAX_ROUNDS"); do
  echo "--- round $i ---" | tee -a "$LOG"
  venv/bin/python -m app.cli run-book-cycle --book-id "$BOOK_ID" --start "$CHAP" --count 1 --max-steps 8 --platform "番茄小说" 2>&1 | tee -a "$LOG"
  # Worker 阻塞式跑，直到该 book 没有 pending/running task
  venv/bin/python -m app.cli run-generation-worker \
    --book-id "$BOOK_ID" \
    --max-loops "$WORKER_MAX_LOOPS" \
    --sleep-seconds "$WORKER_SLEEP" \
    --max-tasks-per-loop 2 \
    --recover-stale-before-run \
    --task-timeout-seconds 3600 \
    --stop-when-empty \
    2>&1 | tee -a "$LOG" | tail -5
  STATE=$(venv/bin/python -m app.cli plan-chapters --book-id "$BOOK_ID" --start "$CHAP" --count 1 2>&1 | tail -1)
  echo "STATE: $STATE" | tee -a "$LOG"
  echo "$STATE" | grep -qE "next_action=done|next_action=publish_" && { echo "chapter $CHAP reached terminal state" | tee -a "$LOG"; break; }
done
echo "=== done $(date +%H:%M:%S) ===" | tee -a "$LOG"
