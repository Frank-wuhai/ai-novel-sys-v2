from __future__ import annotations

from pathlib import Path

from app.db.base import Base
from app.core.config import ROOT_DIR, settings
from app.db import session as db_session
from app.models import entities  # noqa: F401


LEGACY_COLUMNS = {
    "characters": {
        "book_id": "INTEGER",
        "role": "VARCHAR(120) DEFAULT ''",
        "ability": "TEXT DEFAULT ''",
    },
    "chapters": {
        "book_id": "INTEGER",
        "status": "VARCHAR(50) DEFAULT 'planned'",
        "created_at": "DATETIME",
    },
    "foreshadows": {
        "book_id": "INTEGER",
    },
}


def _legacy_columns(table: str) -> set[str]:
    with db_session.engine.connect() as conn:
        rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def _add_legacy_columns() -> None:
    with db_session.engine.begin() as conn:
        for table, columns in LEGACY_COLUMNS.items():
            existing = _legacy_columns(table)
            if not existing:
                continue
            for column, sql_type in columns.items():
                if column not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")


def init_db() -> None:
    Base.metadata.create_all(db_session.engine)
    _add_legacy_columns()


def reset_db() -> None:
    Base.metadata.drop_all(db_session.engine)
    Base.metadata.create_all(db_session.engine)
    _add_legacy_columns()


def current_sqlite_path() -> Path | None:
    url = settings.database_url
    if not url.startswith("sqlite:///"):
        return None
    raw = url.removeprefix("sqlite:///")
    path = Path(raw)
    return path if path.is_absolute() else ROOT_DIR / path


if __name__ == "__main__":
    init_db()
    print("数据库初始化完成")
