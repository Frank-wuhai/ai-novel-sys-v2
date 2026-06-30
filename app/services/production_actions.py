from __future__ import annotations


AUTO_ACTIONS = {
    "create_chapter_brief",
    "draft_chapter",
    "generate_chapter_samples",
    "adopt_recommended_chapter_sample",
    "repair_chapter_brief",
    "enqueue_draft_chapter",
    "review_chapter",
    "create_revision_brief",
    "generate_rebuild_candidates",
    "revision_trend_recovery",
    "revision_budget_recovery",
    "revise_chapter",
    "enqueue_revise_chapter",
    "record_chapter_continuity",
    "create_publish_job",
    "publish_job_dry_run",
    "queue_publish_job",
    "retry_publish_job",
}

MANUAL_ACTIONS = {"approve_chapter", "mark_publish_job"}

LEGACY_AUTO_ACTIONS = {"reading_assessment_review"}
