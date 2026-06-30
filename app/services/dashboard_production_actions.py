from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import (
    Book,
    Chapter,
    ChapterBrief,
    ChapterReview,
    ChapterUnitPlan,
    ChapterVersion,
    GenerationTask,
    ProductionRunReview,
    PublishExecution,
    PublishJob,
    QualityReport,
    StoryBible,
    StoryFoundation,
)
from app.services.chapter_standards import ensure_chapter_production_standard
from app.services.context_contamination import context_anchor_lines
from app.services.db_ops import create_database_backup
from app.services.story import get_story_bible
from app.services.story_dna import chapter_engine_for_number, story_dna_display_fields, story_dna_for_book


def repair_chapter_brief(session: Session, *, book_id: int, chapter_number: int) -> ChapterBrief:
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if not chapter:
        raise ValueError("chapter not found")
    context = _current_book_brief_context(session, book_id=book_id, chapter_number=chapter_number)
    latest = session.scalar(select(ChapterBrief).where(ChapterBrief.chapter_id == chapter.id).order_by(ChapterBrief.id.desc()))
    version = session.scalar(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter.id).order_by(ChapterVersion.id.desc()))
    for old_brief in session.scalars(
        select(ChapterBrief).where(
            ChapterBrief.chapter_id == chapter.id,
            ChapterBrief.status.in_(["ready", "revision_ready"]),
        )
    ):
        old_brief.status = "superseded"
    status = "revision_ready" if version and version.status == "needs_revision" else "ready"
    goal = _clean_brief_goal(chapter_number, latest.goal if latest else "", context=context)
    required = _clean_brief_required_beats(chapter_number, latest.required_beats if latest else "", context=context)
    constraints = ensure_chapter_production_standard(
        _clean_brief_constraints(latest.constraints if latest else "", context=context),
        chapter_number=chapter_number,
    )
    brief = ChapterBrief(chapter_id=chapter.id, goal=goal, required_beats=required, constraints=constraints, status=status)
    session.add(brief)
    session.flush()
    return brief


def restart_production_from_chapter(session: Session, *, book_id: int, start_chapter: int) -> dict:
    start_chapter = max(1, int(start_chapter or 1))
    book = session.get(Book, book_id)
    if not book:
        raise ValueError(f"book not found: {book_id}")
    backup = create_database_backup(session, label=f"restart-book-{book_id}-from-ch{start_chapter}")
    chapters = list(
        session.scalars(
            select(Chapter)
            .where(Chapter.book_id == book_id, Chapter.chapter_number >= start_chapter)
            .order_by(Chapter.chapter_number)
        )
    )
    chapter_ids = [chapter.id for chapter in chapters]
    version_ids = [
        version.id
        for version in session.scalars(select(ChapterVersion).where(ChapterVersion.chapter_id.in_(chapter_ids)))
    ] if chapter_ids else []
    publish_jobs = list(session.scalars(select(PublishJob).where(PublishJob.chapter_version_id.in_(version_ids)))) if version_ids else []
    publish_job_ids = [job.id for job in publish_jobs]
    deleted = {
        "chapters": len(chapters),
        "versions": len(version_ids),
        "publish_jobs": len(publish_jobs),
        "quality_reports": 0,
        "chapter_reviews": 0,
        "chapter_briefs": 0,
        "unit_plans": 0,
        "production_reviews": 0,
        "publish_executions": 0,
        "tasks_canceled": 0,
    }
    if publish_job_ids:
        executions = list(session.scalars(select(PublishExecution).where(PublishExecution.publish_job_id.in_(publish_job_ids))))
        deleted["publish_executions"] = len(executions)
        for item in executions:
            session.delete(item)
    if version_ids:
        for model, key in [(QualityReport, QualityReport.chapter_version_id), (ChapterReview, ChapterReview.chapter_version_id)]:
            rows = list(session.scalars(select(model).where(key.in_(version_ids))))
            deleted["quality_reports" if model is QualityReport else "chapter_reviews"] = len(rows)
            for item in rows:
                session.delete(item)
    if chapter_ids:
        cleanup_specs = [
            (ProductionRunReview, ProductionRunReview.chapter_id, "production_reviews"),
            (ChapterUnitPlan, ChapterUnitPlan.chapter_id, "unit_plans"),
            (ChapterBrief, ChapterBrief.chapter_id, "chapter_briefs"),
        ]
        for model, key, label in cleanup_specs:
            rows = list(session.scalars(select(model).where(key.in_(chapter_ids))))
            deleted[label] = len(rows)
            for item in rows:
                session.delete(item)
    for job in publish_jobs:
        session.delete(job)
    versions = list(session.scalars(select(ChapterVersion).where(ChapterVersion.id.in_(version_ids)))) if version_ids else []
    for version in versions:
        session.delete(version)
    for chapter in chapters:
        session.delete(chapter)
    tasks = list(
        session.scalars(
            select(GenerationTask).where(
                GenerationTask.book_id == book_id,
                GenerationTask.status.in_(["pending", "running", "paused", "failed"]),
            )
        )
    )
    for task in tasks:
        data = _loads_json(task.input_json)
        if int(data.get("chapter_number") or 0) >= start_chapter:
            task.status = "canceled"
            deleted["tasks_canceled"] += 1
    session.flush()
    return {
        "status": "restarted",
        "message": f"已从第 {start_chapter} 章重启生产；旧章节产物已清理，骨架和作品 DNA 保留。",
        "book_id": book_id,
        "start_chapter": start_chapter,
        "backup_path": backup.backup_path,
        "deleted": deleted,
    }


def _current_book_brief_context(session: Session, *, book_id: int, chapter_number: int) -> dict[str, str]:
    book = session.get(Book, book_id)
    foundation = session.scalar(select(StoryFoundation).where(StoryFoundation.book_id == book_id).order_by(StoryFoundation.id.desc()))
    bible = get_story_bible(session, book_id=book_id)
    dna_display = story_dna_display_fields(
        style_guide=bible.style_guide if bible else "",
        forbidden_rules=bible.forbidden_rules if bible else "",
    )
    story_dna = dna_display["story_dna"] or story_dna_for_book(session, book_id=book_id)
    return {
        "title": book.title if book else "",
        "genre": book.genre if book else "",
        "premise": foundation.premise if foundation else (bible.positioning if bible else ""),
        "reader_promise": foundation.reader_promise if foundation else (bible.reader_promise if bible else ""),
        "world_engine": foundation.world_engine if foundation else (bible.power_curve if bible else ""),
        "protagonist_engine": foundation.protagonist_engine if foundation else (bible.protagonist_arc if bible else ""),
        "conflict_engine": foundation.conflict_engine if foundation else (bible.main_plot if bible else ""),
        "forbidden_rules": dna_display["forbidden_rules"] or (bible.forbidden_rules if bible else ""),
        "story_dna": story_dna,
        "chapter_engine": chapter_engine_for_number(story_dna, chapter_number) if story_dna else "",
        "context_anchors": "\n".join(context_anchor_lines(session, book_id=book_id)),
    }


def _brief_context_line(context: dict[str, str], key: str, fallback: str, *, limit: int = 220) -> str:
    value = str(context.get(key) or "").strip()
    if not value:
        value = fallback
    value = " ".join(value.split())
    return value[:limit]


def _clean_brief_goal(chapter_number: int, previous: str, *, context: dict[str, str] | None = None) -> str:
    context = context or {}
    base = _strip_stale_brief_text(previous).strip()
    first_line = base.splitlines()[0] if base else ""
    if not base or len(base) < 20 or _line_has_wrong_book_marker(first_line, context):
        title = _brief_context_line(context, "title", "本书", limit=40)
        premise = _brief_context_line(context, "premise", "围绕当前作品核心设定推进剧情。")
        base = f"第{chapter_number}章：承接《{title}》的当前主线，{premise}"
    return "\n".join(
        [
            base.splitlines()[0],
            "核心作者意图：" + _brief_context_line(context, "reader_promise", _brief_context_line(context, "premise", "兑现当前作品的读者承诺。")),
            "主角方向：" + _brief_context_line(context, "protagonist_engine", "主角必须通过具体行动、判断、选择和代价推动局面，不能依赖万能捷径。"),
        ]
    )


def _clean_brief_required_beats(chapter_number: int, previous: str, *, context: dict[str, str] | None = None) -> str:
    context = context or {}
    cleaned = _strip_stale_brief_text(previous)
    anchors = [
        line
        for line in str(context.get("context_anchors") or "").splitlines()
        if line.strip()
    ]
    lines = [
        f"第{chapter_number}章必须以具体场景、人物行动和外部压力推进，不写后台说明。",
        "本章章节发动机:" + _brief_context_line(context, "chapter_engine", "具体处境破局", limit=80),
        "核心设定必须落在场景里：" + _brief_context_line(context, "premise", "承接本书最新骨架设定。"),
        "世界规则必须可被人物感知：" + _brief_context_line(context, "world_engine", "遵守当前 Story Bible 的世界规则、能力边界和代价。"),
        "主角行动必须体现本书独有能力与限制：" + _brief_context_line(context, "protagonist_engine", "通过判断、选择、试错和代价推进，不靠万能系统解题。"),
        "冲突升级必须来自当前主线：" + _brief_context_line(context, "conflict_engine", "用人物关系、规则压力、资源选择和后果递进制造冲突。"),
        *anchors,
        "章末留下由本章行动自然引出的新危险、新关系或新机会。",
    ]
    for line in cleaned.splitlines():
        line = line.strip(" -\t")
        if line and not _line_has_stale_marker(line) and line not in lines and len(line) <= 80:
            lines.append(line)
        if len(lines) >= 8:
            break
    return "\n".join(f"- {line}" for line in lines)


def _clean_brief_constraints(previous: str, *, context: dict[str, str] | None = None) -> str:
    context = context or {}
    cleaned = _strip_stale_brief_text(previous)
    lines = [
        "3000-4500 中文字符，正文优先，不用自检内容凑字数。",
        "不要输出导演单、质检报告、修订合同、验收清单或系统说明。",
        "少量界面/提示只能作为人物感知层点到为止，不能替代真实人物行动、因果和代价。",
        "对白和动作必须承接上一段后果，不能另起炉灶。",
        "执行作品DNA章节发动机:" + _brief_context_line(context, "chapter_engine", "具体处境破局", limit=80),
        "必须遵守最新作品DNA：" + _brief_context_line(context, "story_dna", "承接最新 Story Bible 和作品审美方向。", limit=260),
        "禁区：" + _brief_context_line(context, "forbidden_rules", "不得出现打怪升级、刷经验、刷副本、任务大厅、机械 NPC、系统任务等反方向表达。", limit=220),
    ]
    for line in cleaned.splitlines():
        line = line.strip(" -\t")
        if line and not _line_has_stale_marker(line) and line not in lines and len(line) <= 90:
            lines.append(line)
        if len(lines) >= 9:
            break
    return "\n".join(f"- {line}" for line in lines)


def _strip_stale_brief_text(text: str) -> str:
    markers = (
        "依据质检报告",
        "上次质检分数",
        "采纳二审建议",
        "修复质检问题",
        "执行修订合同",
        "修订合同:",
        "原始机器修订建议",
        "验收清单",
        "已废弃",
        "旧主角名",
        "旧桥段",
        "旧世界名",
        "江湖志",
    )
    keep = []
    skipping_contract = False
    for raw in (text or "").splitlines():
        line = raw.strip()
        if any(marker in line for marker in markers):
            skipping_contract = True
            continue
        if skipping_contract and (line.startswith("- ") or line.startswith("•") or "：" in line or ":" in line):
            continue
        skipping_contract = False
        if line:
            keep.append(line)
    return "\n".join(keep)


def _line_has_stale_marker(line: str) -> bool:
    return any(
        marker in line
        for marker in (
            "质检",
            "修订合同",
            "原始机器修订建议",
            "验收清单",
            "二审建议",
            "当前设定锚点",
            "旧设定残留",
            "林默",
            "江湖志",
            "已废弃",
            "旧主角名",
            "旧桥段",
            "旧世界名",
        )
    )


def _line_has_wrong_book_marker(line: str, context: dict[str, str]) -> bool:
    current_context = "\n".join(str(value or "") for value in context.values())
    legacy_markers = ("陈默", "林默", "大江湖", "江湖志", "已废弃", "旧主角名", "旧桥段", "旧世界名")
    return any(marker in line and marker not in current_context for marker in legacy_markers)


def _loads_json(value: str) -> dict:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
