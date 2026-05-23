from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, inspect, select
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


def _migration_dir() -> Path:
    return ROOT_DIR / "migrations" / "versions"


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
