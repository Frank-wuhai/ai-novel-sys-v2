from __future__ import annotations

import json
from types import SimpleNamespace

from app.db.session import session_scope
from app.models.entities import Book, Chapter, ChapterBrief, ChapterVersion, QualityReport
from app.services.production_reviewing import _apply_editorial_gate
from app.services.planning import plan_chapters
from app.services.quality import _coverage_score, split_points
from regression_db import isolated_database


def main() -> int:
    isolated_database("editorial-gate-budget-regression")
    failures: list[str] = []

    noisy_brief = "\n".join(
        [
            "system_revision_trend_recovery: detected",
            "恢复底稿：v76 score=75；废弃劣化稿：v80。",
            "当前主角锚点:林北",
            "当前世界/作品锚点:《万象江湖》",
            "当前能力/卖点锚点:桥段复刻、神经肌肉记忆、高神经敏感用户",
            "必须遵守最新作品DNA：【作品DNA】 - 题材主味: 玄幻脑洞 【作品DNA结束】",
            "禁区：不要写成系统文或面板数据流。",
            "主角在《万象江湖》中用桥段复刻解围，并让神经肌肉记忆同步到现实。",
        ]
    )
    text = "林北进入《万象江湖》，在青州镖局外临场复刻旧侠义桥段解围。下线后，他发现神经肌肉记忆改变了现实步态。"
    coverage = _coverage_score(text, split_points(noisy_brief))
    if coverage < 60:
        failures.append(f"diagnostic_brief_points_still_depress_coverage:{coverage}")

    report_data = {
        "passed": False,
        "score": 76,
        "issues": ["naming_governance_risk: 52"],
        "hard_gate": {
            "passed": True,
            "issues": [],
        },
        "dimensions": {
            "brief_coverage": 52,
            "observation_logic": 64,
            "visual_staging": 64,
            "scene_expansion": 58,
        },
        "llm_review": {
            "status": "completed",
            "verdict": "pass",
            "score": 88,
            "issues": ["可读稿，只需轻微增强现实同步代价。"],
        },
    }
    _apply_editorial_gate(SimpleNamespace(passed=False, score=76), report_data)
    if not report_data.get("passed"):
        failures.append(f"editorial_pass_blocked_by_soft_brief_coverage:{report_data.get('editorial_gate')}")

    with session_scope() as session:
        book = Book(title="editorial-gate-budget-regression-book", genre="test", target_platform="test")
        session.add(book)
        session.flush()
        chapter = Chapter(book_id=book.id, chapter_number=1, title="第1章", status="draft")
        session.add(chapter)
        session.flush()
        session.add(ChapterBrief(chapter_id=chapter.id, goal="主角推进当前章", required_beats="完成桥段复刻", constraints="", status="ready"))
        version = ChapterVersion(
            chapter_id=chapter.id,
            version_number=1,
            title="v1",
            content=text,
            status="needs_revision",
            source="revision:regression",
        )
        session.add(version)
        session.flush()
        quality = QualityReport(
            chapter_version_id=version.id,
            score=75,
            passed=False,
            report=json.dumps(
                {
                    "passed": False,
                    "score": 75,
                    "status": "FAIL",
                    "issues": ["naming_governance_risk: 52"],
                    "hard_gate": {"passed": True, "issues": []},
                    "dimensions": {"brief_coverage": 50, "observation_logic": 64},
                    "llm_review": {"status": "completed", "verdict": "pass", "score": 80, "issues": []},
                    "editorial_gate": {
                        "status": "completed",
                        "passed": True,
                        "threshold": 75,
                        "score": 80,
                        "verdict": "pass",
                        "soft_rule_override": False,
                        "soft_override_blockers": ["brief_coverage=50<60"],
                    },
                },
                ensure_ascii=False,
            ),
        )
        session.add(quality)
        session.flush()
        item = plan_chapters(session, book_id=book.id, start=1, count=1)[0]
        session.refresh(version)
        session.refresh(quality)
        if version.status != "reviewed_pass" or not quality.passed:
            failures.append(f"old_quality_report_not_reconciled:{version.status}:{quality.passed}:{item.next_action}")
        if item.next_action != "record_chapter_continuity":
            failures.append(f"plan_did_not_leave_revision_loop:{item.next_action}")

        blocked = ChapterVersion(
            chapter_id=chapter.id,
            version_number=2,
            title="bad-followup",
            content="后续坏稿",
            status="needs_revision",
            source="revision_recovery:v1",
        )
        session.add(blocked)
        session.flush()
        item = plan_chapters(session, book_id=book.id, start=1, count=1)[0]
        restored = session.get(ChapterVersion, item.latest_version_id)
        if not restored or restored.status != "reviewed_pass" or not str(restored.source or "").startswith("quality_reconcile:"):
            failures.append(f"historical_readable_version_not_restored:{item.next_action}:{item.latest_version_status}")
        active_revision_briefs = list(
            session.query(ChapterBrief).filter(ChapterBrief.chapter_id == chapter.id, ChapterBrief.status == "revision_ready")
        )
        if active_revision_briefs:
            failures.append(f"revision_brief_left_active_after_readable:{[brief.id for brief in active_revision_briefs]}")
        archived_versions = [
            version
            for version in session.query(ChapterVersion).filter(ChapterVersion.chapter_id == chapter.id, ChapterVersion.status == "needs_revision")
            if str(version.source or "").startswith("archived:")
        ]
        if not archived_versions:
            failures.append("old_failed_versions_not_archived_after_readable")

    if failures:
        for failure in failures:
            print(failure)
        return 1
    print("editorial-gate-budget-regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
