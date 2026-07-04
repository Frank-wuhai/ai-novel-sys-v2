#!/usr/bin/env bash
# Sprint 2 P1-3 stage-8 后：Ch13-15 长跑
set -u

BOOK=3
BUDGET=10
OUTDIR="logs/baseline/s2_batch_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUTDIR"

echo "=== Sprint 2 P1-3 stage-8 batch: Ch13-Ch15 ==="
echo "outdir=$OUTDIR"

for CH in 13 14 15; do
    echo "--- Ch$CH start $(date -Iseconds) ---"
    bash scripts/drive_chapter.sh $BOOK $CH $BUDGET > "$OUTDIR/ch$CH.log" 2>&1
    RET=$?
    echo "--- Ch$CH done $(date -Iseconds) exit=$RET ---"

    venv/bin/python -c "
from app.db.session import configure_database, session_scope
from app.core.config import settings
from app.models.entities import Chapter, ChapterVersion, QualityReport
from sqlalchemy import select
configure_database(settings.database_url)
with session_scope() as s:
    ch = s.scalar(select(Chapter).where(Chapter.book_id==$BOOK, Chapter.chapter_number==$CH))
    if ch:
        vs = list(s.scalars(select(ChapterVersion).where(ChapterVersion.chapter_id==ch.id, ChapterVersion.status!='discarded').order_by(ChapterVersion.id.desc())))
        v = vs[0] if vs else None
        q = s.scalar(select(QualityReport).where(QualityReport.chapter_version_id==v.id).order_by(QualityReport.id.desc())) if v else None
        rp = list(s.scalars(select(ChapterVersion).where(ChapterVersion.chapter_id==ch.id, ChapterVersion.status=='reviewed_pass')))
        auto_ok = 'YES' if ch.status in ('needs_confirmation','approved','continuity_recorded','published') and rp else 'NO'
        print(f'STAT Ch$CH ch_status={ch.status} versions={len(vs)} v_id={v.id if v else None} score={q.score if q else None} passed={q.passed if q else None} reviewed_pass_count={len(rp)} auto={auto_ok}')
" 2>&1 | tail -1
    echo ""
done

echo "=== batch complete $(date -Iseconds) ==="
echo "logs in: $OUTDIR"
