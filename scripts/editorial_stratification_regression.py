from __future__ import annotations

import json
from datetime import datetime

from app.db.session import session_scope
from app.models.entities import Book, Chapter, ChapterBrief, ChapterVersion, QualityReport
from app.services.editorial_stratification import (
    maybe_apply_editorial_stratification,
    maybe_rollback_failed_elevation,
    stratify_quality_report,
)
from regression_db import isolated_database


def main() -> int:
    isolated_database("editorial-stratification-regression")
    failures: list[str] = []
    with session_scope() as session:
        book = Book(title=f"editorial-stratification-{datetime.utcnow().timestamp()}", genre="网游武侠", target_platform="test")
        session.add(book)
        session.flush()
        chapter = Chapter(book_id=book.id, chapter_number=1, title="第1章", status="draft")
        session.add(chapter)
        session.flush()
        brief = ChapterBrief(
            chapter_id=chapter.id,
            goal="第1章：主角进入江湖，完成首次桥段复刻。",
            required_beats="现实压力；铁剑门入局；桥段复刻；章末钩子。",
            constraints="禁止系统面板直接解题。",
            status="ready",
        )
        session.add(brief)
        version = ChapterVersion(
            chapter_id=chapter.id,
            version_number=1,
            title="第1章",
            content="正文" * 1800,
            status="reviewed_pass",
            source="regression",
        )
        session.add(version)
        session.flush()
        quality = QualityReport(
            chapter_version_id=version.id,
            score=75,
            passed=True,
            report=json.dumps(_solid_draft_report(), ensure_ascii=False),
        )
        session.add(quality)
        session.flush()
        stratification = maybe_apply_editorial_stratification(
            session,
            book_id=book.id,
            chapter_number=1,
            quality=quality,
        )
        latest_brief = session.query(ChapterBrief).filter_by(chapter_id=chapter.id).order_by(ChapterBrief.id.desc()).first()
        if stratification.tier != "B_solid_draft":
            failures.append("solid_draft_not_tier_b")
        if version.status != "needs_revision":
            failures.append("tier_b_not_reopened")
        if not latest_brief or "editorial_elevation_quality#" not in (latest_brief.constraints or ""):
            failures.append("elevation_brief_missing")
        brief_text = "\n".join(
            [
                latest_brief.goal or "",
                latest_brief.required_beats or "",
                latest_brief.constraints or "",
            ]
        ) if latest_brief else ""
        for required_text, failure_name in [
            (f"源版本锁定：v{version.id}", "elevation_source_version_not_locked"),
            ("以源版本正文为底本逐场增强", "elevation_contract_not_source_based"),
            ("不新开一版故事", "elevation_contract_allows_new_story"),
            ("自动回滚到源版本", "elevation_contract_missing_rollback_fuse"),
            ("self_check 必须写明保留了源版本", "elevation_contract_missing_acceptance_check"),
        ]:
            if required_text not in brief_text:
                failures.append(failure_name)
        if "editorial_stratification" not in (quality.report or ""):
            failures.append("quality_report_missing_stratification")
        quality_data = json.loads(quality.report or "{}")
        guidance = quality_data.get("editorial_guidance") if isinstance(quality_data.get("editorial_guidance"), dict) else {}
        if guidance.get("level") != "合格底稿":
            failures.append("quality_report_missing_author_guidance_level")
        if guidance.get("revision_depth") != "targeted_elevation":
            failures.append("quality_report_missing_revision_depth")
        if "不推翻重写" not in guidance.get("decision", ""):
            failures.append("quality_guidance_does_not_protect_solid_draft")
        chief = quality_data.get("editor_in_chief") if isinstance(quality_data.get("editor_in_chief"), dict) else {}
        if chief.get("draft_level") != "合格底稿":
            failures.append("editor_in_chief_missing_draft_level")
        if "保留底稿" not in chief.get("decision", ""):
            failures.append("editor_in_chief_missing_preserve_decision")
        if not chief.get("minimum_effective_revision") or not chief.get("acceptance_checks"):
            failures.append("editor_in_chief_missing_actionable_contract")

        failed_version = ChapterVersion(
            chapter_id=chapter.id,
            version_number=2,
            title="第1章 坏升华稿",
            content="坏稿" * 1800,
            status="needs_revision",
            source="revision:regression",
        )
        session.add(failed_version)
        session.flush()
        failed_quality = QualityReport(
            chapter_version_id=failed_version.id,
            score=45,
            passed=False,
            report=json.dumps(
                {
                    **_failed_elevation_report(),
                    "editorial_stratification": {
                        "tier": "D_rebuild",
                        "label": "废稿/重构稿",
                        "recommended_mode": "structural_rebuild",
                    },
                },
                ensure_ascii=False,
            ),
        )
        session.add(failed_quality)
        session.flush()
        rollback = maybe_rollback_failed_elevation(
            session,
            book_id=book.id,
            chapter_number=1,
            failed_version=failed_version,
            quality=failed_quality,
        )
        if not rollback:
            failures.append("failed_elevation_not_rolled_back")
        elif rollback.status != "reviewed_pass" or "editorial_rollback" not in rollback.source:
            failures.append("rollback_status_or_source_wrong")

        publish = stratify_quality_report(_publish_ready_report())
        if publish.tier != "S_publish_ready" or publish.should_auto_revise:
            failures.append("publish_ready_over_revised")

    print(json.dumps({"status": "pass" if not failures else "fail", "failures": failures}, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


def _solid_draft_report() -> dict:
    return {
        "status": "PASS",
        "score": 75,
        "passed": True,
        "issues": [],
        "warnings": [],
        "dimensions": {
            "brief_coverage": 59,
            "reader_momentum": 58,
            "hook_strength": 63,
            "scene_atmosphere": 38,
            "observation_logic": 26,
            "dialogue_fullness": 55,
            "chapter_unit_flow": 60,
            "payoff_grounding": 64,
            "chapter_necessity": 62,
            "scene_expansion": 82,
            "canon_consistency": 100,
            "author_intent": 95,
            "narrative_logic": 68,
            "anti_ai_flavor": 70,
            "writer_craft": 86,
        },
        "llm_review": {
            "status": "completed",
            "verdict": "pass",
            "score": 88,
            "strengths": ["现实压力清楚", "桥段复刻方向正确", "章末钩子具体"],
            "issues": ["观察逻辑偏跳", "对白偏短"],
            "revision_suggestions": ["补强观察证据链", "让章末压力更强"],
        },
    }


def _publish_ready_report() -> dict:
    dims = {
        "brief_coverage": 82,
        "reader_momentum": 85,
        "hook_strength": 88,
        "scene_atmosphere": 78,
        "observation_logic": 82,
        "dialogue_fullness": 78,
        "chapter_unit_flow": 80,
        "payoff_grounding": 84,
        "chapter_necessity": 86,
        "scene_expansion": 90,
        "canon_consistency": 100,
        "author_intent": 95,
        "narrative_logic": 88,
        "anti_ai_flavor": 86,
        "writer_craft": 90,
    }
    return {
        "status": "PASS",
        "score": 90,
        "passed": True,
        "issues": [],
        "warnings": [],
        "dimensions": dims,
        "llm_review": {"status": "completed", "verdict": "pass", "score": 90, "strengths": ["各项稳定"]},
    }


def _failed_elevation_report() -> dict:
    return {
        "status": "FAIL",
        "score": 45,
        "passed": False,
        "issues": ["brief_coverage_underfulfilled: 40"],
        "warnings": [],
        "dimensions": {
            "brief_coverage": 40,
            "reader_momentum": 45,
            "hook_strength": 50,
            "scene_atmosphere": 39,
            "observation_logic": 42,
            "chapter_unit_flow": 55,
            "canon_consistency": 100,
            "author_intent": 60,
            "narrative_logic": 52,
            "anti_ai_flavor": 60,
            "writer_craft": 58,
        },
        "llm_review": {"status": "completed", "verdict": "needs_revision", "score": 45},
    }


if __name__ == "__main__":
    raise SystemExit(main())
