from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.llm.providers import ArkOpenAIProvider
from app.models.entities import Book, Character, EvidenceSource, MarketSignal, PlatformFeedback, PowerSystem, StoryArc, StoryBible, StoryFoundation, Volume, WorldRule
from app.services.agent_plan_intelligence import summarize_semantic_memory
from app.services.evidence import list_market_signals
from app.services.planning import build_human_decision_package, plan_chapters
from app.services.skeleton_governance import audit_story_skeleton_with_agent_evidence


@dataclass(frozen=True)
class ReadinessCheck:
    name: str
    passed: bool
    detail: str
    severity: str = "blocker"
    action: str = ""


@dataclass(frozen=True)
class ProductionReadinessReport:
    passed: bool
    checks: list[ReadinessCheck]

    @property
    def blockers(self) -> list[ReadinessCheck]:
        return [check for check in self.checks if not check.passed and check.severity == "blocker"]

    @property
    def warnings(self) -> list[ReadinessCheck]:
        return [check for check in self.checks if check.severity == "warning"]


def check_production_readiness(
    session: Session,
    *,
    book_id: int,
    start: int = 1,
    count: int = 10,
    live_llm: bool = False,
) -> ProductionReadinessReport:
    checks = [
        _foundation_check(session, book_id),
        _story_bible_check(session, book_id, start, count),
        _skeleton_approval_check(session, book_id),
        _skeleton_governance_check(session, book_id),
        _evidence_check(session, book_id),
        _canon_check(session, book_id),
        _semantic_memory_check(session, book_id),
        _chapter_queue_check(session, book_id, start, count),
        _human_decision_check(session, book_id, start, count),
        _llm_config_check(live_llm=live_llm),
    ]
    return ProductionReadinessReport(passed=not any(check.severity == "blocker" and not check.passed for check in checks), checks=checks)


def _foundation_check(session: Session, book_id: int) -> ReadinessCheck:
    foundation = session.scalar(select(StoryFoundation).where(StoryFoundation.book_id == book_id).order_by(StoryFoundation.id.desc()))
    if not foundation:
        return ReadinessCheck("foundation", False, "missing story foundation", action="先创建或补全作品基础设定。")
    missing = []
    if not foundation.premise:
        missing.append("premise")
    if not foundation.reader_promise:
        missing.append("reader_promise")
    if missing:
        return ReadinessCheck("foundation", False, "missing fields: " + ",".join(missing), action="补齐一句话核心设定和读者承诺。")
    return ReadinessCheck("foundation", True, f"foundation_id={foundation.id}", severity="info")


def _story_bible_check(session: Session, book_id: int, start: int, count: int) -> ReadinessCheck:
    bible = session.scalar(select(StoryBible).where(StoryBible.book_id == book_id))
    if not bible:
        return ReadinessCheck("story_bible", False, "missing story bible", action="补全 Story Bible。")
    missing = []
    if not bible.positioning:
        missing.append("positioning")
    if not bible.reader_promise:
        missing.append("reader_promise")
    if not bible.main_plot:
        missing.append("main_plot")
    if not bible.forbidden_rules:
        missing.append("forbidden_rules")
    if missing:
        return ReadinessCheck("story_bible", False, "missing fields: " + ",".join(missing), action="补齐主线、读者承诺、禁忌规则等核心字段。")
    end = start + count - 1
    arc_count = session.scalar(
        select(func.count(StoryArc.id)).where(
            StoryArc.book_id == book_id,
            StoryArc.start_chapter <= end,
            StoryArc.end_chapter >= start,
        )
    )
    if not arc_count:
        return ReadinessCheck("story_bible", False, f"no story arcs covering chapters {start}-{end}", action="补齐覆盖当前章节范围的剧情段。")
    return ReadinessCheck("story_bible", True, f"story_bible_id={bible.id} covering_arcs={arc_count}", severity="info")


def _skeleton_approval_check(session: Session, book_id: int) -> ReadinessCheck:
    values = _skeleton_values(session, book_id)
    required = {
        "premise": "一句话核心设定",
        "reader_promise": "读者承诺",
        "world_engine": "世界规则/能力曲线",
        "protagonist_engine": "主角动力/成长弧",
        "conflict_engine": "长期冲突/主线",
        "arc_goal": "剧情段目标",
        "arc_climax": "剧情段高潮",
        "arc_turn": "剧情段转折",
    }
    latest = {}
    rows = session.scalars(
        select(PlatformFeedback)
        .where(PlatformFeedback.book_id == book_id, PlatformFeedback.metric_name == "skeleton_approval")
        .order_by(PlatformFeedback.id.desc())
    )
    for item in rows:
        latest.setdefault(item.metric_value, item.raw_text)
    missing = [label for key, label in required.items() if values.get(key) and latest.get(key) != values.get(key)]
    empty = [label for key, label in required.items() if not values.get(key)]
    if empty:
        return ReadinessCheck("skeleton_approval", False, "missing skeleton fields: " + ",".join(empty), action="先补齐生产骨架字段。")
    if missing:
        return ReadinessCheck("skeleton_approval", False, "pending approval: " + ",".join(missing), action="在作品设定页确认新版骨架。")
    return ReadinessCheck("skeleton_approval", True, f"approved_items={len(required)}", severity="info")


def _skeleton_governance_check(session: Session, book_id: int) -> ReadinessCheck:
    report = audit_story_skeleton_with_agent_evidence(session, book_id=book_id)
    if report.passed:
        detail = f"score={report.score}"
        if report.evidence_summary:
            detail += " " + "；".join(report.evidence_summary[:2])
        return ReadinessCheck("skeleton_governance", True, detail, severity="info")
    issue_text = ",".join(issue.code for issue in report.issues[:4])
    action = "先生成修复草案并确认骨架。"
    if report.human_decisions:
        action = report.human_decisions[0]
    has_blocker = any(issue.severity == "blocker" for issue in report.issues)
    return ReadinessCheck(
        "skeleton_governance",
        not has_blocker,
        f"score={report.score} issues={issue_text}",
        severity="blocker" if has_blocker else "warning",
        action=action,
    )


def _skeleton_values(session: Session, book_id: int) -> dict[str, str]:
    foundation = session.scalar(select(StoryFoundation).where(StoryFoundation.book_id == book_id).order_by(StoryFoundation.id.desc()))
    bible = session.scalar(select(StoryBible).where(StoryBible.book_id == book_id))
    volume = session.scalar(select(Volume).where(Volume.book_id == book_id, Volume.volume_number == 1))
    arc = session.scalar(select(StoryArc).where(StoryArc.book_id == book_id, StoryArc.arc_number == 1))
    return {
        "premise": (foundation.premise if foundation else (bible.positioning if bible else "")).strip(),
        "reader_promise": (foundation.reader_promise if foundation else (bible.reader_promise if bible else "")).strip(),
        "world_engine": (foundation.world_engine if foundation else (bible.power_curve if bible else "")).strip(),
        "protagonist_engine": (foundation.protagonist_engine if foundation else (bible.protagonist_arc if bible else "")).strip(),
        "conflict_engine": (foundation.conflict_engine if foundation else (bible.main_plot if bible else "")).strip(),
        "arc_goal": (arc.goal if arc else "").strip(),
        "arc_climax": (arc.climax if arc else "").strip(),
        "arc_turn": (arc.turn if arc else "").strip(),
    }


def _evidence_check(session: Session, book_id: int) -> ReadinessCheck:
    book = session.get(Book, book_id)
    if not book:
        return ReadinessCheck("evidence", False, f"book not found: {book_id}", action="先选择有效作品。")
    if not book.genre:
        return ReadinessCheck("evidence", False, "book genre is required for market evidence matching", action="先设置作品题材。")
    signals = list_market_signals(session, genre=book.genre, usable_only=True, min_confidence=60)
    recent_count = session.scalar(
        select(func.count(MarketSignal.id)).where(
            MarketSignal.genre == book.genre,
            MarketSignal.confidence >= 60,
            MarketSignal.created_at >= datetime.now() - timedelta(days=14),
        )
    ) or 0
    verified_sources = session.scalar(
        select(func.count(EvidenceSource.id)).where(EvidenceSource.status == "verified", EvidenceSource.reliability >= 3)
    )
    if not signals:
        return ReadinessCheck(
            "evidence",
            False,
            f"no usable market signals for genre={book.genre}",
            action="先执行 Agent Plan 增强一轮，让后台搜索并导入市场证据。",
        )
    if recent_count < 3:
        return ReadinessCheck(
            "evidence",
            True,
            f"genre={book.genre} usable_market_signals={len(signals)} recent14d={recent_count} status=stale_or_thin",
            severity="warning",
            action="建议执行 Agent Plan 增强一轮刷新市场证据；测试或低风险推进可继续。",
        )
    return ReadinessCheck(
        "evidence",
        True,
        f"genre={book.genre} usable_market_signals={len(signals)} recent14d={recent_count} verified_sources={verified_sources}",
        severity="info",
    )


def _canon_check(session: Session, book_id: int) -> ReadinessCheck:
    character_count = session.scalar(select(func.count(Character.id)).where(Character.book_id == book_id)) or 0
    world_rule_count = session.scalar(select(func.count(WorldRule.id)).where(WorldRule.book_id == book_id, WorldRule.status == "active")) or 0
    power_count = session.scalar(
        select(func.count(PowerSystem.id)).where(PowerSystem.book_id == book_id, PowerSystem.status.in_(["active", "locked"]))
    ) or 0
    missing = []
    if character_count < 1:
        missing.append("character")
    if world_rule_count < 1:
        missing.append("world_rule")
    if power_count < 1:
        missing.append("power_system")
    if missing:
        return ReadinessCheck("canon", False, "missing canon: " + ",".join(missing), action="补齐人物、世界规则和力量体系。")
    return ReadinessCheck("canon", True, f"characters={character_count} world_rules={world_rule_count} power_systems={power_count}", severity="info")


def _semantic_memory_check(session: Session, book_id: int) -> ReadinessCheck:
    try:
        summary = summarize_semantic_memory(session, book_id=book_id)
    except OperationalError:
        session.rollback()
        return ReadinessCheck("semantic_memory", True, "migration missing; run alembic upgrade head", severity="warning", action="运行迁移后再重建语义记忆。")
    if not summary["indexed_count"]:
        return ReadinessCheck("semantic_memory", True, "indexed_count=0", severity="warning", action="执行 Agent Plan 增强一轮或重建语义记忆。")
    if summary["stale"]:
        missing = ",".join(summary.get("missing_sources") or [])
        stale_detail = f"missing_sources={missing}" if missing else (
            f"latest_embedding_at={summary['latest_embedding_at']} latest_chapter_version_at={summary['latest_chapter_version_at']}"
        )
        return ReadinessCheck(
            "semantic_memory",
            True,
            f"stale index; indexed_count={summary['indexed_count']} expected_count={summary.get('expected_count', '')} {stale_detail}",
            severity="warning",
            action="重建语义记忆，避免生产时遗漏新设定。",
        )
    return ReadinessCheck(
        "semantic_memory",
        True,
        f"indexed_count={summary['indexed_count']} models={','.join(summary['models'])} "
        f"source_types={','.join(summary['source_types'])}",
        severity="info",
    )


def _chapter_queue_check(session: Session, book_id: int, start: int, count: int) -> ReadinessCheck:
    from app.services.production_decision import decide_chapter_production

    items = plan_chapters(session, book_id=book_id, start=start, count=count)
    decisions = [decide_chapter_production(item) for item in items]
    runnable = [decision for decision in decisions if decision.can_continue]
    waiting = [decision for decision in decisions if decision.needs_author]
    done = [item for item in items if item.next_action == "done"]
    if not runnable and not waiting:
        return ReadinessCheck("chapter_queue", False, "no runnable or human-waiting chapters in range", action="扩大章节范围或检查章节状态。")
    return ReadinessCheck("chapter_queue", True, f"auto_ready={len(runnable)} human_waiting={len(waiting)} done={len(done)}", severity="info")


def _human_decision_check(session: Session, book_id: int, start: int, count: int) -> ReadinessCheck:
    package = build_human_decision_package(session, book_id=book_id, start=start, count=count)
    if package.inspect_count:
        return ReadinessCheck("human_decisions", False, f"manual inspection required={package.inspect_count}", action="先处理需要人工检查的章节或发布项。")
    return ReadinessCheck(
        "human_decisions",
        True,
        f"continuity={package.continuity_count} approval={package.approval_count} publish={package.publish_count}",
        severity="info",
    )


def _llm_config_check(*, live_llm: bool) -> ReadinessCheck:
    missing = []
    if settings.llm_plan == "agent_plan":
        if not settings.ark_agent_plan_api_key:
            missing.append("ARK_AGENT_PLAN_API_KEY")
        if settings.ark_base_url.rstrip("/") != "https://ark.cn-beijing.volces.com/api/plan/v3":
            missing.append("ARK_BASE_URL(api/plan/v3)")
    elif not settings.ark_api_key:
        missing.append("ARK_API_KEY")
    if not settings.ark_base_url:
        missing.append("ARK_BASE_URL")
    if not settings.model_name:
        missing.append("MODEL_NAME")
    if missing:
        return ReadinessCheck("llm", False, "missing config: " + ",".join(missing), action="检查 .env 中的模型配置。")
    if not live_llm:
        return ReadinessCheck("llm", True, f"plan={settings.llm_plan} configured model={settings.model_name} live_check=skipped", severity="info")
    try:
        response = ArkOpenAIProvider().generate(
            '只回复 JSON: {"ok": true}',
            max_tokens=settings.llm_smoke_max_tokens,
            temperature=0,
        )
    except Exception as exc:
        return ReadinessCheck("llm", False, f"live_check_failed={type(exc).__name__}: {exc}", action="先检测模型连接。")
    ok = '"ok"' in response.text or "ok" in response.text.lower()
    return ReadinessCheck("llm", ok, f"live_check_model={response.model} text={response.text[:80]}", severity="info" if ok else "blocker")
