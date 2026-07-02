#!/usr/bin/env bash
# Sprint 2 通宵串跑脚本
# 用法: bash scripts/s2_overnight_pipeline.sh
#
# 逻辑：
#   for ch in 4 5 6 7:
#     跑一轮 drive_chapter.sh $ch 12
#     检查 chapter.status
#       - approved / needs_confirmation → 继续下一章
#       - 卡住（needs_revision 且 accept_early_stop 未触发）→ 跑 s2_manual_promote 兜底
#       - 兜底后再验一次；仍卡住 → 记录并 skip 到下一章
#   最后输出综合报告 + git log
set -uo pipefail
cd /home/frank/ai-novel-system-v2

BOOK_ID=3
LOG_DIR="logs/baseline/s2_overnight_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$LOG_DIR"
REPORT="$LOG_DIR/report.md"

log() {
    echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG_DIR/pipeline.log"
}

check_chapter_status() {
    local ch=$1
    venv/bin/python -c "
from app.db.session import configure_database, session_scope
from app.core.config import settings
from app.models.entities import Chapter, ChapterVersion, QualityReport
from sqlalchemy import select
configure_database(settings.database_url)
with session_scope() as s:
    ch = s.scalar(select(Chapter).where(Chapter.book_id==${BOOK_ID}, Chapter.chapter_number==${ch}))
    if not ch:
        print('missing')
    else:
        v = s.scalars(select(ChapterVersion).where(ChapterVersion.chapter_id==ch.id).order_by(ChapterVersion.id.desc())).first()
        q = s.scalar(select(QualityReport).where(QualityReport.chapter_version_id==v.id).order_by(QualityReport.id.desc())) if v else None
        print(f'{ch.status}|{v.status if v else None}|{q.score if q else None}|{q.passed if q else None}')
"
}

manual_promote_fallback() {
    local ch=$1
    log "  fallback: 运行 s2_manual_promote for ch=${ch}"
    venv/bin/python /tmp/s2_manual_promote_ch.py $ch > "$LOG_DIR/ch${ch}_fallback.log" 2>&1
}

echo "# S2 Overnight Pipeline Report" > "$REPORT"
echo "" >> "$REPORT"
echo "Started: $(date)" >> "$REPORT"
echo "Branch: $(git branch --show-current) @ $(git rev-parse --short HEAD)" >> "$REPORT"
echo "" >> "$REPORT"
echo "| Ch | Rounds | Final Status | Version Status | Score | Passed | Fallback? |" >> "$REPORT"
echo "|----|--------|--------------|----------------|-------|--------|-----------|" >> "$REPORT"

for CH in 4 5 6 7; do
    log "=== Chapter $CH ==="
    LOGFILE="$LOG_DIR/ch${CH}.log"
    bash scripts/drive_chapter.sh $BOOK_ID $CH 12 > "$LOGFILE" 2>&1

    STATUS_RAW=$(check_chapter_status $CH)
    IFS='|' read -r C_STATUS V_STATUS SCORE PASSED <<< "$STATUS_RAW"
    log "  after drive: chapter=$C_STATUS version=$V_STATUS score=$SCORE passed=$PASSED"

    FALLBACK="no"
    if [ "$C_STATUS" != "approved" ] && [ "$C_STATUS" != "needs_confirmation" ] && [ "$C_STATUS" != "published" ]; then
        if [ -f /tmp/s2_manual_promote_ch.py ]; then
            manual_promote_fallback $CH
            FALLBACK="yes"
            # rerun 2 轮 drive to confirm
            bash scripts/drive_chapter.sh $BOOK_ID $CH 4 >> "$LOGFILE" 2>&1
            STATUS_RAW=$(check_chapter_status $CH)
            IFS='|' read -r C_STATUS V_STATUS SCORE PASSED <<< "$STATUS_RAW"
            log "  after fallback: chapter=$C_STATUS version=$V_STATUS score=$SCORE passed=$PASSED"
        else
            log "  no fallback script; skipping"
        fi
    fi

    ROUNDS=$(grep -c "^--- round " "$LOGFILE" || echo 0)
    echo "| $CH | $ROUNDS | $C_STATUS | $V_STATUS | $SCORE | $PASSED | $FALLBACK |" >> "$REPORT"
done

echo "" >> "$REPORT"
echo "" >> "$REPORT"
echo "## Git log this session" >> "$REPORT"
git log --oneline -10 >> "$REPORT"

log "=== DONE ==="
log "Report: $REPORT"
cat "$REPORT"
