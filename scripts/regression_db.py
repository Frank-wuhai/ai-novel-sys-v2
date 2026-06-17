from __future__ import annotations

from pathlib import Path

from app.db.init import reset_db
from app.db.session import configure_database


ROOT = Path(__file__).resolve().parents[1]


def isolated_database(name: str, *, reset: bool = True) -> str:
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in name.strip())
    if not safe:
        raise ValueError("regression database name is required")
    db_path = ROOT / "data" / f"{safe}.db"
    _unlink_sqlite_files(db_path)
    url = f"sqlite:///data/{safe}.db"
    configure_database(url)
    if reset:
        reset_db()
    return url


def sqlite_path(name: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in name.strip())
    return ROOT / "data" / f"{safe}.db"


def _unlink_sqlite_files(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for path in [db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")]:
        if path.exists():
            path.unlink()
