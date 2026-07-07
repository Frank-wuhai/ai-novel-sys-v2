#!/usr/bin/env bash
# 长跑 driver: 并行启 worker + cycle-loop 跑 book 的 [start..start+count-1] 章节，
# 直到所有目标章节都到 `continuity_recorded` 或更后（Sprint2 Phase E 后 approve_chapter
# 已从 MANUAL_ACTIONS 移除，continuity_recorded 就是内容层终态；mark_publish_job
# 是番茄真发环节，不在此脚本目标内）。
#
# 用法:
#   scripts/drive_book_range.sh <book_id> <start> <count> [max_hours]
# 例:
#   scripts/drive_book_range.sh 2 6 7 8
#
# 特性:
# 1. worker + cycle-loop 双后台并行（cycle 是 orchestrator，worker 是 executor）
# 2. cycle-loop 每 60s 一 iter；跑完检查 [start..start+count-1] 章节的终态数
# 3. 达到目标 count 就 break，两条后台一起收
# 4. 兜底 max_hours（默认 8h）到时间强退
# 5. 日志分离: worker → /tmp/drive-b<book>-r<start>-<count>-worker.log
#              cycle  → /tmp/drive-b<book>-r<start>-<count>-cycle.log
# 6. exit 0 = 全绿；exit 1 = 超时未达标；exit 2 = 参数不对
set -euo pipefail

if [[ $# -lt 3 ]]; then
  echo "usage: $0 <book_id> <start> <count> [max_hours]" >&2
  exit 2
fi

BOOK_ID="$1"
START="$2"
COUNT="$3"
MAX_HOURS="${4:-8}"
END=$((START + COUNT - 1))

DB="${AI_NOVEL_DATABASE_URL:-sqlite:///data/novel.db}"
DB_PATH="${DB#sqlite:///}"
TAG="b${BOOK_ID}-r${START}-${COUNT}"
WORKER_LOG="/tmp/drive-${TAG}-worker.log"
CYCLE_LOG="/tmp/drive-${TAG}-cycle.log"

# 终态白名单: content 层已完工的章节状态
# continuity_recorded = Phase E 内容层终态；approved / published_* 是历史 / 番茄真发后
TERMINAL_STATES=("continuity_recorded" "approved" "published" "publish_scheduled" "publish_completed")

# 构造 SQL IN 列表
terminal_sql_list=$(printf "'%s'," "${TERMINAL_STATES[@]}")
terminal_sql_list="${terminal_sql_list%,}"

count_terminal() {
  sqlite3 "$DB_PATH" "select count(*) from chapters where book_id=$BOOK_ID and chapter_number between $START and $END and status in ($terminal_sql_list);"
}

echo "=== drive_book_range start $(date -Iseconds) book=$BOOK_ID chapters=$START..$END target_count=$COUNT max_hours=$MAX_HOURS ==="
echo "worker_log=$WORKER_LOG"
echo "cycle_log=$CYCLE_LOG"

# --- 启 worker ---
(
  venv/bin/python -m app.cli --database-url "$DB" run-generation-worker \
    --max-loops 3600 \
    --sleep-seconds 5 \
    --max-tasks-per-loop 1 \
    --recover-stale-before-run \
    --task-timeout-seconds 3600 \
    > "$WORKER_LOG" 2>&1
) &
WORKER_PID=$!
echo "worker started pid=$WORKER_PID"

cleanup() {
  echo "=== cleanup: killing worker pid=$WORKER_PID ==="
  kill "$WORKER_PID" 2>/dev/null || true
  sleep 2
  kill -9 "$WORKER_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# --- cycle-loop ---
DEADLINE=$(($(date +%s) + MAX_HOURS * 3600))
ITER=0

while : ; do
  ITER=$((ITER + 1))
  NOW=$(date +%s)
  if [[ $NOW -ge $DEADLINE ]]; then
    echo "=== TIMEOUT after ${MAX_HOURS}h at iter=$ITER ==="
    exit 1
  fi

  echo "=== cycle-iter=$ITER $(date -Iseconds) ===" | tee -a "$CYCLE_LOG"
  # cycle 内部偶尔会撞 SQLite 瞬时锁 (database is locked) 或其他瞬时异常。
  # 不能因为一次瞬时错误就整个 driver 挂掉（旧 inline loop 没 set -e 所以自愈）。
  # 用 || true 吞掉非零退出，下一 iter 60s 后重试；连续失败会在 terminal_count
  # 长时间不动时被 deadline 兜底掉。
  set +e
  venv/bin/python -m app.cli --database-url "$DB" run-book-cycle \
    --book-id "$BOOK_ID" --start "$START" --count "$COUNT" --max-steps 60 \
    >> "$CYCLE_LOG" 2>&1
  CYCLE_RC=$?
  set -e
  if [[ $CYCLE_RC -ne 0 ]]; then
    echo "cycle-iter=$ITER exited rc=$CYCLE_RC (transient? retry next iter)" | tee -a "$CYCLE_LOG"
  fi

  DONE=$(count_terminal)
  echo "terminal_count=$DONE / target=$COUNT" | tee -a "$CYCLE_LOG"

  if [[ "$DONE" == "$COUNT" ]]; then
    echo "=== ALL_${COUNT}_TERMINAL_REACHED at iter=$ITER $(date -Iseconds) ===" | tee -a "$CYCLE_LOG"
    exit 0
  fi

  sleep 60
done
