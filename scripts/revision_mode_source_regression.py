from __future__ import annotations

import json

from app.models.entities import ChapterBrief
from app.services.chapter_revision import _revision_is_local_patch, _revision_requires_rewrite
from app.services.chapter_samples import _sample_adoption_text
from app.services.feedback import build_brief_revision_contract, normalize_revision_mode
from app.services.production_state import brief_is_local_revision
from app.services.prompts import REVISE_CHAPTER_TEMPLATE_V3
from app.services.author_workbench import _revision_mode as author_workbench_revision_mode
from app.services.quality import evaluate_chapter


def main() -> int:
    failures: list[str] = []

    local_contract = build_brief_revision_contract(
        "修订模式:local_patch\n不要整章重写，只修失败单元和问题句。",
        chapter_number=2,
    )
    local_brief = ChapterBrief(
        chapter_id=1,
        goal="局部修订第2章：不要再按小样整章重写，只做轻量修补。",
        required_beats="修订模式:local_patch；按本次修订要求验收，不扩大修改范围",
        constraints=local_contract,
        status="revision_ready",
    )
    if not _revision_is_local_patch(local_brief):
        failures.append("local_patch_not_detected")
    if _revision_requires_rewrite(local_brief):
        failures.append("local_patch_negated_rewrite_triggers_rewrite")
    if not brief_is_local_revision("\n".join([local_brief.goal, local_brief.required_beats, local_brief.constraints])):
        failures.append("production_state_local_patch_negated_rewrite_not_local")
    if "开篇是否在前500字" in local_contract or "章末是否留下具体危险" in local_contract:
        failures.append("local_patch_contract_contains_whole_chapter_checklist")
    if "必须按最小范围处理" not in local_contract:
        failures.append("local_patch_contract_missing_scope_rule")

    targeted_contract = build_brief_revision_contract(
        "修订模式:targeted\n保留可用结构，只重写明确不合格的段落。",
        chapter_number=2,
    )
    targeted_brief = ChapterBrief(
        chapter_id=1,
        goal="定点修订第2章",
        required_beats="修订模式:targeted",
        constraints=targeted_contract,
        status="revision_ready",
    )
    if _revision_requires_rewrite(targeted_brief):
        failures.append("targeted_rewrite_word_triggers_rewrite")
    targeted_with_stale_local = ChapterBrief(
        chapter_id=1,
        goal="定点修订第2章",
        required_beats="修订模式:targeted；按当前修订方向处理",
        constraints="历史合同残留：revision_mode:local_patch；不要整章重写。",
        status="revision_ready",
    )
    if _revision_is_local_patch(targeted_with_stale_local):
        failures.append("stale_local_patch_overrides_primary_targeted")
    conflicting_brief = ChapterBrief(
        chapter_id=1,
        goal="阅读评估重建第2章：旧稿只保留素材。",
        required_beats="reading_assessment_auto_quality#1\n失败结构不得沿用。",
        constraints="revision_mode:targeted\nreading_assessment_contract: 自动评估\nrevision_mode:rewrite",
        status="revision_ready",
    )
    if not _revision_requires_rewrite(conflicting_brief):
        failures.append("conflicting_revision_mode_did_not_escalate_to_rewrite")
    if normalize_revision_mode("unknown-mode") != "targeted":
        failures.append("unknown_revision_mode_not_targeted")
    if "必要时重写开头、重排场景" in REVISE_CHAPTER_TEMPLATE_V3:
        failures.append("targeted_template_expands_to_rewrite")
    if "重写合同与验收标准" in REVISE_CHAPTER_TEMPLATE_V3:
        failures.append("targeted_template_uses_rewrite_contract_label")
    if "只改明确不合格的句段或单元" not in REVISE_CHAPTER_TEMPLATE_V3:
        failures.append("targeted_template_missing_scope_guard")
    if author_workbench_revision_mode("局部修订：不要整章重写，只修问题句。", "") != "local_patch":
        failures.append("author_workbench_local_negated_rewrite_not_local")
    quality = evaluate_chapter(
        "陈默推开门，先看见地上的水痕，又听见屋里有人压低声音。他没有急着解释，只把手里的铜片往袖中一藏。",
        min_chars=20,
        max_chars=500,
        constraints=local_contract,
    )
    if quality.dimensions.get("brief_coverage", 0) < 65:
        failures.append("revision_contract_meta_hurts_brief_coverage")

    heavy_brief = ChapterBrief(
        chapter_id=1,
        goal="按最新生产骨架重启本章",
        required_beats="修订模式:fresh",
        constraints="旧稿已废弃，整章重写。",
        status="revision_ready",
    )
    if not _revision_requires_rewrite(heavy_brief):
        failures.append("fresh_rewrite_not_detected")

    adoption_text = _sample_adoption_text(
        task_id=1,
        sample={
            "index": 1,
            "title": "守夜赌命",
            "exploration_axis": "规则误判",
            "experiment_hypothesis": "主角误判江湖规矩并付出代价",
            "direction": "保留压力逻辑，不复刻原文",
            "opening": "油灯晃了一下，陈默看见老人腮侧那点铜光。" * 30,
            "scene_plan": ["灰袖查夜", "陈默遮掩", "章末选择"],
            "risks": ["不要二选一俗套"],
            "difference_from_existing": "换成守夜压力",
            "pov_strategy": "贴住陈默误判",
            "precision_strategy": "只写可见证据",
            "adoption_note": "保留压力方向",
        },
    )
    if "修订模式:fresh" in adoption_text:
        failures.append("sample_adoption_injects_fresh_mode")
    if "轻量方向约束" not in adoption_text:
        failures.append("sample_adoption_not_lightweight")
    if len(adoption_text) > 1300:
        failures.append("sample_adoption_contract_too_long")

    print(json.dumps({"status": "fail" if failures else "pass", "failures": failures}, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
