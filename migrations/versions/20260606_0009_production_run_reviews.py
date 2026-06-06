"""Add production run review records.

Revision ID: 20260606_0009
Revises: 20260606_0008
Create Date: 2026-06-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260606_0009"
down_revision = "20260606_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "production_run_reviews",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("book_id", sa.Integer(), sa.ForeignKey("books.id"), nullable=False),
        sa.Column("chapter_id", sa.Integer(), sa.ForeignKey("chapters.id"), nullable=False),
        sa.Column("chapter_version_id", sa.Integer(), sa.ForeignKey("chapter_versions.id"), nullable=True),
        sa.Column("generation_task_id", sa.Integer(), sa.ForeignKey("generation_tasks.id"), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=True),
        sa.Column("review_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_production_run_reviews_chapter_created", "production_run_reviews", ["chapter_id", "created_at"])
    op.create_index("ix_production_run_reviews_version", "production_run_reviews", ["chapter_version_id"])
    op.create_index("ix_production_run_reviews_task", "production_run_reviews", ["generation_task_id"])


def downgrade() -> None:
    op.drop_index("ix_production_run_reviews_task", table_name="production_run_reviews")
    op.drop_index("ix_production_run_reviews_version", table_name="production_run_reviews")
    op.drop_index("ix_production_run_reviews_chapter_created", table_name="production_run_reviews")
    op.drop_table("production_run_reviews")
