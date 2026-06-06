from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.config import ROOT_DIR, settings
from app.db.init import current_sqlite_path
from app.services.agent_plan_intelligence import summarize_semantic_memory
from app.services.db_ops import check_schema_version
from app.services.readiness import check_production_readiness


def build_development_status(
    session: Session,
    *,
    book_id: int | None = None,
    start: int = 1,
    count: int = 5,
) -> dict[str, Any]:
    docs = _document_summary()
    schema = check_schema_version(session)
    readiness_payload = None
    semantic_memory = None
    blockers: list[str] = []
    next_spiral: list[str] = []

    if book_id:
        readiness = check_production_readiness(session, book_id=book_id, start=start, count=count, live_llm=False)
        readiness_payload = {
            "passed": readiness.passed,
            "checks": [{"name": item.name, "passed": item.passed, "detail": item.detail} for item in readiness.checks],
        }
        blockers = [f"{item.name}: {item.detail}" for item in readiness.checks if not item.passed]
        try:
            semantic_memory = summarize_semantic_memory(session, book_id=book_id)
        except OperationalError:
            session.rollback()
            semantic_memory = {"ready": False, "indexed_count": 0, "attention": "migration missing"}
        next_spiral = _book_next_spiral(blockers=blockers, semantic_memory=semantic_memory)
    else:
        next_spiral = [
            "Choose a book_id and run development-status --book-id <id> for book-specific readiness.",
            "Keep schema current before adding new Agent Plan or production features.",
        ]

    return {
        "method": "DNA double helix: capability growth paired with stability anchors",
        "documents": docs,
        "capability_strand": _capability_strand(),
        "stability_strand": _stability_strand(schema_status=schema.status),
        "schema": {
            "status": schema.status,
            "current_versions": schema.current_versions,
            "expected_head": schema.expected_head,
            "latest_migration": schema.latest_migration,
            "message": schema.message,
        },
        "database_safety": _database_safety(),
        "agent_plan": {
            "plan": settings.llm_plan,
            "base_url": settings.ark_base_url,
            "language_model": settings.model_name,
            "embedding_model": settings.ark_embedding_model,
            "vision_model": settings.ark_vision_model,
            "image_model": settings.ark_image_model,
            "agent_key_configured": bool(settings.ark_agent_plan_api_key),
            "search_key_configured": bool(settings.ark_search_api_key),
            "position": "enhancement_strand",
        },
        "book_id": book_id,
        "readiness": readiness_payload,
        "semantic_memory": semantic_memory,
        "blockers": blockers,
        "next_spiral": next_spiral,
    }


def _document_summary() -> list[dict[str, str]]:
    entries = [
        ("docs/dna_spiral_development.md", "DNA spiral development rule", "active"),
        ("docs/development_archive.md", "consolidated session-era decisions", "living_archive"),
        ("docs/production_roadmap.md", "author-friendly product roadmap", "active"),
        ("docs/humanized_production.md", "human-style chapter production model", "active"),
        ("docs/root_cause_alignment.md", "direction drift diagnosis", "active"),
    ]
    result: list[dict[str, str]] = []
    for relative_path, role, status in entries:
        path = ROOT_DIR / relative_path
        result.append(
            {
                "path": relative_path,
                "role": role,
                "status": status if path.exists() else "missing",
            }
        )
    archive_dir = ROOT_DIR / "docs" / "archive"
    archive_count = len([path for path in archive_dir.glob("*") if path.is_file()]) if archive_dir.exists() else 0
    result.append({"path": "docs/archive/", "role": f"original old session documents; files={archive_count}", "status": "ready"})
    return result


def _database_safety() -> dict[str, Any]:
    path = current_sqlite_path()
    path_text = str(path or "")
    is_primary = path_text.endswith("/data/novel.db") or path_text.endswith("data/novel.db")
    is_regression = any(marker in path_text for marker in ("regression", "test", "smoke", "dashboard-integration"))
    if is_primary:
        mode = "primary"
        warning = "Use preview/dry-run for development checks; apply only when the user explicitly chooses production data changes."
    elif is_regression:
        mode = "test"
        warning = "Safe for development verification."
    else:
        mode = "custom"
        warning = "Confirm whether this database is disposable before applying scaffold or production changes."
    return {"database_path": path_text, "mode": mode, "warning": warning}


def _capability_strand() -> list[dict[str, str]]:
    return [
        {"area": "main_production", "status": "active", "owner": "production/readiness/planning services"},
        {"area": "author_workbench", "status": "active", "owner": "dashboard and author runner"},
        {"area": "agent_plan_research", "status": "enhancement", "owner": "market research pack and evidence import"},
        {"area": "agent_plan_semantic_memory", "status": "enhancement", "owner": "knowledge embeddings and production packet recall"},
        {"area": "agent_plan_visual_assets", "status": "planned_assets", "owner": "visual asset prompt artifacts"},
    ]


def _stability_strand(*, schema_status: str) -> list[dict[str, str]]:
    return [
        {"area": "schema", "status": schema_status, "anchor": "alembic + migration_regression_test"},
        {"area": "production_gate", "status": "active", "anchor": "production-readiness"},
        {"area": "dashboard_gate", "status": "active", "anchor": "run_local_dashboard.py --self-test"},
        {"area": "dry_run", "status": "active", "anchor": "draft/revise/Agent Plan cycle dry-runs"},
        {"area": "session_memory", "status": "active", "anchor": "docs/development_archive.md"},
    ]


def _book_next_spiral(*, blockers: list[str], semantic_memory: dict[str, Any] | None) -> list[str]:
    if blockers:
        if any(item.startswith("story_bible:") or item.startswith("skeleton_approval:") for item in blockers):
            return [
                "Capability: repair Story Bible and production skeleton for the target book.",
                "Stability: rerun production-readiness and skeleton governance before drafting.",
                "Web/CLI: keep changes available through both dashboard skeleton tools and CLI story commands.",
            ]
        if any(item.startswith("evidence:") for item in blockers):
            return [
                "Capability: import Agent Plan market research into Evidence and MarketSignal.",
                "Stability: audit evidence by genre before it enters production context.",
                "Web/CLI: keep research pack and import paths traceable.",
            ]
        if any(item.startswith("canon:") for item in blockers):
            return [
                "Capability: complete Character, WorldRule, and PowerSystem Canon records.",
                "Stability: rerun production-readiness and story-alignment-audit.",
                "Web/CLI: expose Canon fixes without changing existing chapter state.",
            ]
    if semantic_memory and not semantic_memory.get("ready"):
        return [
            "Capability: rebuild semantic memory for the book.",
            "Stability: use dry-run first, then live embeddings only when cost is intentional.",
        ]
    return [
        "Capability: continue author-mode production for the next chapter.",
        "Stability: run production-readiness, dashboard self-test, and targeted regression after each feature ring.",
    ]
