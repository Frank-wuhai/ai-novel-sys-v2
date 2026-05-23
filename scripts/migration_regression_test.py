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
EXPECTED_HEAD = "20260523_0005"

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
        if not _has_target_unique_constraint(conn):
            return _fail("publishing_targets unique constraint is missing")
    finally:
        conn.close()
    print("migration-regression-test: PASS")
    print(f"database={TEST_DB_URL}")
    print(f"alembic_head={EXPECTED_HEAD}")
    print(f"table_count={len(tables)}")
    return 0


def _has_target_unique_constraint(conn: sqlite3.Connection) -> bool:
    for row in conn.execute("pragma index_list(publishing_targets)"):
        unique = int(row[2] or 0)
        index_name = row[1]
        if not unique:
            continue
        columns = [item[2] for item in conn.execute(f"pragma index_info({index_name})")]
        if columns == ["platform", "account_label", "work_identifier"]:
            return True
    return False


def _fail(message: str) -> int:
    print("migration-regression-test: FAIL")
    print(message)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
