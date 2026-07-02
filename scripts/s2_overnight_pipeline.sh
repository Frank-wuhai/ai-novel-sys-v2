#!/usr/bin/env bash
# 在 Ch4 background 跑完后自动接续：
# 1. 判断 Ch4 是否自动闭环
# 2. 若否，走 s2_manual_promote_ch.py 4 兜底
# 3. 串跑 Ch5→Ch6→Ch7（每章 12 轮 + 卡住兜底 + 再 4 轮确认）
# 4. 生成综合 report
set -uo pipefail
cd /home/frank/ai-novel-system-v2

BOOK_ID=3
LOG_DIR="logs/baseline/s2_overnight_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$LOG_DIR"
REPORT="$LOG_DIR/report.md"

log() {
    echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG_DIR/pipeline.log"
}

check_chapter() {
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
        print('missing|missing|0|False')
    else:
        v = s.scalars(select(ChapterVersion).where(ChapterVersion.chapter_id==ch.id, ChapterVersion.status!='discarded').order_by(ChapterVersion.id.desc())).first()
        q = s.scalar(select(QualityReport).where(QualityReport.chapter_version_id==v.id).order_by(QualityReport.id.desc())) if v else None
        print(f'{ch.status}|{v.status if v else \"none\"}|{q.score if q else 0}|{q.passed if q else False}')
" 2>/dev/null
}

echo "# S2 Overnight Report" > "$REPORT"
echo "" >> "$REPORT"
echo "Started: $(date)" >> "$REPORT"
echo "Branch: $(git branch --show-current) @ $(git rev-parse --short HEAD)" >> "$REPORT"
echo "" >> "$REPORT"
echo "| Ch | Rounds | Chapter | Version | Score | Passed | Fallback |" >> "$REPORT"
echo "|----|--------|---------|---------|-------|--------|----------|" >> "$REPORT"

# Ch4 特殊：先看当前状态（可能之前 background 已跑）
STATUS_RAW=$(check_chapter 4)
IFS='|' read -r C_STATUS V_STATUS SCORE PASSED <<< "$STATUS_RAW"
log "Ch4 initial: chapter=$C_STATUS version=$V_STATUS score=$SCORE passed=$PASSED"

FALLBACK4="no"
if [ "$C_STATUS" != "approved" ] && [ "$C_STATUS" != "needs_confirmation" ] && [ "$C_STATUS" != "published" ] && [ "$C_STATUS" != "continuity_recorded" ]; then
    log "Ch4 需人工兜底"
    venv/bin/python scripts/s2_manual_promote_ch.py 4 > "$LOG_DIR/ch4_fallback.log" 2>&1
    FALLBACK4="yes"
    log "Ch4 兜底完成, 再跑 2 轮 drive 确认"
    bash scripts/drive_chapter.sh $BOOK_ID 4 2 > "$LOG_DIR/ch4_confirm.log" 2>&1
    STATUS_RAW=$(check_chapter 4)
    IFS='|' read -r C_STATUS V_STATUS SCORE PASSED <<< "$STATUS_RAW"
fi
echo "| 4 | N/A | $C_STATUS | $V_STATUS | $SCORE | $PASSED | $FALLBACK4 |" >> "$REPORT"

for CH in 5 6 7; do
    log "=== Chapter $CH ==="
    LOGFILE="$LOG_DIR/ch${CH}.log"
    bash scripts/drive_chapter.sh $BOOK_ID $CH 12 > "$LOGFILE" 2>&1

    STATUS_RAW=$(check_chapter $CH)
    IFS='|' read -r C_STATUS V_STATUS SCORE PASSED <<< "$STATUS_RAW"
    log "  after drive: chapter=$C_STATUS version=$V_STATUS score=$SCORE passed=$PASSED"

    FB="no"
    if [ "$C_STATUS" != "approved" ] && [ "$C_STATUS" != "needs_confirmation" ] && [ "$C_STATUS" != "published" ] && [ "$C_STATUS" != "continuity_recorded" ]; then
        venv/bin/python scripts/s2_manual_promote_ch.py $CH > "$LOG_DIR/ch${CH}_fallback.log" 2>&1
        FB="yes"
        bash scripts/drive_chapter.sh $BOOK_ID $CH 4 >> "$LOGFILE" 2>&1
        STATUS_RAW=$(check_chapter $CH)
        IFS='|' read -r C_STATUS V_STATUS SCORE PASSED <<< "$STATUS_RAW"
        log "  after fallback: chapter=$C_STATUS version=$V_STATUS score=$SCORE passed=$PASSED"
    fi

    ROUNDS=$(grep -c "^--- round " "$LOGFILE" 2>/dev/null || echo 0)
    echo "| $CH | $ROUNDS | $C_STATUS | $V_STATUS | $SCORE | $PASSED | $FB |" >> "$REPORT"
done

echo "" >> "$REPORT"
echo "## Git log" >> "$REPORT"
git log --oneline -10 >> "$REPORT"

log "=== DONE ==="
cat "$REPORT"
