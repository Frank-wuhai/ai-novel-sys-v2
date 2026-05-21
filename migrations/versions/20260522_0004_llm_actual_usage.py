"""Add actual LLM usage token columns.

Revision ID: 20260522_0004
Revises: 20260522_0003
Create Date: 2026-05-22
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260522_0004"
down_revision = "20260522_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("llm_request_logs", sa.Column("actual_prompt_tokens", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("llm_request_logs", sa.Column("actual_response_tokens", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("llm_request_logs", sa.Column("actual_total_tokens", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("llm_request_logs", "actual_total_tokens")
    op.drop_column("llm_request_logs", "actual_response_tokens")
    op.drop_column("llm_request_logs", "actual_prompt_tokens")
