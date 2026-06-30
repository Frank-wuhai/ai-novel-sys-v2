from __future__ import annotations

import json

from sqlalchemy import select

from app.db.session import session_scope
from app.models.entities import Book, Chapter, ChapterBrief, ChapterVersion, QualityReport
from app.services.planning import plan_chapters
from app.services.reading_assessment import maybe_apply_reading_assessment
from regression_db import isolated_database


def main() -> int:
    isolated_database("reading-assessment-rebind-regression")
    failures: list[str] = []
    with session_scope() as session:
        book = Book(title="reading assessment rebind", genre="test", target_platform="manual")
        session.add(book)
        session.flush()
        chapter = Chapter(book_id=book.id, chapter_number=1, title="第1章", status="draft")
        session.add(chapter)
        session.flush()
        version = ChapterVersion(
            chapter_id=chapter.id,
            version_number=1,
            title="第1章",
            content="正文" * 1800,
            status="needs_revision",
            source="revision_compare_restore:v1",
        )
        session.add(version)
        session.flush()
        quality = QualityReport(
            chapter_version_id=version.id,
            score=88,
            passed=True,
            report=json.dumps(
                {
                    "status": "PASS",
                    "score": 88,
                    "passed": True,
                    "dimensions": {
                        "author_intent": 82,
                        "brief_coverage": 82,
                        "reader_momentum": 80,
                        "hook_strength": 80,
                        "payoff_grounding": 80,
                        "chapter_necessity": 80,
                        "chapter_unit_flow": 80,
                    },
                    "reading_assessment": {
                        "source": "reading_assessment@v1",
                        "quality_id": 999,
                        "revision_brief_id": 888,
                        "action": "auto_rebuild",
                        "level": "stale",
                        "label": "旧合同残留",
                        "summary": "旧版本残留，不应继续使用。",
                        "revision_mode": "fresh",
                        "preserve": [],
                        "improve": [],
                        "blockers": ["stale"],
                    },
                },
                ensure_ascii=False,
            ),
        )
        session.add(quality)
        session.flush()
        assessment = maybe_apply_reading_assessment(session, book_id=book.id, chapter_number=1, quality=quality)
        data = json.loads(quality.report)
        stored = data.get("reading_assessment") or {}
        if assessment.action != "approve_ready":
            failures.append(f"stale_assessment_not_recomputed:{assessment.action}")
        if int(stored.get("quality_id") or 0) != quality.id:
            failures.append(f"assessment_not_rebound:{stored}")
        refreshed = session.scalar(select(ChapterVersion).where(ChapterVersion.id == version.id))
        if refreshed.status != "reviewed_pass":
            failures.append(f"version_not_approved_after_recompute:{refreshed.status}")

        plateau_chapter = Chapter(book_id=book.id, chapter_number=2, title="第2章", status="draft")
        session.add(plateau_chapter)
        session.flush()
        session.add(
            ChapterBrief(
                chapter_id=plateau_chapter.id,
                goal="第2章测试 brief",
                required_beats="具体外部压力；桥段复刻；奖励和代价。",
                constraints="3000-4500 中文字符。",
                status="ready",
            )
        )
        session.flush()
        plateau_version = ChapterVersion(
            chapter_id=plateau_chapter.id,
            version_number=1,
            title="第2章",
            content="正文" * 1800,
            status="needs_revision",
            source="revision_compare_restore:v74",
        )
        session.add(plateau_version)
        session.flush()
        plateau_quality = QualityReport(
            chapter_version_id=plateau_version.id,
            score=74,
            passed=False,
            report=json.dumps(
                {
                    "status": "FAIL",
                    "score": 74,
                    "passed": False,
                    "hard_gate": {"status": "PASS"},
                    "dimensions": {
                        "author_intent": 65,
                        "brief_coverage": 47,
                        "reader_momentum": 66,
                        "scene_atmosphere": 43,
                        "dialogue_fullness": 49,
                        "chapter_unit_flow": 62,
                    },
                    "llm_review": {
                        "issues": ["场景氛围描写不足", "对白偏功能化"],
                        "revision_suggestions": ["补足场景空间、声音、触感", "让对白承担试探和情绪"],
                    },
                },
                ensure_ascii=False,
            ),
        )
        session.add(plateau_quality)
        session.flush()
        item = plan_chapters(session, book_id=book.id, start=2, count=1, apply_state_repairs=True)[0]
        plateau_data = json.loads(plateau_quality.report)
        plateau_assessment = plateau_data.get("reading_assessment") or {}
        active_brief = session.scalar(
            select(ChapterBrief)
            .where(ChapterBrief.chapter_id == plateau_chapter.id, ChapterBrief.status == "revision_ready")
            .order_by(ChapterBrief.id.desc())
        )
        if item.next_action != "revise_chapter":
            failures.append(f"plateau_plan_not_revision:{item.next_action}")
        if plateau_assessment.get("action") != "auto_revise":
            failures.append(f"plateau_assessment_not_created:{plateau_assessment}")
        if int(plateau_assessment.get("quality_id") or 0) != plateau_quality.id:
            failures.append(f"plateau_assessment_not_bound:{plateau_assessment}")
        if not active_brief or f"reading_assessment_auto_quality#{plateau_quality.id}" not in (active_brief.required_beats or ""):
            failures.append("plateau_revision_brief_missing_marker")
        if active_brief and len(active_brief.constraints or "") > 900:
            failures.append(f"plateau_revision_constraints_not_compact:{len(active_brief.constraints or '')}")

        local_patch_chapter = Chapter(book_id=book.id, chapter_number=3, title="第3章", status="draft")
        session.add(local_patch_chapter)
        session.flush()
        session.add(
            ChapterBrief(
                chapter_id=local_patch_chapter.id,
                goal="第3章测试 brief",
                required_beats="具体外部压力；桥段复刻；奖励和代价。",
                constraints="3000-4500 中文字符。",
                status="ready",
            )
        )
        session.flush()
        local_patch_version = ChapterVersion(
            chapter_id=local_patch_chapter.id,
            version_number=1,
            title="第3章",
            content="正文" * 1800,
            status="needs_revision",
            source="revision:ark_openai_compatible",
        )
        session.add(local_patch_version)
        session.flush()
        local_patch_quality = QualityReport(
            chapter_version_id=local_patch_version.id,
            score=75,
            passed=False,
            report=json.dumps(
                {
                    "status": "FAIL",
                    "score": 75,
                    "passed": False,
                    "hard_gate": {"status": "PASS"},
                    "dimensions": {
                        "author_intent": 65,
                        "brief_coverage": 47,
                        "reader_momentum": 66,
                        "scene_atmosphere": 41,
                        "dialogue_fullness": 50,
                        "chapter_unit_flow": 61,
                    },
                },
                ensure_ascii=False,
            ),
        )
        session.add(local_patch_quality)
        session.flush()
        plan_chapters(session, book_id=book.id, start=3, count=1, apply_state_repairs=True)
        local_patch_data = json.loads(local_patch_quality.report)
        local_patch_assessment = local_patch_data.get("reading_assessment") or {}
        if local_patch_assessment.get("revision_mode") != "local_patch":
            failures.append(f"score75_plateau_not_local_patch:{local_patch_assessment}")

    if failures:
        for failure in failures:
            print(failure)
        return 1
    print("reading-assessment-rebind-regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
