from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / "venv/bin/python"
TEST_DB = ROOT / "data/migration-regression.db"
TEST_DB_URL = "sqlite:///data/migration-regression.db"
EXPECTED_HEAD = "20260524_0006"

REQUIRED_TABLES = {
    "alembic_version",
    "books",
    "chapter_versions",
    "database_backups",
    "feedback_adjustments",
    "generation_tasks",
    "llm_request_logs",
    "platform_feedback",
    "publish_executions",
    "publish_jobs",
    "publishing_targets",
    "quality_reports",
    "story_bibles",
}

REQUIRED_COLUMNS = {
    "llm_request_logs": {
        "actual_prompt_tokens",
        "actual_response_tokens",
        "actual_total_tokens",
        "estimated_total_tokens",
        "request_id",
    },
    "publishing_targets": {
        "account_label",
        "automation_mode",
        "config_json",
        "platform",
        "status",
        "work_identifier",
    },
    "publish_executions": {
        "artifact_path",
        "automation_mode",
        "publish_job_id",
        "report",
        "status",
    },
    "database_backups": {
        "backup_path",
        "database_url",
        "report",
        "size_bytes",
        "status",
    },
    "feedback_adjustments": {
        "adjustment_text",
        "feedback_ids",
        "status",
        "target_chapter_number",
    },
}

REQUIRED_INDEXES = {
    "chapter_versions": {
        "ix_chapter_versions_chapter_created",
    },
    "generation_tasks": {
        "ix_generation_tasks_book_status_created",
        "ix_generation_tasks_book_type_status",
        "ix_generation_tasks_status_created",
    },
    "llm_request_logs": {
        "ix_llm_request_logs_book_created",
        "ix_llm_request_logs_book_status_created",
        "ix_llm_request_logs_generation_task",
    },
    "platform_feedback": {
        "ix_platform_feedback_book_collected",
        "ix_platform_feedback_book_metric",
    },
    "publish_jobs": {
        "ix_publish_jobs_platform_status",
        "ix_publish_jobs_status_created",
        "ix_publish_jobs_version_status",
    },
    "publish_executions": {
        "ix_publish_executions_job_created",
        "ix_publish_executions_status_created",
    },
    "quality_reports": {
        "ix_quality_reports_version_created",
    },
}

REQUIRED_UNIQUE_INDEX_COLUMNS = {
    "chapter_versions": {"chapter_id", "version_number"},
    "prompt_templates": {"name", "version"},
    "publishing_targets": {"platform", "account_label", "work_identifier"},
}


def main() -> int:
    if not _alembic_available():
        print("migration-regression-test: SKIP")
        print("reason=missing python package: alembic")
        print("hint=install project dependencies, then run: venv/bin/python scripts/migration_regression_test.py")
        return 2
    TEST_DB.parent.mkdir(parents=True, exist_ok=True)
    if TEST_DB.exists():
        TEST_DB.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(TEST_DB) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    env = os.environ.copy()
    env["DATABASE_URL"] = TEST_DB_URL
    result = subprocess.run(
        [str(PYTHON), "-m", "alembic", "upgrade", "head"],
        cwd=str(ROOT),
        env=env,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        print("migration-regression-test: FAIL")
        print("command=python -m alembic upgrade head")
        print(result.stdout.strip())
        print(result.stderr.strip())
        return 1
    return _inspect_schema()


def _alembic_available() -> bool:
    result = subprocess.run(
        [str(PYTHON), "-c", "import alembic"],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
    )
    return result.returncode == 0


def _inspect_schema() -> int:
    conn = sqlite3.connect(TEST_DB)
    try:
        tables = {row[0] for row in conn.execute("select name from sqlite_master where type='table'")}
        missing_tables = sorted(REQUIRED_TABLES - tables)
        if missing_tables:
            return _fail("missing_tables=" + ",".join(missing_tables))
        version = conn.execute("select version_num from alembic_version").fetchone()
        if not version or version[0] != EXPECTED_HEAD:
            return _fail(f"expected_head={EXPECTED_HEAD} actual_head={version[0] if version else ''}")
        for table, expected_columns in REQUIRED_COLUMNS.items():
            columns = {row[1] for row in conn.execute(f"pragma table_info({table})")}
            missing_columns = sorted(expected_columns - columns)
            if missing_columns:
                return _fail(f"table={table} missing_columns={','.join(missing_columns)}")
        for table, expected_indexes in REQUIRED_INDEXES.items():
            indexes = {row[1] for row in conn.execute(f"pragma index_list({table})")}
            missing_indexes = sorted(expected_indexes - indexes)
            if missing_indexes:
                return _fail(f"table={table} missing_indexes={','.join(missing_indexes)}")
        for table, columns in REQUIRED_UNIQUE_INDEX_COLUMNS.items():
            if not _has_unique_index_columns(conn, table=table, columns=columns):
                return _fail(f"table={table} unique_columns_missing={','.join(sorted(columns))}")
    finally:
        conn.close()
    schema_result = subprocess.run(
        [str(PYTHON), "-m", "app.cli", "--database-url", TEST_DB_URL, "schema-version"],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
    )
    schema_output = (schema_result.stdout + schema_result.stderr).strip()
    if schema_result.returncode != 0:
        return _fail("schema-version command failed\n" + schema_output)
    if f"status=current" not in schema_output or f"expected_head={EXPECTED_HEAD}" not in schema_output:
        return _fail("schema-version did not report current head\n" + schema_output)
    print("migration-regression-test: PASS")
    print(f"database={TEST_DB_URL}")
    print(f"alembic_head={EXPECTED_HEAD}")
    print(f"table_count={len(tables)}")
    return 0


def _has_unique_index_columns(conn: sqlite3.Connection, *, table: str, columns: set[str]) -> bool:
    for row in conn.execute(f"pragma index_list({table})"):
        unique = int(row[2] or 0)
        index_name = row[1]
        if not unique:
            continue
        index_columns = {item[2] for item in conn.execute(f"pragma index_info({index_name})")}
        if index_columns == columns:
            return True
    return False


def _fail(message: str) -> int:
    print("migration-regression-test: FAIL")
    print(message)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
