"""Add chapter unit plans.

Revision ID: 20260606_0008
Revises: 20260605_0007
Create Date: 2026-06-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260606_0008"
down_revision = "20260605_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chapter_unit_plans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chapter_id", sa.Integer(), sa.ForeignKey("chapters.id"), nullable=False),
        sa.Column("chapter_brief_id", sa.Integer(), sa.ForeignKey("chapter_briefs.id"), nullable=True),
        sa.Column("source", sa.String(length=120), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=True),
        sa.Column("plan_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_chapter_unit_plans_chapter_created", "chapter_unit_plans", ["chapter_id", "created_at"])
    op.create_index("ix_chapter_unit_plans_chapter_status", "chapter_unit_plans", ["chapter_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_chapter_unit_plans_chapter_status", table_name="chapter_unit_plans")
    op.drop_index("ix_chapter_unit_plans_chapter_created", table_name="chapter_unit_plans")
    op.drop_table("chapter_unit_plans")
