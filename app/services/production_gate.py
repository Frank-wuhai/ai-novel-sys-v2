from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import PlatformFeedback, StoryArc, StoryBible, StoryFoundation


WRITE_ACTIONS = {
    "draft_chapter",
    "enqueue_draft_chapter",
    "review_chapter",
    "revise_chapter",
    "enqueue_revise_chapter",
    "create_publish_job",
    "publish_job_dry_run",
    "queue_publish_job",
    "retry_publish_job",
}


@dataclass(frozen=True)
class ProductionGateResult:
    passed: bool
    blockers: list[str]

    @property
    def message(self) -> str:
        return "；".join(self.blockers)


def check_production_gate(session: Session, *, book_id: int, action: str) -> ProductionGateResult:
    if action not in WRITE_ACTIONS:
        return ProductionGateResult(True, [])
    pending = pending_skeleton_approval_labels(session, book_id=book_id)
    if pending:
        return ProductionGateResult(
            False,
            ["生产门禁未通过：作品设定尚未确认（" + "、".join(pending[:4]) + "）。请先保存并启用当前作品设定。"],
        )
    return ProductionGateResult(True, [])


def assert_production_gate(session: Session, *, book_id: int, action: str) -> None:
    result = check_production_gate(session, book_id=book_id, action=action)
    if not result.passed:
        raise ValueError(result.message)


def pending_skeleton_approval_labels(session: Session, *, book_id: int) -> list[str]:
    foundation = session.scalar(select(StoryFoundation).where(StoryFoundation.book_id == book_id).order_by(StoryFoundation.id.desc()))
    bible = session.scalar(select(StoryBible).where(StoryBible.book_id == book_id))
    arc = session.scalar(select(StoryArc).where(StoryArc.book_id == book_id, StoryArc.arc_number == 1))
    values = {
        "premise": (foundation.premise if foundation else (bible.positioning if bible else "")).strip(),
        "reader_promise": (foundation.reader_promise if foundation else (bible.reader_promise if bible else "")).strip(),
        "world_engine": (foundation.world_engine if foundation else (bible.power_curve if bible else "")).strip(),
        "protagonist_engine": (foundation.protagonist_engine if foundation else (bible.protagonist_arc if bible else "")).strip(),
        "conflict_engine": (foundation.conflict_engine if foundation else (bible.main_plot if bible else "")).strip(),
        "arc_goal": (arc.goal if arc else "").strip(),
        "arc_climax": (arc.climax if arc else "").strip(),
        "arc_turn": (arc.turn if arc else "").strip(),
    }
    labels = {
        "premise": "一句话核心设定",
        "reader_promise": "读者承诺",
        "world_engine": "世界规则/能力曲线",
        "protagonist_engine": "主角动力/成长弧",
        "conflict_engine": "长期冲突/主线",
        "arc_goal": "剧情段目标",
        "arc_climax": "剧情段高潮",
        "arc_turn": "剧情段转折",
    }
    rows = session.scalars(
        select(PlatformFeedback)
        .where(PlatformFeedback.book_id == book_id, PlatformFeedback.metric_name == "skeleton_approval")
        .order_by(PlatformFeedback.id.desc())
    )
    latest: dict[str, str] = {}
    for item in rows:
        latest.setdefault(item.metric_value, item.raw_text)
    return [label for key, label in labels.items() if not values.get(key, "") or latest.get(key) != values.get(key, "")]
