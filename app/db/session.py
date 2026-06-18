from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import ROOT_DIR, settings


def _database_url() -> str:
    url = settings.database_url
    if url.startswith("sqlite:///"):
        db_path = url.removeprefix("sqlite:///")
        path = Path(db_path)
        if not path.is_absolute():
            path = ROOT_DIR / path
        path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{path}"
    return url


def _engine_kwargs(database_url: str) -> dict:
    if database_url.startswith("sqlite:///"):
        return {"connect_args": {"timeout": 120}}
    return {}


def _configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA busy_timeout=120000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
    finally:
        cursor.close()


def _create_engine(database_url: str):
    engine_obj = create_engine(database_url, future=True, **_engine_kwargs(database_url))
    if database_url.startswith("sqlite:///"):
        event.listen(engine_obj, "connect", _configure_sqlite_connection)
    return engine_obj


engine = _create_engine(_database_url())
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def configure_database(database_url: str) -> None:
    global engine, SessionLocal
    settings_url = settings.database_url
    object.__setattr__(settings, "database_url", database_url)
    try:
        engine = _create_engine(_database_url())
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    finally:
        object.__setattr__(settings, "database_url", database_url or settings_url)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
