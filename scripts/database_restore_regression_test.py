from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / "venv/bin/python"
TEST_DB = ROOT / "data/database-restore-regression.db"
TEST_DB_URL = "sqlite:///data/database-restore-regression.db"


def main() -> int:
    TEST_DB.parent.mkdir(parents=True, exist_ok=True)
    _unlink_sqlite_files(TEST_DB)

    reset = _run(["reset-dev-db", "--yes"])
    if "reset-dev-db: PASS" not in reset:
        return _fail("reset-dev-db failed", reset)

    base_book = _run(["create-book", "--title", "Restore Regression Base", "--genre", "测试", "--platform", "manual"])
    base_book_id = _extract_id("book_id", base_book)
    base_list = _run(["list-books"])
    if f"{base_book_id}\tRestore Regression Base" not in base_list:
        return _fail("base book was not created before backup", base_list)

    backup_output = _run(["backup-database", "--label", "restore-regression"])
    backup_id = _extract_id("database_backup_id", backup_output)
    backup_path = Path(_extract_value("backup_path", backup_output))
    if not backup_path.exists() or backup_path.stat().st_size <= 0:
        return _fail("backup file was not created", backup_output)

    backup_list = _run(["list-database-backups", "--limit", "3"])
    if f"{backup_id}\tstatus=completed" not in backup_list:
        return _fail("backup registry did not include created backup", backup_list)

    guard = _run(["restore-database", "--backup-path", str(backup_path)], expect=1)
    if "restore-database requires --yes" not in guard:
        return _fail("restore did not require explicit --yes", guard)

    marker_book = _run(["create-book", "--title", "Restore Regression Marker", "--genre", "测试", "--platform", "manual"])
    marker_book_id = _extract_id("book_id", marker_book)
    dirty_list = _run(["list-books"])
    if f"{marker_book_id}\tRestore Regression Marker" not in dirty_list:
        return _fail("marker book was not created before restore", dirty_list)

    restore_output = _run(["restore-database", "--backup-path", str(backup_path), "--yes"])
    pre_restore_path = Path(_extract_value("pre_restore_backup_path", restore_output))
    if "restore-database: PASS" not in restore_output:
        return _fail("restore command did not pass", restore_output)
    if not pre_restore_path.exists() or pre_restore_path.stat().st_size <= 0:
        return _fail("pre-restore backup file was not preserved", restore_output)

    restored_list = _run(["list-books"])
    if "Restore Regression Marker" in restored_list:
        return _fail("restore did not roll back marker book", restored_list)
    if f"{base_book_id}\tRestore Regression Base" not in restored_list:
        return _fail("restore lost base book from backup", restored_list)

    health = _run(["database-health"])
    if "database_backups" not in health or "books" not in health:
        return _fail("restored database health is missing expected tables", health)

    print("database-restore-regression-test: PASS")
    print(f"database={TEST_DB_URL}")
    print(f"backup_path={backup_path}")
    print(f"pre_restore_backup_path={pre_restore_path}")
    return 0


def _run(args: list[str], *, expect: int = 0) -> str:
    cmd = [str(PYTHON), "-m", "app.cli", "--database-url", TEST_DB_URL, *args]
    result = subprocess.run(
        cmd,
        cwd=str(ROOT),
        text=True,
        capture_output=True,
    )
    output = (result.stdout + result.stderr).strip()
    if result.returncode != expect:
        print("database-restore-regression-test: FAIL")
        print("command=" + " ".join(cmd))
        print(f"expected_returncode={expect}")
        print(f"actual_returncode={result.returncode}")
        print(output)
        raise SystemExit(1)
    return output


def _unlink_sqlite_files(db_path: Path) -> None:
    for path in [db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")]:
        if path.exists():
            path.unlink()


def _extract_id(name: str, output: str) -> int:
    return int(_extract_value(name, output))


def _extract_value(name: str, output: str) -> str:
    prefix = f"{name}="
    for line in output.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    raise ValueError(f"missing {name} in output:\n{output}")


def _fail(message: str, detail: str) -> int:
    print("database-restore-regression-test: FAIL")
    print(message)
    print(detail)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
