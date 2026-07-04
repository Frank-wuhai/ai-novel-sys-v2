#!/usr/bin/env bash
# Sprint 2 P1-4 batch: Ch17-Ch20 (Ch16 已在前一轮跑通 v22=78 continuity_recorded)
# 前置修复：commit 482119c (urban intent alias) + e0acebd (candidate retry)
set -u

BOOK=3
BUDGET=10
OUTDIR="logs/baseline/s2_p14_batch_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUTDIR"

echo "=== Sprint 2 P1-4 batch: Ch17-Ch20 (post urban-intent-fix) ==="
echo "outdir=$OUTDIR"

for CH in 17 18 19 20; do
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
        n_all = s.scalar(select(ChapterVersion).where(ChapterVersion.chapter_id==ch.id)) is not None
        n_vs = len(list(s.scalars(select(ChapterVersion).where(ChapterVersion.chapter_id==ch.id))))
        print(f'  Ch$CH result: status={ch.status} latest_v={v.version_number if v else None} score={q.score if q else None} passed={q.passed if q else None} n_versions={n_vs}')
"

    # 若章节不是 continuity_recorded/published，跑不下去，中止后续章节
    STATUS=$(venv/bin/python -c "
from app.db.session import configure_database, session_scope
from app.core.config import settings
from app.models.entities import Chapter
from sqlalchemy import select
configure_database(settings.database_url)
with session_scope() as s:
    ch = s.scalar(select(Chapter).where(Chapter.book_id==$BOOK, Chapter.chapter_number==$CH))
    print(ch.status if ch else 'missing')
")
    if [[ "$STATUS" != "continuity_recorded" && "$STATUS" != "published" && "$STATUS" != "approved" && "$STATUS" != "needs_confirmation" ]]; then
        echo "!!! Ch$CH stuck at status=$STATUS, aborting batch"
        break
    fi
done

echo "=== batch done $(date -Iseconds) ==="
echo "logs: $OUTDIR"
