#!/usr/bin/env bash
# Sprint 2 Phase B (resume): Ch28-Ch30 after P2-Ch27 fix (297edbc).
# Ch21-Ch27 已完成 (前次 batch 20260704_180822)。
set -u

BOOK=3
BUDGET=10
OUTDIR="logs/baseline/s2_pb_batch_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUTDIR"

echo "=== Phase B resume: Ch28-Ch30 (post P2-Ch27 fix 297edbc) ==="
echo "outdir=$OUTDIR"

for CH in 28 29 30; do
  echo ""
  echo "--- Ch$CH start $(date -Iseconds) ---"
  bash scripts/drive_chapter.sh "$BOOK" "$CH" "$BUDGET" \
    > "$OUTDIR/ch${CH}.log" 2>&1
  RC=$?
  echo "--- Ch$CH done $(date -Iseconds) exit=$RC ---"

  # 简报
  venv/bin/python << PYEOF
from app.db.session import configure_database, session_scope
from app.core.config import settings
from app.models.entities import Chapter, ChapterVersion, QualityReport
from sqlalchemy import select

configure_database(settings.database_url)
with session_scope() as s:
    ch = s.execute(select(Chapter).where(Chapter.book_id==$BOOK, Chapter.chapter_number==$CH)).scalar_one_or_none()
    if not ch:
        print(f"  Ch$CH: no chapter row"); import sys; sys.exit(0)
    vs = list(s.execute(select(ChapterVersion).where(ChapterVersion.chapter_id==ch.id).order_by(ChapterVersion.version_number.desc())).scalars())
    if not vs:
        print(f"  Ch$CH: status={ch.status} no versions"); import sys; sys.exit(0)
    v = vs[0]
    qr = s.execute(select(QualityReport).where(QualityReport.version_id==v.id).order_by(QualityReport.id.desc())).scalars().first()
    score = qr.score if qr else None
    passed = qr.passed if qr else None
    print(f"  Ch$CH: status={ch.status} latest_v={v.version_number} v.status={v.status} score={score} passed={passed} n_versions={len(vs)}")
PYEOF
done

echo ""
echo "=== Phase B resume complete $(date -Iseconds) ==="
