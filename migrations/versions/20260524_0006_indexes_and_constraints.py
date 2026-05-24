"""Add operational indexes and uniqueness constraints.

Revision ID: 20260524_0006
Revises: 20260523_0005
Create Date: 2026-05-24
"""

from __future__ import annotations

from alembic import op


revision = "20260524_0006"
down_revision = "20260523_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_characters_book_name", "characters", ["book_id", "name"])
    op.create_index("ix_chapters_book_status", "chapters", ["book_id", "status"])
    op.create_index("ix_chapter_versions_chapter_created", "chapter_versions", ["chapter_id", "created_at"])
    op.create_index("ix_database_backups_status_created", "database_backups", ["status", "created_at"])
    op.create_index("ix_feedback_adjustments_book_status", "feedback_adjustments", ["book_id", "status"])
    op.create_index("ix_feedback_adjustments_book_target", "feedback_adjustments", ["book_id", "target_chapter_number"])
    op.create_index("ix_foreshadows_book_status", "foreshadows", ["book_id", "status"])
    op.create_index("ix_generation_tasks_book_status_created", "generation_tasks", ["book_id", "status", "created_at"])
    op.create_index("ix_generation_tasks_book_type_status", "generation_tasks", ["book_id", "task_type", "status"])
    op.create_index("ix_generation_tasks_status_created", "generation_tasks", ["status", "created_at"])
    op.create_index("ix_llm_request_logs_book_created", "llm_request_logs", ["book_id", "created_at"])
    op.create_index("ix_llm_request_logs_book_status_created", "llm_request_logs", ["book_id", "status", "created_at"])
    op.create_index("ix_llm_request_logs_generation_task", "llm_request_logs", ["generation_task_id"])
    op.create_index("ix_market_signals_genre_confidence", "market_signals", ["genre", "confidence"])
    op.create_index("ix_market_signals_genre_created", "market_signals", ["genre", "created_at"])
    op.create_index("ix_platform_feedback_book_collected", "platform_feedback", ["book_id", "collected_at"])
    op.create_index("ix_platform_feedback_book_metric", "platform_feedback", ["book_id", "metric_name"])
    op.create_index("ix_plot_threads_book_status", "plot_threads", ["book_id", "status"])
    op.create_index("ix_prompt_templates_name_status", "prompt_templates", ["name", "status"])
    op.create_index("ix_publish_executions_job_created", "publish_executions", ["publish_job_id", "created_at"])
    op.create_index("ix_publish_executions_status_created", "publish_executions", ["status", "created_at"])
    op.create_index("ix_publish_jobs_platform_status", "publish_jobs", ["platform", "status"])
    op.create_index("ix_publish_jobs_status_created", "publish_jobs", ["status", "created_at"])
    op.create_index("ix_publish_jobs_version_status", "publish_jobs", ["chapter_version_id", "status"])
    op.create_index("ix_publishing_targets_platform_status", "publishing_targets", ["platform", "status"])
    op.create_index("ix_quality_reports_version_created", "quality_reports", ["chapter_version_id", "created_at"])
    op.create_index("ix_world_rules_book_status", "world_rules", ["book_id", "status"])
    with op.batch_alter_table("chapter_versions") as batch_op:
        batch_op.create_unique_constraint("uq_chapter_versions_chapter_version", ["chapter_id", "version_number"])
    with op.batch_alter_table("prompt_templates") as batch_op:
        batch_op.create_unique_constraint("uq_prompt_templates_name_version", ["name", "version"])


def downgrade() -> None:
    with op.batch_alter_table("prompt_templates") as batch_op:
        batch_op.drop_constraint("uq_prompt_templates_name_version", type_="unique")
    with op.batch_alter_table("chapter_versions") as batch_op:
        batch_op.drop_constraint("uq_chapter_versions_chapter_version", type_="unique")
    op.drop_index("ix_world_rules_book_status", table_name="world_rules")
    op.drop_index("ix_quality_reports_version_created", table_name="quality_reports")
    op.drop_index("ix_publishing_targets_platform_status", table_name="publishing_targets")
    op.drop_index("ix_publish_jobs_version_status", table_name="publish_jobs")
    op.drop_index("ix_publish_jobs_status_created", table_name="publish_jobs")
    op.drop_index("ix_publish_jobs_platform_status", table_name="publish_jobs")
    op.drop_index("ix_publish_executions_status_created", table_name="publish_executions")
    op.drop_index("ix_publish_executions_job_created", table_name="publish_executions")
    op.drop_index("ix_prompt_templates_name_status", table_name="prompt_templates")
    op.drop_index("ix_plot_threads_book_status", table_name="plot_threads")
    op.drop_index("ix_platform_feedback_book_metric", table_name="platform_feedback")
    op.drop_index("ix_platform_feedback_book_collected", table_name="platform_feedback")
    op.drop_index("ix_market_signals_genre_created", table_name="market_signals")
    op.drop_index("ix_market_signals_genre_confidence", table_name="market_signals")
    op.drop_index("ix_llm_request_logs_generation_task", table_name="llm_request_logs")
    op.drop_index("ix_llm_request_logs_book_status_created", table_name="llm_request_logs")
    op.drop_index("ix_llm_request_logs_book_created", table_name="llm_request_logs")
    op.drop_index("ix_generation_tasks_status_created", table_name="generation_tasks")
    op.drop_index("ix_generation_tasks_book_type_status", table_name="generation_tasks")
    op.drop_index("ix_generation_tasks_book_status_created", table_name="generation_tasks")
    op.drop_index("ix_foreshadows_book_status", table_name="foreshadows")
    op.drop_index("ix_feedback_adjustments_book_target", table_name="feedback_adjustments")
    op.drop_index("ix_feedback_adjustments_book_status", table_name="feedback_adjustments")
    op.drop_index("ix_database_backups_status_created", table_name="database_backups")
    op.drop_index("ix_chapter_versions_chapter_created", table_name="chapter_versions")
    op.drop_index("ix_chapters_book_status", table_name="chapters")
    op.drop_index("ix_characters_book_name", table_name="characters")
