from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import EvidenceSource, MarketSignal


@dataclass(frozen=True)
class EvidenceAuditItem:
    signal_id: int
    genre: str
    confidence: int
    source_key: str
    source_status: str
    source_reliability: int | None
    usable: bool
    reasons: list[str]
    signal_text: str


def add_evidence_source(
    session: Session,
    *,
    source_id: str,
    title: str = "",
    url: str = "",
    reliability: int = 0,
    status: str = "candidate",
) -> EvidenceSource:
    existing = session.scalar(select(EvidenceSource).where(EvidenceSource.source_id == source_id))
    if existing:
        existing.title = title or existing.title
        existing.url = url or existing.url
        existing.reliability = reliability
        existing.status = status
        session.flush()
        return existing
    source = EvidenceSource(
        source_id=source_id,
        title=title,
        url=url,
        reliability=reliability,
        status=status,
    )
    session.add(source)
    session.flush()
    return source


def list_evidence_sources(session: Session, *, status: str = "", min_reliability: int = 0) -> list[EvidenceSource]:
    stmt = select(EvidenceSource).where(EvidenceSource.reliability >= min_reliability).order_by(EvidenceSource.id)
    if status:
        stmt = stmt.where(EvidenceSource.status == status)
    return list(session.scalars(stmt))


def get_evidence_source_by_key(session: Session, source_key: str) -> EvidenceSource:
    source = session.scalar(select(EvidenceSource).where(EvidenceSource.source_id == source_key))
    if not source:
        raise ValueError(f"evidence source not found: {source_key}")
    return source


def add_market_signal(
    session: Session,
    *,
    genre: str,
    signal_text: str,
    confidence: int = 0,
    source_key: str = "",
) -> MarketSignal:
    source = get_evidence_source_by_key(session, source_key) if source_key else None
    signal = MarketSignal(
        source_id=source.id if source else None,
        genre=genre,
        signal_text=signal_text,
        confidence=confidence,
    )
    session.add(signal)
    session.flush()
    return signal


def list_market_signals(
    session: Session,
    *,
    genre: str = "",
    usable_only: bool = False,
    min_confidence: int = 0,
) -> list[MarketSignal]:
    stmt = select(MarketSignal).where(MarketSignal.confidence >= min_confidence).order_by(MarketSignal.confidence.desc(), MarketSignal.id)
    if genre:
        stmt = stmt.where(MarketSignal.genre == genre)
    signals = list(session.scalars(stmt))
    if not usable_only:
        return signals
    return [signal for signal in signals if _is_usable_signal(session, signal)]


def usable_market_signals_for_genre(session: Session, *, genre: str, limit: int = 5) -> list[MarketSignal]:
    signals = list_market_signals(session, genre=genre, usable_only=True, min_confidence=60)
    return signals[:limit]


def format_market_evidence_context(session: Session, *, genre: str, limit: int = 5) -> tuple[str, list[int]]:
    signals = usable_market_signals_for_genre(session, genre=genre, limit=limit)
    if not signals:
        return "未登记可用市场证据；不得编造市场结论。", []
    lines: list[str] = []
    ids: list[int] = []
    for signal in signals:
        ids.append(signal.id)
        source_label = "no-source"
        if signal.source_id:
            source = session.get(EvidenceSource, signal.source_id)
            if source:
                source_label = source.source_id
        lines.append(f"- signal#{signal.id} source={source_label} confidence={signal.confidence}: {signal.signal_text}")
    return "\n".join(lines), ids


def audit_market_evidence(
    session: Session,
    *,
    genre: str = "",
    min_confidence: int = 0,
) -> list[EvidenceAuditItem]:
    signals = list_market_signals(session, genre=genre, min_confidence=min_confidence)
    return [_audit_signal(session, signal) for signal in signals]


def _audit_signal(session: Session, signal: MarketSignal) -> EvidenceAuditItem:
    reasons: list[str] = []
    source_key = ""
    source_status = "missing"
    source_reliability: int | None = None

    if signal.confidence < 60:
        reasons.append(f"confidence_below_60:{signal.confidence}")
    if not signal.source_id:
        reasons.append("missing_source")
    else:
        source = session.get(EvidenceSource, signal.source_id)
        if not source:
            reasons.append(f"source_not_found:{signal.source_id}")
        else:
            source_key = source.source_id
            source_status = source.status
            source_reliability = source.reliability
            if source.status != "verified":
                reasons.append(f"source_status_not_verified:{source.status}")
            if source.reliability < 3:
                reasons.append(f"source_reliability_below_3:{source.reliability}")

    return EvidenceAuditItem(
        signal_id=signal.id,
        genre=signal.genre,
        confidence=signal.confidence,
        source_key=source_key,
        source_status=source_status,
        source_reliability=source_reliability,
        usable=not reasons,
        reasons=reasons,
        signal_text=signal.signal_text,
    )


def _is_usable_signal(session: Session, signal: MarketSignal) -> bool:
    if signal.confidence < 60 or not signal.source_id:
        return False
    source = session.get(EvidenceSource, signal.source_id)
    if not source:
        return False
    return source.status == "verified" and source.reliability >= 3
