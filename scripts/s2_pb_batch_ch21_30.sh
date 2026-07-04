#!/usr/bin/env bash
# Sprint 2 Phase B: Ch21-Ch30 长跑（arc 跨越验证 + 稳定性基线）
# 前置：commit d24d391（Phase A 完成）
# 期望：每章 15-30 min，auto 率 ≥ 90%
set -u

BOOK=3
BUDGET=10
OUTDIR="logs/baseline/s2_pb_batch_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUTDIR"

echo "=== Phase B batch: Ch21-Ch30 (post P2 stabilization d24d391) ==="
echo "outdir=$OUTDIR"

for CH in 21 22 23 24 25 26 27 28 29 30; do
  LOG="$OUTDIR/ch${CH}.log"
  echo "--- Ch$CH start $(date +%Y-%m-%dT%H:%M:%S%z) ---" | tee -a "$OUTDIR/summary.log"
  bash scripts/drive_chapter.sh "$BOOK" "$CH" 25 > "$LOG" 2>&1
  EC=$?
  echo "--- Ch$CH done $(date +%Y-%m-%dT%H:%M:%S%z) exit=$EC ---" | tee -a "$OUTDIR/summary.log"

  # 收集状态
  STAT=$(venv/bin/python <<PYEOF
from app.db.session import configure_database, session_scope
from app.core.config import settings
from app.models.entities import Chapter, ChapterVersion, QualityReport
from sqlalchemy import select, func
configure_database(settings.database_url)
with session_scope() as s:
    ch = s.scalar(select(Chapter).where(Chapter.book_id==$BOOK, Chapter.chapter_number==$CH))
    if not ch:
        print(f'  Ch$CH: no chapter row')
    else:
        vs = list(s.scalars(select(ChapterVersion).where(ChapterVersion.chapter_id==ch.id, ChapterVersion.status!='discarded').order_by(ChapterVersion.version_number.desc())))
        latest = vs[0] if vs else None
        q = s.scalar(select(QualityReport).where(QualityReport.chapter_version_id==latest.id).order_by(QualityReport.id.desc())) if latest else None
        print(f'  Ch$CH: status={ch.status} latest_v={latest.version_number if latest else "-"} v.status={latest.status if latest else "-"} score={q.score if q else "-"} passed={q.passed if q else "-"} n_versions={len(vs)}')
PYEOF
)
  echo "$STAT" | tee -a "$OUTDIR/summary.log"
done

echo "=== batch done $(date +%Y-%m-%dT%H:%M:%S%z) ===" | tee -a "$OUTDIR/summary.log"
echo "logs: $OUTDIR"
