from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Book, Chapter, ChapterBrief
from app.services.dashboard_production_actions import repair_chapter_brief
from app.services.llm_queue import build_generation_queue_health
from app.services.planning import AUTO_ACTIONS, plan_chapters, run_next_action
from app.services.production_control import build_production_control_report
from app.services.readiness import check_production_readiness
from app.services.story_alignment import build_story_alignment_audit


@dataclass(frozen=True)
class RouterStep:
    name: str
    status: str
    message: str
    object_id: int | None = None


@dataclass(frozen=True)
class ProductionRouterResult:
    status: str
    headline: str
    detail: str
    author_state: str
    recommended_action: str
    primary_label: str
    primary_intent: str
    target_chapter_number: int | None
    can_continue: bool
    auto_fixed: bool
    steps: list[RouterStep]
    blockers: list[str]
    warnings: list[str]
    checked: list[str]
    control: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "headline": self.headline,
            "detail": self.detail,
            "author_state": self.author_state,
            "recommended_action": self.recommended_action,
            "primary_label": self.primary_label,
            "primary_intent": self.primary_intent,
            "target_chapter_number": self.target_chapter_number,
            "can_continue": self.can_continue,
            "auto_fixed": self.auto_fixed,
            "steps": [asdict(item) for item in self.steps],
            "blockers": self.blockers,
            "warnings": self.warnings,
            "checked": self.checked,
            "control": self.control,
        }


def prepare_production(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    platform: str = "manual",
) -> ProductionRouterResult:
    book = session.get(Book, book_id)
    if not book:
        raise ValueError(f"book not found: {book_id}")

    steps: list[RouterStep] = []
    checked = ["作品准备", "作品设定", "章节说明", "后台队列", "生产下一步"]

    queue = build_generation_queue_health(session)
    if queue.running_count:
        return _result(
            status="running",
            headline="后台正在生成",
            detail="当前已有模型任务在运行，先等待完成，避免重复启动。",
            author_state="可以离开等待",
            primary_label="等待自动刷新",
            primary_intent="wait",
            checked=checked,
            control=_control(session, book_id=book_id, chapter_number=chapter_number),
        )
    if queue.counts.get("failed", 0):
        return _result(
            status="needs_attention",
            headline="需要先处理失败任务",
            detail="系统检测到失败生成任务，会先自动重试可恢复任务；不能恢复时再给出唯一下一步。",
            author_state="系统可先自动处理",
            primary_label="自动处理打断项",
            primary_intent="auto_resolve_blocker",
            blockers=["后台队列存在失败任务"],
            checked=checked,
            control=_control(session, book_id=book_id, chapter_number=chapter_number),
        )

    before_readiness = check_production_readiness(session, book_id=book_id, start=chapter_number, count=5, live_llm=False)
    before_alignment = build_story_alignment_audit(session, book_id=book_id, chapter_limit=max(5, chapter_number))
    _repair_brief_blockers(session, book_id=book_id, chapter_number=chapter_number, blockers=before_alignment.blockers, steps=steps)
    _ensure_current_brief(session, book_id=book_id, chapter_number=chapter_number, platform=platform, steps=steps)
    if steps:
        session.flush()

    readiness = check_production_readiness(session, book_id=book_id, start=chapter_number, count=5, live_llm=False)
    alignment = build_story_alignment_audit(session, book_id=book_id, chapter_limit=max(5, chapter_number))
    control = _control(session, book_id=book_id, chapter_number=chapter_number)
    plan_items = plan_chapters(session, book_id=book_id, start=chapter_number, count=1)
    current = plan_items[0] if plan_items else None

    hard_readiness = [f"{item.name}: {item.detail}" for item in readiness.blockers]
    hard_alignment = [item for item in alignment.blockers if not _is_repairable_brief_blocker(item)]
    blockers = _dedupe([*hard_readiness, *hard_alignment])
    warnings = _dedupe([*(item.action for item in readiness.warnings if item.action), *alignment.recommendations])
    auto_fixed = bool(steps)

    if blockers:
        state = _blocked_author_state(blockers)
        return _result(
            status="needs_confirmation",
            headline=state[0],
            detail=state[1],
            author_state="需要处理阻断项",
            primary_label=state[2],
            primary_intent=state[3],
            can_continue=False,
            auto_fixed=auto_fixed,
            steps=steps,
            blockers=blockers,
            warnings=warnings,
            checked=checked,
            control=control,
        )

    if queue.counts.get("pending", 0):
        return _result(
            status="ready",
            headline="可以生产",
            detail="队列里已有待启动任务，点击继续写作会启动后台生成。",
            author_state="可以生产",
            primary_label="继续写作",
            primary_intent="continue",
            can_continue=True,
            auto_fixed=auto_fixed,
            steps=steps,
            warnings=warnings,
            checked=checked,
            control=control,
        )

    if current and current.next_action in {"approve_chapter", "record_chapter_continuity"}:
        return _result(
            status="needs_author",
            headline="当前章可读，等待确认",
            detail="当前章已有可读稿，下一步是阅读后通过、局部改或整章重写。",
            author_state="等待作者确认当前章",
            primary_label="阅读当前章",
            primary_intent="approve",
            auto_fixed=auto_fixed,
            steps=steps,
            warnings=warnings,
            checked=checked,
            control=control,
        )

    if current and current.next_action in AUTO_ACTIONS:
        return _result(
            status="repaired_ready" if auto_fixed else "ready",
            headline="已自动整理，可以生产" if auto_fixed else "可以生产",
            detail="系统已确认当前设定、作品 DNA、章节说明和后台队列状态，可以进入正文生产。",
            author_state="可以生产",
            primary_label="继续写作",
            primary_intent="continue",
            can_continue=True,
            auto_fixed=auto_fixed,
            steps=steps,
            warnings=warnings,
            checked=checked,
            control=control,
        )

    return _result(
        status="idle",
        headline="当前没有可自动生产的动作",
        detail=current.reason if current else "当前章不在加载范围内。",
        author_state="无需处理",
        primary_label="刷新状态",
        primary_intent="refresh",
        auto_fixed=auto_fixed,
        steps=steps,
        warnings=warnings,
        checked=checked,
        control=control,
    )


def _repair_brief_blockers(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    blockers: list[str],
    steps: list[RouterStep],
) -> None:
    for number in _repairable_brief_chapters(blockers, fallback=chapter_number):
        brief = repair_chapter_brief(session, book_id=book_id, chapter_number=number)
        steps.append(RouterStep("repair_chapter_brief", "completed", f"已清理第 {number} 章生产说明", brief.id))


def _ensure_current_brief(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    platform: str,
    steps: list[RouterStep],
) -> None:
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    brief = (
        session.scalar(select(ChapterBrief).where(ChapterBrief.chapter_id == chapter.id).order_by(ChapterBrief.id.desc()))
        if chapter
        else None
    )
    if brief:
        return
    item = plan_chapters(session, book_id=book_id, start=chapter_number, count=1)[0]
    if item.next_action != "create_chapter_brief":
        return
    result = run_next_action(
        session,
        book_id=book_id,
        chapter_number=chapter_number,
        dry_run=False,
        queue_generation=False,
        platform=platform,
    )
    steps.append(RouterStep("create_chapter_brief", result.status, "已补齐当前章生产说明", result.object_id))


def _control(session: Session, *, book_id: int, chapter_number: int) -> dict[str, Any]:
    return build_production_control_report(session, book_id=book_id, start=chapter_number, count=8).to_dict()


def _result(
    *,
    status: str,
    headline: str,
    detail: str,
    author_state: str,
    primary_label: str,
    primary_intent: str,
    target_chapter_number: int | None = None,
    can_continue: bool = False,
    auto_fixed: bool = False,
    steps: list[RouterStep] | None = None,
    blockers: list[str] | None = None,
    warnings: list[str] | None = None,
    checked: list[str] | None = None,
    control: dict[str, Any] | None = None,
) -> ProductionRouterResult:
    return ProductionRouterResult(
        status=status,
        headline=headline,
        detail=detail,
        author_state=author_state,
        recommended_action=primary_label,
        primary_label=primary_label,
        primary_intent=primary_intent,
        target_chapter_number=target_chapter_number,
        can_continue=can_continue,
        auto_fixed=auto_fixed,
        steps=steps or [],
        blockers=blockers or [],
        warnings=warnings or [],
        checked=checked or [],
        control=control or {},
    )


def _blocked_author_state(blockers: list[str]) -> tuple[str, str, str, str]:
    merged = "\n".join(blockers)
    if any(marker in merged for marker in ("骨架", "StoryFoundation", "StoryBible", "核心设定源", "skeleton")):
        return ("作品设定需要修复", "当前设定里还有会影响后续正文方向的冲突或旧内容。系统会先生成修复草案。", "自动处理打断项", "auto_resolve_blocker")
    if "evidence" in merged or "market" in merged or "市场" in merged:
        return ("需要补齐生产准备", "当前作品缺少必要的市场/证据准备，系统会先自动补齐可处理项。", "自动处理打断项", "auto_resolve_blocker")
    return ("需要处理生产阻断", "系统发现继续生产会跑偏的阻断项，会先尝试自动处理。", "自动处理打断项", "auto_resolve_blocker")


def _repairable_brief_chapters(blockers: list[str], *, fallback: int) -> list[int]:
    numbers: list[int] = []
    for blocker in blockers:
        if not _is_repairable_brief_blocker(blocker):
            continue
        tail = blocker.split(":", 1)[-1] if ":" in blocker else ""
        for part in tail.replace("，", ",").split(","):
            value = part.strip()
            if value.isdigit():
                numbers.append(int(value))
    if not numbers and any(_is_repairable_brief_blocker(item) for item in blockers):
        numbers.append(fallback)
    return sorted(set(numbers))


def _is_repairable_brief_blocker(value: str) -> bool:
    return (
        "最新章节 brief 仍含旧质检/旧修订合同残留" in value
        or "章节 brief 未显式承接核心作者意图" in value
    )


def _dedupe(items) -> list[str]:
    seen = set()
    result: list[str] = []
    for item in items:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
