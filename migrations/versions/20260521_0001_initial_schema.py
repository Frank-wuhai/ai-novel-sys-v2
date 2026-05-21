"""Initial schema.

Revision ID: 20260521_0001
Revises:
Create Date: 2026-05-21
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260521_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "books",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("genre", sa.String(length=120), nullable=False),
        sa.Column("target_platform", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("title"),
    )
    op.create_table(
        "evidence_sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.String(length=160), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("reliability", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.UniqueConstraint("source_id"),
    )
    op.create_table(
        "prompt_templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("version", sa.String(length=40), nullable=False),
        sa.Column("template", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
    )
    op.create_table(
        "publishing_targets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("platform", sa.String(length=120), nullable=False),
        sa.Column("account_label", sa.String(length=120), nullable=False),
        sa.Column("automation_mode", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
    )
    op.create_table(
        "characters",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("book_id", sa.Integer(), sa.ForeignKey("books.id"), nullable=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("role", sa.String(length=120), nullable=False),
        sa.Column("personality", sa.Text(), nullable=False),
        sa.Column("ability", sa.Text(), nullable=False),
        sa.Column("background", sa.Text(), nullable=False),
    )
    op.create_table(
        "chapters",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("book_id", sa.Integer(), sa.ForeignKey("books.id"), nullable=False),
        sa.Column("chapter_number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("book_id", "chapter_number"),
    )
    op.create_table(
        "foreshadows",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("book_id", sa.Integer(), sa.ForeignKey("books.id"), nullable=True),
        sa.Column("setup_text", sa.Text(), nullable=False),
        sa.Column("payoff_text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
    )
    op.create_table(
        "generation_tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("book_id", sa.Integer(), sa.ForeignKey("books.id"), nullable=False),
        sa.Column("task_type", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("input_json", sa.Text(), nullable=False),
        sa.Column("output_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "market_signals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("evidence_sources.id"), nullable=True),
        sa.Column("genre", sa.String(length=120), nullable=False),
        sa.Column("signal_text", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "platform_feedback",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("book_id", sa.Integer(), sa.ForeignKey("books.id"), nullable=False),
        sa.Column("chapter_id", sa.Integer(), sa.ForeignKey("chapters.id"), nullable=True),
        sa.Column("platform", sa.String(length=120), nullable=False),
        sa.Column("metric_name", sa.String(length=120), nullable=False),
        sa.Column("metric_value", sa.String(length=120), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("collected_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "plot_threads",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("book_id", sa.Integer(), sa.ForeignKey("books.id"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
    )
    op.create_table(
        "power_systems",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("book_id", sa.Integer(), sa.ForeignKey("books.id"), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("rules", sa.Text(), nullable=False),
        sa.Column("costs", sa.Text(), nullable=False),
        sa.Column("limits", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
    )
    op.create_table(
        "volumes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("book_id", sa.Integer(), sa.ForeignKey("books.id"), nullable=False),
        sa.Column("volume_number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.UniqueConstraint("book_id", "volume_number"),
    )
    op.create_table(
        "story_arcs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("book_id", sa.Integer(), sa.ForeignKey("books.id"), nullable=False),
        sa.Column("volume_id", sa.Integer(), sa.ForeignKey("volumes.id"), nullable=True),
        sa.Column("arc_number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("start_chapter", sa.Integer(), nullable=False),
        sa.Column("end_chapter", sa.Integer(), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("climax", sa.Text(), nullable=False),
        sa.Column("turn", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("book_id", "arc_number"),
    )
    op.create_table(
        "story_bibles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("book_id", sa.Integer(), sa.ForeignKey("books.id"), nullable=False),
        sa.Column("positioning", sa.Text(), nullable=False),
        sa.Column("reader_promise", sa.Text(), nullable=False),
        sa.Column("main_plot", sa.Text(), nullable=False),
        sa.Column("protagonist_arc", sa.Text(), nullable=False),
        sa.Column("relationship_arc", sa.Text(), nullable=False),
        sa.Column("power_curve", sa.Text(), nullable=False),
        sa.Column("forbidden_rules", sa.Text(), nullable=False),
        sa.Column("style_guide", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("book_id"),
    )
    op.create_table(
        "story_foundations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("book_id", sa.Integer(), sa.ForeignKey("books.id"), nullable=False),
        sa.Column("premise", sa.Text(), nullable=False),
        sa.Column("reader_promise", sa.Text(), nullable=False),
        sa.Column("world_engine", sa.Text(), nullable=False),
        sa.Column("protagonist_engine", sa.Text(), nullable=False),
        sa.Column("conflict_engine", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "world_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("book_id", sa.Integer(), sa.ForeignKey("books.id"), nullable=False),
        sa.Column("category", sa.String(length=120), nullable=False),
        sa.Column("rule_text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
    )
    op.create_table(
        "character_states",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("character_id", sa.Integer(), sa.ForeignKey("characters.id"), nullable=False),
        sa.Column("chapter_id", sa.Integer(), sa.ForeignKey("chapters.id"), nullable=True),
        sa.Column("state_text", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=120), nullable=False),
    )
    op.create_table(
        "chapter_briefs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chapter_id", sa.Integer(), sa.ForeignKey("chapters.id"), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("required_beats", sa.Text(), nullable=False),
        sa.Column("constraints", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
    )
    op.create_table(
        "chapter_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chapter_id", sa.Integer(), sa.ForeignKey("chapters.id"), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("source", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "chapter_reviews",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chapter_version_id", sa.Integer(), sa.ForeignKey("chapter_versions.id"), nullable=False),
        sa.Column("verdict", sa.String(length=50), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("reviewer", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "publish_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chapter_version_id", sa.Integer(), sa.ForeignKey("chapter_versions.id"), nullable=False),
        sa.Column("platform", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("automation_payload", sa.Text(), nullable=False),
        sa.Column("result_report", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "quality_reports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chapter_version_id", sa.Integer(), sa.ForeignKey("chapter_versions.id"), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("report", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("quality_reports")
    op.drop_table("publish_jobs")
    op.drop_table("chapter_reviews")
    op.drop_table("chapter_versions")
    op.drop_table("chapter_briefs")
    op.drop_table("character_states")
    op.drop_table("world_rules")
    op.drop_table("volumes")
    op.drop_table("story_foundations")
    op.drop_table("story_bibles")
    op.drop_table("story_arcs")
    op.drop_table("power_systems")
    op.drop_table("plot_threads")
    op.drop_table("platform_feedback")
    op.drop_table("market_signals")
    op.drop_table("generation_tasks")
    op.drop_table("foreshadows")
    op.drop_table("chapters")
    op.drop_table("characters")
    op.drop_table("publishing_targets")
    op.drop_table("prompt_templates")
    op.drop_table("evidence_sources")
    op.drop_table("books")
