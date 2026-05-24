from __future__ import annotations

import shutil
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, inspect, select, text
from sqlalchemy.orm import Session

from app.core.config import ROOT_DIR, settings
from app.db import session as db_session
from app.db.init import current_sqlite_path
from app.models.entities import DatabaseBackup


@dataclass(frozen=True)
class DatabaseHealth:
    database_url: str
    sqlite_path: str
    table_count: int
    tables: list[str]
    migration_count: int
    latest_migration: str
    backup_count: int


@dataclass(frozen=True)
class SchemaVersionReport:
    database_url: str
    current_versions: list[str]
    expected_head: str
    status: str
    migration_count: int
    latest_migration: str
    message: str


@dataclass(frozen=True)
class DatabaseRestoreResult:
    database_path: str
    source_backup_path: str
    pre_restore_backup_path: str
    restored_size_bytes: int


def create_database_backup(session: Session, *, label: str = "") -> DatabaseBackup:
    db_path = current_sqlite_path()
    if db_path is None:
        raise ValueError("database backup currently supports sqlite databases only")
    if not db_path.exists():
        raise ValueError(f"sqlite database file does not exist: {db_path}")
    backup_path = _copy_sqlite_backup(db_path, label=label)
    size = backup_path.stat().st_size
    backup = DatabaseBackup(
        database_url=settings.database_url,
        backup_path=str(backup_path),
        status="completed",
        size_bytes=size,
        report=f"sqlite backup copied from {db_path}",
    )
    session.add(backup)
    session.flush()
    return backup


def restore_database_from_backup(*, backup_path: str, confirm: bool = False) -> DatabaseRestoreResult:
    if not confirm:
        raise ValueError("restore-database requires --yes because it overwrites the current sqlite database")
    db_path = current_sqlite_path()
    if db_path is None:
        raise ValueError("database restore currently supports sqlite databases only")
    if not db_path.exists():
        raise ValueError(f"sqlite database file does not exist: {db_path}")
    source_path = _resolve_backup_path(backup_path)
    if not source_path.exists():
        raise ValueError(f"backup file does not exist: {source_path}")
    if not source_path.is_file():
        raise ValueError(f"backup path is not a file: {source_path}")
    pre_restore_path = _copy_sqlite_backup(db_path, label="before-restore")
    db_session.engine.dispose()
    shutil.copy2(source_path, db_path)
    return DatabaseRestoreResult(
        database_path=str(db_path),
        source_backup_path=str(source_path),
        pre_restore_backup_path=str(pre_restore_path),
        restored_size_bytes=db_path.stat().st_size,
    )


def list_database_backups(session: Session, *, limit: int = 20) -> list[DatabaseBackup]:
    stmt = select(DatabaseBackup).order_by(DatabaseBackup.id.desc()).limit(limit)
    return list(session.scalars(stmt))


def check_database_health(session: Session) -> DatabaseHealth:
    inspector = inspect(db_session.engine)
    tables = sorted(inspector.get_table_names())
    migrations = sorted(path.name for path in _migration_dir().glob("*.py") if path.name != "__init__.py")
    backup_count = int(session.scalar(select(func.count(DatabaseBackup.id))) or 0)
    db_path = current_sqlite_path()
    return DatabaseHealth(
        database_url=settings.database_url,
        sqlite_path=str(db_path or ""),
        table_count=len(tables),
        tables=tables,
        migration_count=len(migrations),
        latest_migration=migrations[-1] if migrations else "",
        backup_count=backup_count,
    )


def check_schema_version(session: Session) -> SchemaVersionReport:
    migrations = _migration_revisions()
    expected_head = _expected_migration_head(migrations)
    current_versions = _current_alembic_versions(session)
    current_set = set(current_versions)
    known_revisions = set(migrations.values())
    expected_set = set(expected_head.split(",")) if expected_head else set()
    latest_migration = _latest_migration_name()

    if not expected_head:
        status = "no_migrations"
        message = "no migration files found"
    elif not current_versions:
        status = "unversioned"
        message = "database has no alembic_version entry; run alembic upgrade head for durable databases"
    elif current_set == expected_set:
        status = "current"
        message = "database schema is at expected head"
    elif current_set.issubset(known_revisions):
        status = "behind"
        message = f"database schema is behind expected head {expected_head}"
    elif expected_set.issubset(current_set):
        status = "current_with_extra_heads"
        message = "database includes expected head plus additional alembic heads"
    else:
        status = "ahead_or_diverged"
        message = "database schema version is not recognized by this code checkout"

    return SchemaVersionReport(
        database_url=settings.database_url,
        current_versions=current_versions,
        expected_head=expected_head,
        status=status,
        migration_count=len(migrations),
        latest_migration=latest_migration,
        message=message,
    )


def _migration_dir() -> Path:
    return ROOT_DIR / "migrations" / "versions"


def _latest_migration_name() -> str:
    migrations = sorted(path.name for path in _migration_dir().glob("*.py") if path.name != "__init__.py")
    return migrations[-1] if migrations else ""


def _migration_revisions() -> dict[str, str]:
    revisions: dict[str, str] = {}
    for path in sorted(_migration_dir().glob("*.py")):
        if path.name == "__init__.py":
            continue
        text_value = path.read_text(encoding="utf-8")
        match = re.search(r"^revision\s*=\s*['\"]([^'\"]+)['\"]", text_value, re.MULTILINE)
        if match:
            revisions[path.name] = match.group(1)
    return revisions


def _expected_migration_head(migrations: dict[str, str]) -> str:
    down_revisions: set[str] = set()
    for path in sorted(_migration_dir().glob("*.py")):
        if path.name == "__init__.py":
            continue
        text_value = path.read_text(encoding="utf-8")
        match = re.search(r"^down_revision\s*=\s*['\"]([^'\"]+)['\"]", text_value, re.MULTILINE)
        if match:
            down_revisions.add(match.group(1))
    heads = sorted(set(migrations.values()) - down_revisions)
    return ",".join(heads)


def _current_alembic_versions(session: Session) -> list[str]:
    inspector = inspect(db_session.engine)
    if "alembic_version" not in inspector.get_table_names():
        return []
    rows = session.execute(text("select version_num from alembic_version")).all()
    return sorted(str(row[0]) for row in rows if row[0])


def _copy_sqlite_backup(db_path: Path, *, label: str = "") -> Path:
    safe_label = "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in label.strip())
    suffix = f"-{safe_label}" if safe_label else ""
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    backup_dir = ROOT_DIR / "data" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{db_path.stem}-{stamp}{suffix}{db_path.suffix or '.db'}"
    shutil.copy2(db_path, backup_path)
    return backup_path


def _resolve_backup_path(value: str) -> Path:
    if not value:
        raise ValueError("backup path is required")
    path = Path(value)
    return path if path.is_absolute() else ROOT_DIR / path
