from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def now() -> datetime:
    return datetime.utcnow()


class Book(Base):
    __tablename__ = "books"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    genre: Mapped[str] = mapped_column(String(120), default="")
    target_platform: Mapped[str] = mapped_column(String(120), default="")
    status: Mapped[str] = mapped_column(String(50), default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    volumes: Mapped[list["Volume"]] = relationship(back_populates="book", cascade="all, delete-orphan")
    chapters: Mapped[list["Chapter"]] = relationship(back_populates="book", cascade="all, delete-orphan")


class Volume(Base):
    __tablename__ = "volumes"
    __table_args__ = (UniqueConstraint("book_id", "volume_number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"), nullable=False)
    volume_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(255), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(50), default="planning")

    book: Mapped[Book] = relationship(back_populates="volumes")


class StoryFoundation(Base):
    __tablename__ = "story_foundations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"), nullable=False)
    premise: Mapped[str] = mapped_column(Text, nullable=False)
    reader_promise: Mapped[str] = mapped_column(Text, default="")
    world_engine: Mapped[str] = mapped_column(Text, default="")
    protagonist_engine: Mapped[str] = mapped_column(Text, default="")
    conflict_engine: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(50), default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class StoryBible(Base):
    __tablename__ = "story_bibles"
    __table_args__ = (UniqueConstraint("book_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"), nullable=False)
    positioning: Mapped[str] = mapped_column(Text, default="")
    reader_promise: Mapped[str] = mapped_column(Text, default="")
    main_plot: Mapped[str] = mapped_column(Text, default="")
    protagonist_arc: Mapped[str] = mapped_column(Text, default="")
    relationship_arc: Mapped[str] = mapped_column(Text, default="")
    power_curve: Mapped[str] = mapped_column(Text, default="")
    forbidden_rules: Mapped[str] = mapped_column(Text, default="")
    style_guide: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(50), default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class StoryArc(Base):
    __tablename__ = "story_arcs"
    __table_args__ = (UniqueConstraint("book_id", "arc_number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"), nullable=False)
    volume_id: Mapped[int | None] = mapped_column(ForeignKey("volumes.id"), nullable=True)
    arc_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(255), default="")
    start_chapter: Mapped[int] = mapped_column(Integer, default=1)
    end_chapter: Mapped[int] = mapped_column(Integer, default=1)
    goal: Mapped[str] = mapped_column(Text, default="")
    climax: Mapped[str] = mapped_column(Text, default="")
    turn: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(50), default="planning")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Character(Base):
    __tablename__ = "characters"
    __table_args__ = (Index("ix_characters_book_name", "book_id", "name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    book_id: Mapped[int | None] = mapped_column(ForeignKey("books.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[str] = mapped_column(String(120), default="")
    personality: Mapped[str] = mapped_column(Text, default="")
    ability: Mapped[str] = mapped_column(Text, default="")
    background: Mapped[str] = mapped_column(Text, default="")


class CharacterState(Base):
    __tablename__ = "character_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    character_id: Mapped[int] = mapped_column(ForeignKey("characters.id"), nullable=False)
    chapter_id: Mapped[int | None] = mapped_column(ForeignKey("chapters.id"), nullable=True)
    state_text: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(120), default="manual")


class WorldRule(Base):
    __tablename__ = "world_rules"
    __table_args__ = (Index("ix_world_rules_book_status", "book_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"), nullable=False)
    category: Mapped[str] = mapped_column(String(120), default="")
    rule_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="active")


class PowerSystem(Base):
    __tablename__ = "power_systems"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    rules: Mapped[str] = mapped_column(Text, default="")
    costs: Mapped[str] = mapped_column(Text, default="")
    limits: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(50), default="draft")


class PlotThread(Base):
    __tablename__ = "plot_threads"
    __table_args__ = (Index("ix_plot_threads_book_status", "book_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(50), default="open")


class Chapter(Base):
    __tablename__ = "chapters"
    __table_args__ = (
        UniqueConstraint("book_id", "chapter_number"),
        Index("ix_chapters_book_status", "book_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"), nullable=False)
    chapter_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(255), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(50), default="planned")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    book: Mapped[Book] = relationship(back_populates="chapters")
    versions: Mapped[list["ChapterVersion"]] = relationship(back_populates="chapter", cascade="all, delete-orphan")


class ChapterVersion(Base):
    __tablename__ = "chapter_versions"
    __table_args__ = (
        UniqueConstraint("chapter_id", "version_number", name="uq_chapter_versions_chapter_version"),
        Index("ix_chapter_versions_chapter_created", "chapter_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chapter_id: Mapped[int] = mapped_column(ForeignKey("chapters.id"), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(255), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(50), default="draft")
    source: Mapped[str] = mapped_column(String(120), default="llm")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    chapter: Mapped[Chapter] = relationship(back_populates="versions")


class ChapterBrief(Base):
    __tablename__ = "chapter_briefs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chapter_id: Mapped[int] = mapped_column(ForeignKey("chapters.id"), nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    required_beats: Mapped[str] = mapped_column(Text, default="")
    constraints: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(50), default="ready")


class ChapterUnitPlan(Base):
    __tablename__ = "chapter_unit_plans"
    __table_args__ = (
        Index("ix_chapter_unit_plans_chapter_created", "chapter_id", "created_at"),
        Index("ix_chapter_unit_plans_chapter_status", "chapter_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chapter_id: Mapped[int] = mapped_column(ForeignKey("chapters.id"), nullable=False)
    chapter_brief_id: Mapped[int | None] = mapped_column(ForeignKey("chapter_briefs.id"), nullable=True)
    source: Mapped[str] = mapped_column(String(120), default="system")
    status: Mapped[str] = mapped_column(String(50), default="active")
    plan_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ProductionRunReview(Base):
    __tablename__ = "production_run_reviews"
    __table_args__ = (
        Index("ix_production_run_reviews_chapter_created", "chapter_id", "created_at"),
        Index("ix_production_run_reviews_version", "chapter_version_id"),
        Index("ix_production_run_reviews_task", "generation_task_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"), nullable=False)
    chapter_id: Mapped[int] = mapped_column(ForeignKey("chapters.id"), nullable=False)
    chapter_version_id: Mapped[int | None] = mapped_column(ForeignKey("chapter_versions.id"), nullable=True)
    generation_task_id: Mapped[int | None] = mapped_column(ForeignKey("generation_tasks.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="recorded")
    review_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Foreshadow(Base):
    __tablename__ = "foreshadows"
    __table_args__ = (Index("ix_foreshadows_book_status", "book_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    book_id: Mapped[int | None] = mapped_column(ForeignKey("books.id"), nullable=True)
    setup_text: Mapped[str] = mapped_column(Text, nullable=False)
    payoff_text: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(50), default="open")


class ChapterReview(Base):
    __tablename__ = "chapter_reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chapter_version_id: Mapped[int] = mapped_column(ForeignKey("chapter_versions.id"), nullable=False)
    verdict: Mapped[str] = mapped_column(String(50), nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="")
    reviewer: Mapped[str] = mapped_column(String(120), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class QualityReport(Base):
    __tablename__ = "quality_reports"
    __table_args__ = (Index("ix_quality_reports_version_created", "chapter_version_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chapter_version_id: Mapped[int] = mapped_column(ForeignKey("chapter_versions.id"), nullable=False)
    score: Mapped[int] = mapped_column(Integer, default=0)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    report: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class GenerationTask(Base):
    __tablename__ = "generation_tasks"
    __table_args__ = (
        Index("ix_generation_tasks_status_created", "status", "created_at"),
        Index("ix_generation_tasks_book_status_created", "book_id", "status", "created_at"),
        Index("ix_generation_tasks_book_type_status", "book_id", "task_type", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"), nullable=False)
    task_type: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="pending")
    input_json: Mapped[str] = mapped_column(Text, default="{}")
    output_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class LLMRequestLog(Base):
    __tablename__ = "llm_request_logs"
    __table_args__ = (
        Index("ix_llm_request_logs_book_created", "book_id", "created_at"),
        Index("ix_llm_request_logs_book_status_created", "book_id", "status", "created_at"),
        Index("ix_llm_request_logs_generation_task", "generation_task_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generation_task_id: Mapped[int | None] = mapped_column(ForeignKey("generation_tasks.id"), nullable=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"), nullable=False)
    task_type: Mapped[str] = mapped_column(String(80), nullable=False)
    provider: Mapped[str] = mapped_column(String(120), default="")
    model: Mapped[str] = mapped_column(String(160), default="")
    request_id: Mapped[str] = mapped_column(String(160), default="")
    prompt_template: Mapped[str] = mapped_column(String(160), default="")
    prompt_chars: Mapped[int] = mapped_column(Integer, default=0)
    response_chars: Mapped[int] = mapped_column(Integer, default=0)
    estimated_prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_response_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    actual_prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    actual_response_tokens: Mapped[int] = mapped_column(Integer, default=0)
    actual_total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(50), default="completed")
    error_category: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class PromptTemplate(Base):
    __tablename__ = "prompt_templates"
    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_prompt_templates_name_version"),
        Index("ix_prompt_templates_name_status", "name", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[str] = mapped_column(String(40), default="v1")
    template: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="active")


class PublishingTarget(Base):
    __tablename__ = "publishing_targets"
    __table_args__ = (
        UniqueConstraint("platform", "account_label", "work_identifier"),
        Index("ix_publishing_targets_platform_status", "platform", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(120), nullable=False)
    account_label: Mapped[str] = mapped_column(String(120), default="")
    work_identifier: Mapped[str] = mapped_column(String(255), default="")
    automation_mode: Mapped[str] = mapped_column(String(80), default="manual")
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(50), default="active")


class PublishJob(Base):
    __tablename__ = "publish_jobs"
    __table_args__ = (
        Index("ix_publish_jobs_status_created", "status", "created_at"),
        Index("ix_publish_jobs_version_status", "chapter_version_id", "status"),
        Index("ix_publish_jobs_platform_status", "platform", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chapter_version_id: Mapped[int] = mapped_column(ForeignKey("chapter_versions.id"), nullable=False)
    platform: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="pending")
    automation_payload: Mapped[str] = mapped_column(Text, default="{}")
    result_report: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class PublishExecution(Base):
    __tablename__ = "publish_executions"
    __table_args__ = (
        Index("ix_publish_executions_job_created", "publish_job_id", "created_at"),
        Index("ix_publish_executions_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    publish_job_id: Mapped[int] = mapped_column(ForeignKey("publish_jobs.id"), nullable=False)
    platform: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    automation_mode: Mapped[str] = mapped_column(String(80), default="dry_run")
    report: Mapped[str] = mapped_column(Text, default="")
    artifact_path: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class DatabaseBackup(Base):
    __tablename__ = "database_backups"
    __table_args__ = (Index("ix_database_backups_status_created", "status", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    database_url: Mapped[str] = mapped_column(Text, nullable=False)
    backup_path: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="completed")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    report: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class PlatformFeedback(Base):
    __tablename__ = "platform_feedback"
    __table_args__ = (
        Index("ix_platform_feedback_book_collected", "book_id", "collected_at"),
        Index("ix_platform_feedback_book_metric", "book_id", "metric_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"), nullable=False)
    chapter_id: Mapped[int | None] = mapped_column(ForeignKey("chapters.id"), nullable=True)
    platform: Mapped[str] = mapped_column(String(120), nullable=False)
    metric_name: Mapped[str] = mapped_column(String(120), nullable=False)
    metric_value: Mapped[str] = mapped_column(String(120), default="")
    raw_text: Mapped[str] = mapped_column(Text, default="")
    collected_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class FeedbackAdjustment(Base):
    __tablename__ = "feedback_adjustments"
    __table_args__ = (
        Index("ix_feedback_adjustments_book_status", "book_id", "status"),
        Index("ix_feedback_adjustments_book_target", "book_id", "target_chapter_number"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"), nullable=False)
    chapter_id: Mapped[int | None] = mapped_column(ForeignKey("chapters.id"), nullable=True)
    target_chapter_number: Mapped[int] = mapped_column(Integer, nullable=False)
    feedback_ids: Mapped[str] = mapped_column(Text, default="")
    adjustment_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="ready")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class EvidenceSource(Base):
    __tablename__ = "evidence_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), default="")
    url: Mapped[str] = mapped_column(Text, default="")
    reliability: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(50), default="candidate")


class MarketSignal(Base):
    __tablename__ = "market_signals"
    __table_args__ = (
        Index("ix_market_signals_genre_confidence", "genre", "confidence"),
        Index("ix_market_signals_genre_created", "genre", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("evidence_sources.id"), nullable=True)
    genre: Mapped[str] = mapped_column(String(120), default="")
    signal_text: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class KnowledgeEmbedding(Base):
    __tablename__ = "knowledge_embeddings"
    __table_args__ = (
        Index("ix_knowledge_embeddings_book_source", "book_id", "source_type"),
        Index("ix_knowledge_embeddings_book_created", "book_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"), nullable=False)
    source_type: Mapped[str] = mapped_column(String(80), nullable=False)
    source_ref_id: Mapped[str] = mapped_column(String(120), default="")
    source_label: Mapped[str] = mapped_column(String(255), default="")
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_json: Mapped[str] = mapped_column(Text, default="[]")
    model: Mapped[str] = mapped_column(String(160), default="")
    dimensions: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class VisualAsset(Base):
    __tablename__ = "visual_assets"
    __table_args__ = (
        Index("ix_visual_assets_book_type_status", "book_id", "asset_type", "status"),
        Index("ix_visual_assets_chapter_type", "chapter_id", "asset_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id"), nullable=False)
    chapter_id: Mapped[int | None] = mapped_column(ForeignKey("chapters.id"), nullable=True)
    asset_type: Mapped[str] = mapped_column(String(80), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String(160), default="")
    status: Mapped[str] = mapped_column(String(50), default="planned")
    artifact_path: Mapped[str] = mapped_column(Text, default="")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
