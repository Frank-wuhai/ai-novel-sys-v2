from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import select

from app.db.session import session_scope
from app.models.entities import Book, Chapter, ChapterVersion, QualityReport
from app.services.anti_ai_flavor import evaluate_anti_ai_flavor
from app.services.bias import evaluate_generation_bias
from app.services.canon import format_canon_context
from app.services.design_quality import evaluate_design_quality
from app.services.expression_precision import evaluate_expression_precision
from app.services.feedback import format_author_preference_context
from app.services.humanized_quality import evaluate_humanized_delivery
from app.services.intent_acceptance import evaluate_author_intent
from app.services.production_state import latest_story_brief
from app.services.quality import chinese_chars
from app.services.prose_voice import evaluate_prose_voice
from app.services.readability import evaluate_readability
from app.services.writer_craft import evaluate_writer_craft


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect chapter quality regression cases without calling live models.")
    parser.add_argument("--cases", default=str(ROOT / "evals" / "quality_cases.json"))
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = parser.parse_args()

    cases = json.loads(Path(args.cases).read_text(encoding="utf-8")).get("cases", [])
    results = []
    with session_scope() as session:
        for case in cases:
            book = _find_book(session, str(case.get("book_title_contains") or ""))
            if not book:
                results.append({"case": case.get("name"), "status": "missing_book"})
                continue
            start = int(case.get("start") or 1)
            count = int(case.get("count") or 1)
            expect = case.get("expect") or {}
            rows = []
            for number in range(start, start + count):
                rows.append(_inspect_chapter(session, book=book, chapter_number=number, expect=expect))
            case_status = "pass" if all(row["status"] == "pass" for row in rows) else "attention"
            results.append({"case": case.get("name"), "book_id": book.id, "book_title": book.title, "status": case_status, "chapters": rows})

    if args.json:
        print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
    else:
        for result in results:
            print(f"CASE {result.get('case')} status={result.get('status')} book={result.get('book_title', '')}")
            for row in result.get("chapters", []):
                print(
                    "  "
                    f"ch={row['chapter_number']} status={row['status']} version={row.get('version_id') or ''} "
                    f"version_status={row.get('version_status') or ''} chars={row.get('chars') or 0} "
                    f"quality={row.get('quality_passed')} score={row.get('quality_score')} "
                    f"humanized={row.get('humanized_score')} "
                    f"design={row.get('design_score')} "
                    f"action={row.get('next_action', '')} note={row.get('note', '')}"
                )
    return 0


def _find_book(session, title_contains: str) -> Book | None:
    stmt = select(Book).order_by(Book.id.desc())
    if title_contains:
        stmt = stmt.where(Book.title.like(f"%{title_contains}%"))
    return session.scalar(stmt)


def _inspect_chapter(session, *, book: Book, chapter_number: int, expect: dict) -> dict:
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book.id, Chapter.chapter_number == chapter_number))
    if not chapter:
        return {
            "chapter_number": chapter_number,
            "status": "fail",
            "note": "missing chapter",
            "next_action": "create_chapter_brief_or_plan",
            "recommendation": "先补章节计划或 brief，再进入生成队列。",
        }
    version = session.scalar(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter.id).order_by(ChapterVersion.id.desc()))
    if not version:
        allow_missing = bool(expect.get("allow_missing", False))
        notes = ["missing version"]
        return {
            "chapter_number": chapter_number,
            "status": "pass" if allow_missing else "fail",
            "note": ",".join(notes),
            "next_action": "enqueue_draft",
            "recommendation": "已有章节但没有正文版本，下一步应生成草稿。",
        }
    quality = session.scalar(select(QualityReport).where(QualityReport.chapter_version_id == version.id).order_by(QualityReport.id.desc()))
    chars = chinese_chars(version.content)
    min_chars = int(expect.get("min_chars") or 0)
    notes = []
    if min_chars and chars < min_chars:
        notes.append(f"short:{chars}<{min_chars}")
    if version.status not in {"reviewed_pass", "approved"}:
        notes.append(f"not_ready:{version.status}")
    if quality and not quality.passed:
        notes.append(f"quality_failed:{quality.score}")
    if not quality:
        notes.append("missing_quality")
    brief = latest_story_brief(session, chapter.id)
    canon_context, _ = format_canon_context(session, book_id=book.id, chapter_number=chapter_number)
    author_preferences = format_author_preference_context(session, book_id=book.id)
    readability = evaluate_readability(version.content or "").to_dict()
    design_report = evaluate_design_quality(version.content or "", canon_context=canon_context)
    prose_voice_report = evaluate_prose_voice(version.content or "")
    expression_precision_report = evaluate_expression_precision(version.content or "")
    humanized_report = evaluate_humanized_delivery(version.content or "")
    humanized = humanized_report.to_dict()
    design = design_report.to_dict()
    prose_voice = prose_voice_report.to_dict()
    expression_precision = expression_precision_report.to_dict()
    anti_ai = evaluate_anti_ai_flavor(
        design=design_report,
        prose_voice=prose_voice_report,
        humanized=humanized_report,
    ).to_dict()
    writer_craft = evaluate_writer_craft(version.content or "")
    if brief:
        intent = evaluate_author_intent(
            content=version.content or "",
            goal=brief.goal or "",
            required_beats=brief.required_beats or "",
            constraints=brief.constraints or "",
            canon_context=canon_context,
            author_preferences=author_preferences,
        ).to_dict()
    else:
        intent = {"score": 0, "passed": False, "covered_points": [], "missing_points": ["missing_brief"], "blockers": ["missing_brief"]}
    bias = evaluate_generation_bias(
        content=version.content or "",
        goal=brief.goal if brief else "",
        required_beats=brief.required_beats if brief else "",
        constraints=brief.constraints if brief else "",
        canon_context=canon_context,
    )
    if bias.model_bias_hits:
        notes.append("model_bias:" + ",".join(bias.model_bias_hits))
    min_readability = int(expect.get("min_readability") or 0)
    if min_readability and int(readability.get("score") or 0) < min_readability:
        notes.append(f"readability_low:{readability.get('score')}<{min_readability}")
    min_intent = int(expect.get("min_intent") or 0)
    if min_intent and int(intent.get("score") or 0) < min_intent:
        notes.append(f"intent_low:{intent.get('score')}<{min_intent}")
    min_humanized = int(expect.get("min_humanized") or 0)
    if min_humanized and int(humanized.get("score") or 0) < min_humanized:
        notes.append(f"humanized_low:{humanized.get('score')}<{min_humanized}")
    min_design = int(expect.get("min_design") or 0)
    if min_design and int(design.get("score") or 0) < min_design:
        notes.append(f"design_low:{design.get('score')}<{min_design}")
    min_visual = int(expect.get("min_visual_staging") or 0)
    visual_score = int((design.get("checks") or {}).get("visual_staging") or 0) if isinstance(design.get("checks"), dict) else 0
    if min_visual and visual_score < min_visual:
        notes.append(f"visual_low:{visual_score}<{min_visual}")
    min_imageable = int(expect.get("min_imageable_paragraphs") or 0)
    imageable_score = int((design.get("checks") or {}).get("imageable_paragraphs") or 0) if isinstance(design.get("checks"), dict) else 0
    if min_imageable and imageable_score < min_imageable:
        notes.append(f"imageable_low:{imageable_score}<{min_imageable}")
    min_nomenclature = int(expect.get("min_designed_nomenclature") or 0)
    nomenclature_score = int((design.get("checks") or {}).get("designed_nomenclature") or 0) if isinstance(design.get("checks"), dict) else 0
    if min_nomenclature and nomenclature_score < min_nomenclature:
        notes.append(f"nomenclature_low:{nomenclature_score}<{min_nomenclature}")
    voice_checks = prose_voice.get("checks") if isinstance(prose_voice.get("checks"), dict) else {}
    prose_voice_score = int(prose_voice.get("score") or 0)
    native_flow_score = int(voice_checks.get("native_chinese_flow") or 0)
    dialogue_fullness_score = int(voice_checks.get("dialogue_fullness") or 0)
    character_voice_score = int(voice_checks.get("character_voice") or 0)
    min_prose_voice = int(expect.get("min_prose_voice") or 0)
    if min_prose_voice and prose_voice_score < min_prose_voice:
        notes.append(f"prose_voice_low:{prose_voice_score}<{min_prose_voice}")
    min_native_flow = int(expect.get("min_native_chinese_flow") or 0)
    if min_native_flow and native_flow_score < min_native_flow:
        notes.append(f"native_flow_low:{native_flow_score}<{min_native_flow}")
    min_dialogue = int(expect.get("min_dialogue_fullness") or 0)
    if min_dialogue and dialogue_fullness_score < min_dialogue:
        notes.append(f"dialogue_low:{dialogue_fullness_score}<{min_dialogue}")
    min_character_voice = int(expect.get("min_character_voice") or 0)
    if min_character_voice and character_voice_score < min_character_voice:
        notes.append(f"character_voice_low:{character_voice_score}<{min_character_voice}")
    expression_precision_score = int(expression_precision.get("score") or 0)
    min_expression_precision = int(expect.get("min_expression_precision") or 0)
    if min_expression_precision and expression_precision_score < min_expression_precision:
        notes.append(f"expression_precision_low:{expression_precision_score}<{min_expression_precision}")
    anti_ai_score = int(anti_ai.get("score") or 0)
    min_anti_ai = int(expect.get("min_anti_ai_flavor") or 0)
    if min_anti_ai and anti_ai_score < min_anti_ai:
        notes.append(f"anti_ai_flavor_low:{anti_ai_score}<{min_anti_ai}")
    writer_craft_score = int(writer_craft.get("score") or 0)
    writer_craft_checks = writer_craft.get("checks") if isinstance(writer_craft.get("checks"), dict) else {}
    embodied_pov_score = int(writer_craft_checks.get("embodied_pov") or 0)
    min_writer_craft = int(expect.get("min_writer_craft") or 0)
    if min_writer_craft and writer_craft_score < min_writer_craft:
        notes.append(f"writer_craft_low:{writer_craft_score}<{min_writer_craft}")
    min_embodied_pov = int(expect.get("min_embodied_pov") or 0)
    if min_embodied_pov and embodied_pov_score < min_embodied_pov:
        notes.append(f"embodied_pov_low:{embodied_pov_score}<{min_embodied_pov}")
    forbidden = [str(item) for item in expect.get("forbid_terms", [])]
    forbidden_hits = [term for term in forbidden if term in (version.content or "")]
    if forbidden_hits:
        notes.append("forbidden_terms:" + ",".join(forbidden_hits))
    next_action, recommendation = _next_action(
        notes=notes,
        version_status=version.status,
        quality_passed=quality.passed if quality else None,
    )
    return {
        "chapter_number": chapter_number,
        "status": "pass" if not notes else "attention",
        "version_id": version.id,
        "version_status": version.status,
        "chars": chars,
        "quality_report_id": quality.id if quality else None,
        "quality_passed": quality.passed if quality else None,
        "quality_score": quality.score if quality else None,
        "evaluation_brief_id": brief.id if brief else None,
        "evaluation_brief_status": brief.status if brief else None,
        "readability_score": readability.get("score"),
        "humanized_score": humanized.get("score"),
        "humanized_issues": humanized.get("issues", []),
        "design_score": design.get("score"),
        "visual_staging_score": visual_score,
        "imageable_paragraphs_score": imageable_score,
        "designed_nomenclature_score": nomenclature_score,
        "design_issues": design.get("issues", []),
        "new_terms": design.get("new_terms", []),
        "ungrounded_terms": design.get("ungrounded_terms", []),
        "prose_voice_score": prose_voice_score,
        "native_chinese_flow_score": native_flow_score,
        "dialogue_fullness_score": dialogue_fullness_score,
        "character_voice_score": character_voice_score,
        "prose_voice_issues": prose_voice.get("issues", []),
        "terse_dialogue_examples": prose_voice.get("terse_dialogue_examples", []),
        "translationese_hits": prose_voice.get("translationese_hits", []),
        "expression_precision_score": expression_precision_score,
        "expression_precision_checks": expression_precision.get("checks", {}),
        "expression_precision_issues": expression_precision.get("issues", []),
        "expression_precision_examples": expression_precision.get("examples", []),
        "anti_ai_flavor_score": anti_ai_score,
        "anti_ai_flavor_issues": anti_ai.get("issues", []),
        "writer_craft_score": writer_craft_score,
        "embodied_pov_score": embodied_pov_score,
        "writer_craft_checks": writer_craft_checks,
        "writer_craft_issues": writer_craft.get("issues", []),
        "intent_score": intent.get("score"),
        "model_bias_hits": bias.model_bias_hits,
        "next_action": next_action,
        "recommendation": recommendation,
        "note": ",".join(notes),
    }


def _loads_json(value: str) -> dict:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _next_action(*, notes: list[str], version_status: str, quality_passed: bool | None) -> tuple[str, str]:
    if not notes:
        if version_status == "reviewed_pass":
            return "human_approve", "质检已过，下一步交给作者阅读并审批。"
        if version_status == "approved":
            return "ready", "章节已通过并审批，无需处理。"
        return "inspect_status", "没有质量问题，但版本状态不在预期范围，建议检查状态机。"
    if "missing_quality" in notes:
        return "review_chapter", "缺少质量报告，先运行 review-chapter 生成门禁结果。"
    if any(note.startswith("quality_failed") for note in notes):
        return "create_revision_brief", "质量门禁未通过，先生成修订 brief，再按模式修订或重写。"
    if any(
        note.startswith(
            (
                "forbidden_terms",
                "model_bias",
                "short",
                "readability_low",
                "intent_low",
                "humanized_low",
                "design_low",
                "visual_low",
                "imageable_low",
                "nomenclature_low",
                "prose_voice_low",
                "native_flow_low",
                "dialogue_low",
                "character_voice_low",
                "anti_ai_flavor_low",
            )
        )
        for note in notes
    ):
        return "revise_chapter", "内容未达样本期望，建议按失败项定点修订；若方向偏离明显则整章重写。"
    if any(note.startswith("not_ready") for note in notes):
        if quality_passed:
            return "review_chapter_or_status_sync", "质量已过但版本状态未同步，复跑 review-chapter 或检查是否需要人工审批。"
        return "revise_chapter", "版本仍处于待修订状态，继续修订后复检。"
    return "inspect_manually", "存在未分类异常，建议人工查看章节版本、brief 和质量报告。"


if __name__ == "__main__":
    raise SystemExit(main())
