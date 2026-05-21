"""Add production operation audit tables.

Revision ID: 20260522_0003
Revises: 20260521_0002
Create Date: 2026-05-22
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260522_0003"
down_revision = "20260521_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_request_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("generation_task_id", sa.Integer(), sa.ForeignKey("generation_tasks.id"), nullable=True),
        sa.Column("book_id", sa.Integer(), sa.ForeignKey("books.id"), nullable=False),
        sa.Column("task_type", sa.String(length=80), nullable=False),
        sa.Column("provider", sa.String(length=120), nullable=False),
        sa.Column("model", sa.String(length=160), nullable=False),
        sa.Column("request_id", sa.String(length=160), nullable=False),
        sa.Column("prompt_template", sa.String(length=160), nullable=False),
        sa.Column("prompt_chars", sa.Integer(), nullable=False),
        sa.Column("response_chars", sa.Integer(), nullable=False),
        sa.Column("estimated_prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("estimated_response_tokens", sa.Integer(), nullable=False),
        sa.Column("estimated_total_tokens", sa.Integer(), nullable=False),
        sa.Column("elapsed_ms", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("error_category", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "publish_executions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("publish_job_id", sa.Integer(), sa.ForeignKey("publish_jobs.id"), nullable=False),
        sa.Column("platform", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("automation_mode", sa.String(length=80), nullable=False),
        sa.Column("report", sa.Text(), nullable=False),
        sa.Column("artifact_path", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "database_backups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("database_url", sa.Text(), nullable=False),
        sa.Column("backup_path", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("report", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("database_backups")
    op.drop_table("publish_executions")
    op.drop_table("llm_request_logs")
