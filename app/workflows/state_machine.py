from __future__ import annotations

from dataclasses import dataclass


class WorkflowError(ValueError):
    pass


@dataclass(frozen=True)
class Transition:
    entity: str
    from_status: str
    to_status: str
    action: str


CHAPTER_VERSION_TRANSITIONS = {
    ("draft", "reviewed_pass", "quality_pass"),
    ("draft", "needs_revision", "quality_fail"),
    ("draft", "needs_revision", "feedback_reopen"),
    ("needs_revision", "reviewed_pass", "quality_pass"),
    ("needs_revision", "needs_revision", "quality_fail"),
    ("reviewed_pass", "reviewed_pass", "quality_pass"),
    ("reviewed_pass", "needs_revision", "quality_fail"),
    ("reviewed_pass", "needs_revision", "feedback_reopen"),
    ("reviewed_pass", "approved", "human_approve"),
    ("approved", "approved", "quality_pass"),
    ("approved", "needs_revision", "quality_fail"),
    ("approved", "needs_revision", "feedback_reopen"),
}

PUBLISH_JOB_TRANSITIONS = {
    ("pending", "dry_run_ready", "dry_run"),
    ("dry_run_ready", "queued", "queue_for_platform"),
    ("queued", "published", "mark_published"),
    ("queued", "failed", "mark_failed"),
    ("failed", "queued", "retry"),
}


def assert_transition(entity: str, current: str, target: str, action: str) -> Transition:
    if entity == "chapter_version":
        allowed = CHAPTER_VERSION_TRANSITIONS
    elif entity == "publish_job":
        allowed = PUBLISH_JOB_TRANSITIONS
    else:
        raise WorkflowError(f"unknown workflow entity: {entity}")
    if (current, target, action) not in allowed:
        raise WorkflowError(f"invalid {entity} transition: {current} --{action}--> {target}")
    return Transition(entity=entity, from_status=current, to_status=target, action=action)


def move(entity: str, current: str, target: str, action: str) -> str:
    assert_transition(entity, current, target, action)
    return target
