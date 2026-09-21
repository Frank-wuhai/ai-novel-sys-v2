"""系统体检看板 (health-report) 隔离 DB 回归 — 2026-09-21 人声攻坚元能力。

验证口径（对齐预登记验收标准，见 claude_handoff 报告第九节）：
1. 只读：build_health_report 运行前后全表行数不变；
2. 全量覆盖：章节/版本/质检报告计数与种子数据一致；
3. 三视图结构：版本轨迹按章分组、慢性低分维度命中已知种子（dialogue_fullness
   全期低分入榜）、近期塌陷视图识别「早期 65→近期 35」的回归型信号；
4. 失败分类：种子失败任务按 task_type:error_category 聚合；
5. 确定性：同库连跑两次渲染输出逐字节一致；零 LLM 调用（不依赖 provider，
   种子数据全部手工构造）。
"""

from __future__ import annotations

import json

from sqlalchemy import select

from app.db.session import session_scope
from app.models.entities import (
    Book,
    Chapter,
    ChapterVersion,
    GenerationTask,
    QualityReport,
)
from app.services.health_report import build_health_report, render_health_report_markdown
from regression_db import isolated_database

CONTENT = "沈渡在山镇上睁开眼。" * 200


def _seed(session) -> int:
    book = Book(title="Health Report Regression", genre="仙侠", target_platform="番茄")
    session.add(book)
    session.flush()
    for chapter_number in (1, 2):
        chapter = Chapter(book_id=book.id, chapter_number=chapter_number, title=f"第{chapter_number}章")
        session.add(chapter)
        session.flush()
        for version_number in (1, 2):
            version = ChapterVersion(
                chapter_id=chapter.id,
                version_number=version_number,
                title=f"第{chapter_number}章 v{version_number}",
                content=CONTENT,
                status="needs_revision",
                source="manual",
            )
            session.add(version)
            session.flush()
            # 每版本一份质检：dialogue_fullness 恒 35（慢性低分种子）；
            # memorable_dialogue 第1章 100 → 第2章 25（近期塌陷种子）。
            early = chapter_number == 1
            report = QualityReport(
                chapter_version_id=version.id,
                score=60,
                passed=False,
                report=json.dumps(
                    {
                        "verdict": "hard_fail",
                        "reading_assessment": {"level": "quality_gate_reopen_required"},
                        "dimensions": {
                            "dialogue_fullness": 35,
                            "memorable_dialogue": 100 if early else 25,
                            "scene_atmosphere": 70,
                        },
                    },
                    ensure_ascii=False,
                ),
            )
            session.add(report)
    session.add(
        GenerationTask(
            book_id=book.id,
            task_type="revise_chapter",
            status="failed",
            output_json=json.dumps({"error_category": "validation", "error_type": "StructuredOutputError"}),
        )
    )
    session.add(
        GenerationTask(book_id=book.id, task_type="revise_chapter", status="completed", output_json="{}")
    )
    session.flush()
    return book.id


def main() -> int:
    isolated_database("health-report-regression")
    failures: list[str] = []
    with session_scope() as session:
        book_id = _seed(session)

        tables_before = _table_counts(session)
        report = build_health_report(session, book_id=book_id)
        first = render_health_report_markdown(report)
        second = render_health_report_markdown(build_health_report(session, book_id=book_id))
        tables_after = _table_counts(session)

        # 1. 只读
        if tables_before != tables_after:
            failures.append("readonly_violated")

        # 2. 覆盖
        if report["chapter_count"] != 2 or report["version_count"] != 4:
            failures.append(f"coverage:{report['chapter_count']}/{report['version_count']}")
        if report["quality_report_count"] != 4 or report["quality_report_total"] != 4:
            failures.append(f"quality_coverage:{report['quality_report_count']}/{report['quality_report_total']}")

        # 3a. 轨迹结构
        if len(report["trajectories"]) != 2 or any(len(t.points) != 2 for t in report["trajectories"]):
            failures.append("trajectory_shape")
        # 3b. 慢性低分：dialogue_fullness 4/4 低分必入榜
        chronic = {c.name: c for c in report["chronic_dimensions"]}
        if "dialogue_fullness" not in chronic or chronic["dialogue_fullness"].low_hits != 4:
            failures.append("chronic_dialogue_fullness_missing")
        if "scene_atmosphere" in chronic:
            failures.append("chronic_false_positive:scene_atmosphere")
        # 3c. 近期塌陷：memorable_dialogue 早期 100 → 近期 25 必入榜
        regressions = {r.name: r for r in report["dimension_regressions"]}
        if "memorable_dialogue" not in regressions:
            failures.append("regression_memorable_dialogue_missing")
        elif regressions["memorable_dialogue"].delta >= 0:
            failures.append(f"regression_delta_sign:{regressions['memorable_dialogue'].delta}")
        if "dialogue_fullness" in regressions:
            failures.append("regression_false_positive:dialogue_fullness")

        # 4. 失败分类
        counts = report["failures"]["task_counts"].get("revise_chapter") or {}
        if counts.get("failed") != 1 or counts.get("completed") != 1:
            failures.append(f"task_counts:{counts}")
        if report["failures"]["failure_categories"].get("revise_chapter:validation") != 1:
            failures.append("failure_category_missing")

        # 5. 确定性
        if first != second:
            failures.append("nondeterministic_render")

    if failures:
        print("health_report_regression FAILED")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("health_report_regression OK")
    return 0


def _table_counts(session) -> dict[str, int]:
    counts: dict[str, int] = {}
    for model in (Book, Chapter, ChapterVersion, QualityReport, GenerationTask):
        counts[model.__tablename__] = len(list(session.scalars(select(model))))
    return counts


if __name__ == "__main__":
    raise SystemExit(main())
