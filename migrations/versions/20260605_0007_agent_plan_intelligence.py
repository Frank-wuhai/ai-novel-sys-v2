"""Add Agent Plan intelligence tables.

Revision ID: 20260605_0007
Revises: 20260524_0006
Create Date: 2026-06-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260605_0007"
down_revision = "20260524_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_embeddings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("book_id", sa.Integer(), sa.ForeignKey("books.id"), nullable=False),
        sa.Column("source_type", sa.String(length=80), nullable=False),
        sa.Column("source_ref_id", sa.String(length=120), nullable=True),
        sa.Column("source_label", sa.String(length=255), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding_json", sa.Text(), nullable=True),
        sa.Column("model", sa.String(length=160), nullable=True),
        sa.Column("dimensions", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_knowledge_embeddings_book_source", "knowledge_embeddings", ["book_id", "source_type"])
    op.create_index("ix_knowledge_embeddings_book_created", "knowledge_embeddings", ["book_id", "created_at"])

    op.create_table(
        "visual_assets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("book_id", sa.Integer(), sa.ForeignKey("books.id"), nullable=False),
        sa.Column("chapter_id", sa.Integer(), sa.ForeignKey("chapters.id"), nullable=True),
        sa.Column("asset_type", sa.String(length=80), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("model", sa.String(length=160), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=True),
        sa.Column("artifact_path", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_visual_assets_book_type_status", "visual_assets", ["book_id", "asset_type", "status"])
    op.create_index("ix_visual_assets_chapter_type", "visual_assets", ["chapter_id", "asset_type"])


def downgrade() -> None:
    op.drop_index("ix_visual_assets_chapter_type", table_name="visual_assets")
    op.drop_index("ix_visual_assets_book_type_status", table_name="visual_assets")
    op.drop_table("visual_assets")
    op.drop_index("ix_knowledge_embeddings_book_created", table_name="knowledge_embeddings")
    op.drop_index("ix_knowledge_embeddings_book_source", table_name="knowledge_embeddings")
    op.drop_table("knowledge_embeddings")
