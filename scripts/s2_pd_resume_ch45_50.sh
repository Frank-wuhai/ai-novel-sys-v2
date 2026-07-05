#!/usr/bin/env bash
# Phase D resume: Ch45-Ch50 after manual Ch44 fix (open-loop chapter_type_gate override).
set -u
BOOK=3
BUDGET=10
OUTDIR="logs/baseline/s2_pd_batch_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUTDIR"

echo "=== Phase D resume: Ch45-Ch50 ==="
echo "outdir=$OUTDIR"

for CH in 45 46 47 48 49 50; do
  echo ""
  echo "--- Ch$CH start $(date -Iseconds) ---"
  bash scripts/drive_chapter.sh "$BOOK" "$CH" "$BUDGET" \
    > "$OUTDIR/ch${CH}.log" 2>&1
  RC=$?
  echo "--- Ch$CH done $(date -Iseconds) exit=$RC ---"
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
    vs = list(s.execute(select(ChapterVersion).where(ChapterVersion.chapter_id==ch.id)).scalars())
    promoted = [v for v in vs if v.status in ("reviewed_pass","approved")]
    pv = promoted[0] if promoted else None
    score = None
    if pv:
        qr = s.execute(select(QualityReport).where(QualityReport.chapter_version_id==pv.id).order_by(QualityReport.id.desc())).scalars().first()
        score = qr.score if qr else None
    n_v = len(vs)
    pv_str = f"v{pv.version_number}" if pv else "-"
    print(f"  Ch$CH: status={ch.status} n_v={n_v} promoted={pv_str} score={score}")
PYEOF
done

echo ""
echo "=== Phase D resume complete $(date -Iseconds) ==="
venv/bin/python << 'PYEOF'
from app.db.session import configure_database, session_scope
from app.core.config import settings
from app.models.entities import Chapter, ChapterVersion, QualityReport
from sqlalchemy import select

configure_database(settings.database_url)
with session_scope() as s:
    print(f"\n{'Ch':>3} {'status':>22} {'n_v':>4} {'promoted':>10} {'score':>6}")
    ok = 0
    for CH in range(41, 51):
        ch = s.execute(select(Chapter).where(Chapter.book_id==3, Chapter.chapter_number==CH)).scalar_one_or_none()
        if not ch:
            print(f"{CH:>3} MISSING"); continue
        vs = list(s.execute(select(ChapterVersion).where(ChapterVersion.chapter_id==ch.id)).scalars())
        promoted = [v for v in vs if v.status in ("reviewed_pass","approved")]
        pv = promoted[0] if promoted else None
        score = None
        if pv:
            qr = s.execute(select(QualityReport).where(QualityReport.chapter_version_id==pv.id).order_by(QualityReport.id.desc())).scalars().first()
            score = qr.score if qr else None
        if ch.status == "continuity_recorded":
            ok += 1
        print(f"{CH:>3} {ch.status:>22} {len(vs):>4} {'v'+str(pv.version_number) if pv else '-':>10} {score if score else '-':>6}")
    print(f"\n=== Phase D final auto rate: {ok}/10 ===")
PYEOF
