from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from app.services.story_bible_logic_gate import evaluate_story_bible_logic


APPROVED_STATUSES = ("approved", "frozen")
EDITABLE_STATUSES = ("draft", "review_ready", "rejected")
CARD_TYPES = ("character", "world_rule", "power_system", "plot_rule", "style_rule")


@dataclass(frozen=True)
class CanonCardAudit:
    passed: bool
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"passed": self.passed, "issues": self.issues, "warnings": self.warnings}


@dataclass(frozen=True)
class CanonCardRecord:
    id: int
    book_id: int
    card_type: str
    name: str
    status: str
    payload: dict[str, Any]
    audit: dict[str, Any]


def ensure_canon_cards_table(session: Session) -> None:
    session.execute(
        sql_text(
            """
            CREATE TABLE IF NOT EXISTS canon_cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id INTEGER NOT NULL,
                card_type TEXT NOT NULL,
                name TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'review_ready',
                source TEXT NOT NULL DEFAULT 'manual',
                audit_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(book_id, card_type, name)
            )
            """
        )
    )
    session.execute(sql_text("CREATE INDEX IF NOT EXISTS ix_canon_cards_book_status ON canon_cards(book_id, status)"))
    session.flush()


def audit_canon_card(payload: dict[str, Any], *, story_bible_text: str = "", constraints: str = "") -> CanonCardAudit:
    text = json.dumps(payload or {}, ensure_ascii=False)
    issues: list[str] = []
    warnings: list[str] = []
    if not text.strip() or text == "{}":
        issues.append("empty_card_payload")
    if any(marker in text for marker in ("待补", "随便", "以后再说", "TBD", "todo")):
        issues.append("placeholder_card_content")
    logic = evaluate_story_bible_logic(text, story_bible_text=story_bible_text, constraints=constraints)
    issues.extend(f"story_bible_logic:{issue}" for issue in logic.issues)
    warnings.extend(f"story_bible_logic:{warning}" for warning in logic.warnings)
    return CanonCardAudit(passed=not issues, issues=issues, warnings=warnings)


def upsert_canon_card(
    session: Session,
    *,
    book_id: int,
    card_type: str,
    name: str,
    payload: dict[str, Any],
    source: str = "manual",
    status: str = "review_ready",
    story_bible_text: str = "",
    constraints: str = "",
) -> CanonCardRecord:
    ensure_canon_cards_table(session)
    card_type = _validate_card_type(card_type)
    name = str(name or "").strip()
    if not name:
        raise ValueError("canon card name is required")
    if status not in {"draft", "review_ready"}:
        raise ValueError("upsert can only create editable draft/review_ready cards")
    audit = audit_canon_card(payload, story_bible_text=story_bible_text, constraints=constraints)
    now = _now()
    existing = session.execute(
        sql_text("SELECT id, status FROM canon_cards WHERE book_id=:book_id AND card_type=:card_type AND name=:name"),
        {"book_id": book_id, "card_type": card_type, "name": name},
    ).fetchone()
    if existing and existing[1] == "frozen":
        raise ValueError(f"canon card is frozen and cannot be edited: {card_type}:{name}")
    params = {
        "book_id": book_id,
        "card_type": card_type,
        "name": name,
        "payload_json": json.dumps(payload or {}, ensure_ascii=False),
        "status": status,
        "source": source,
        "audit_json": json.dumps(audit.to_dict(), ensure_ascii=False),
        "now": now,
    }
    if existing:
        session.execute(
            sql_text(
                """
                UPDATE canon_cards
                SET payload_json=:payload_json, status=:status, source=:source, audit_json=:audit_json, updated_at=:now
                WHERE id=:id
                """
            ),
            {**params, "id": existing[0]},
        )
        card_id = int(existing[0])
    else:
        result = session.execute(
            sql_text(
                """
                INSERT INTO canon_cards(book_id, card_type, name, payload_json, status, source, audit_json, created_at, updated_at)
                VALUES(:book_id, :card_type, :name, :payload_json, :status, :source, :audit_json, :now, :now)
                """
            ),
            params,
        )
        card_id = int(result.lastrowid)
    session.flush()
    return get_canon_card(session, card_id=card_id)


def approve_canon_card(session: Session, *, card_id: int, force: bool = False) -> CanonCardRecord:
    ensure_canon_cards_table(session)
    card = get_canon_card(session, card_id=card_id)
    if card.status == "frozen":
        return card
    if card.status not in EDITABLE_STATUSES:
        raise ValueError(f"canon card cannot be approved from status={card.status}")
    audit = card.audit if isinstance(card.audit, dict) else {}
    if not force and not bool(audit.get("passed")):
        raise ValueError(f"canon card audit failed: {audit.get('issues')}")
    session.execute(
        sql_text("UPDATE canon_cards SET status='approved', updated_at=:now WHERE id=:id"),
        {"id": card_id, "now": _now()},
    )
    session.flush()
    return get_canon_card(session, card_id=card_id)


def freeze_canon_cards(session: Session, *, book_id: int, card_ids: list[int] | None = None) -> int:
    ensure_canon_cards_table(session)
    params: dict[str, Any] = {"book_id": book_id, "now": _now()}
    where = "book_id=:book_id AND status='approved'"
    if card_ids:
        placeholders = []
        for idx, card_id in enumerate(card_ids):
            key = f"id{idx}"
            params[key] = int(card_id)
            placeholders.append(f":{key}")
        where += f" AND id IN ({','.join(placeholders)})"
    result = session.execute(
        sql_text(f"UPDATE canon_cards SET status='frozen', updated_at=:now WHERE {where}"),
        params,
    )
    session.flush()
    return int(result.rowcount or 0)


def get_canon_card(session: Session, *, card_id: int) -> CanonCardRecord:
    ensure_canon_cards_table(session)
    row = session.execute(
        sql_text("SELECT id, book_id, card_type, name, status, payload_json, audit_json FROM canon_cards WHERE id=:id"),
        {"id": card_id},
    ).fetchone()
    if not row:
        raise ValueError(f"canon card not found: {card_id}")
    return _record_from_row(row)


def format_approved_canon_cards(session: Session, *, book_id: int, limit: int = 12) -> tuple[str, list[int]]:
    ensure_canon_cards_table(session)
    rows = session.execute(
        sql_text(
            """
            SELECT id, book_id, card_type, name, status, payload_json, audit_json
            FROM canon_cards
            WHERE book_id=:book_id AND status IN ('approved', 'frozen')
            ORDER BY CASE status WHEN 'frozen' THEN 0 ELSE 1 END, id
            LIMIT :limit
            """
        ),
        {"book_id": book_id, "limit": limit},
    ).fetchall()
    records = [_record_from_row(row) for row in rows]
    if not records:
        return "", []
    lines = ["治理卡片（仅已批准/冻结，草稿不得作为 Canon）："]
    for card in records:
        lines.append(f"- card#{card.id} [{card.status}/{card.card_type}] {card.name}: {_compact_payload(card.payload)}")
    return "\n".join(lines), [card.id for card in records]


def _record_from_row(row) -> CanonCardRecord:
    try:
        payload = json.loads(row[5] or "{}")
    except json.JSONDecodeError:
        payload = {}
    try:
        audit = json.loads(row[6] or "{}")
    except json.JSONDecodeError:
        audit = {}
    return CanonCardRecord(
        id=int(row[0]),
        book_id=int(row[1]),
        card_type=str(row[2]),
        name=str(row[3]),
        status=str(row[4]),
        payload=payload if isinstance(payload, dict) else {},
        audit=audit if isinstance(audit, dict) else {},
    )


def _validate_card_type(card_type: str) -> str:
    value = str(card_type or "").strip()
    if value not in CARD_TYPES:
        raise ValueError(f"unsupported canon card type: {card_type}")
    return value


def _compact_payload(payload: dict[str, Any]) -> str:
    pairs = []
    for key, value in (payload or {}).items():
        if value in (None, "", [], {}):
            continue
        pairs.append(f"{key}={str(value).strip()}")
    return "；".join(pairs)[:260]


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")
