from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.chapter_standards import extract_max_chars, extract_min_chars
from app.services.chapter_revision import _primary_revision_mode
from app.services.production_blueprint import build_production_blueprint, classify_quality_failure


def main() -> int:
    failures: list[str] = []
    constraints = "正文字数:3000-4500中文字符；不要输出系统说明；禁止面板直接解题。"
    if extract_min_chars(constraints) != 3000:
        failures.append("min_chars_not_extracted")
    if extract_max_chars(constraints) != 4500:
        failures.append("max_chars_not_extracted")
    if extract_min_chars("", default=3000) != 3000:
        failures.append("review_default_min_not_3000")
    if extract_max_chars("", default=4500) != 4500:
        failures.append("review_default_max_not_4500")
    mixed_mode = "修订模式:targeted\n旧合同残留\nrevision_mode:rewrite"
    if _primary_revision_mode(mixed_mode) != "rewrite":
        failures.append("last_revision_mode_should_win")
    long_required = "；".join(f"必须兑现第{i}个节拍，落到动作和后果" for i in range(1, 25))
    blueprint = build_production_blueprint(
        chapter_number=2,
        mode="revision",
        goal="第2章承接上一章茶棚后果，写林北与赵乾从试探到合作。",
        required_beats=long_required,
        constraints=constraints,
        previous_chapter_context="上一章结尾：林北拿到铁牌，赵乾盯上他，捕快开始封街。" * 20,
        canon_context="林北是主角；赵乾是同行玩家；青字门信物仍有效。" * 20,
        author_preferences="江湖要有烟火气；对白承担试探和交易；不要系统面板直接解题。",
        chapter_unit_plan={"target_unit_count": 8, "units": [{"role": "承接", "goal": "接住后果", "obstacle": "捕快封街", "action": "低声试探", "handoff": "赵乾摊牌"}]},
        book_aesthetic_standard={"narrative_flavor": ["江湖烟火气"], "scene_density": ["每500字局面变化"], "forbidden_tone": ["冷硬装酷"]},
        quality_report='{"score": 73, "issues": ["too_long: 6800 > 4500"], "dimensions": {"chapter_unit_flow": 61}}',
        previous_content="旧稿开头" * 1000,
        rewrite_mode=True,
    )
    if blueprint.target_max_chars != 4500:
        failures.append(f"blueprint_max_chars:{blueprint.target_max_chars}")
    if blueprint.target_unit_count != 6:
        failures.append(f"blueprint_unit_count:{blueprint.target_unit_count}")
    if len(blueprint.required_beats) > 1200:
        failures.append(f"blueprint_required_too_long:{len(blueprint.required_beats)}")
    if len(blueprint.prompt_block) > 5200:
        failures.append(f"blueprint_prompt_too_long:{len(blueprint.prompt_block)}")
    contaminated = build_production_blueprint(
        chapter_number=1,
        mode="revision",
        goal="【自然网文正文约束·最高优先级】: 不要像提纲。",
        required_beats=(
            "剧情基线：他一边在现实里送外卖、照顾病重的父亲、撑着家里濒倒的小武馆，"
            "一边在《入梦》里靠清虚观武学一步步变强——每强一分，现实的担子就更扛不动一分。\n"
            "第1章硬性交付：第一句必须从门外逼问开场，不得以系统菜单开场。\n"
            "章末钩子落在具体细节（一条同步提示、一次现实里不受控的身法、一个人的眼神、一句话）。"
        ),
        constraints="不要输出系统说明；禁止系统面板直接解题。",
        previous_chapter_context="",
        canon_context="顾晚是主角；清虚观山门用木牌盘问身份。",
        author_preferences="",
        chapter_unit_plan={"target_unit_count": 8, "units": []},
        book_aesthetic_standard={},
    )
    contaminated_text = "\n".join([contaminated.prompt_block, contaminated.required_beats])
    for marker in ("剧情基线", "自然网文正文约束", "他一边在现实里", "每强一分", "家里濒倒的小武馆"):
        if marker in contaminated_text:
            failures.append(f"contaminated_blueprint_marker:{marker}")
    if contaminated.goal != "第1章完整成章":
        failures.append(f"contaminated_goal_not_default:{contaminated.goal}")
    if contaminated.target_unit_count != 6:
        failures.append(f"contaminated_unit_count:{contaminated.target_unit_count}")
    failure = classify_quality_failure(
        {
            "chinese_chars": 6898,
            "thresholds": {"max_chars": 4500},
            "issues": ["too_long: 6898 > 4500"],
            "warnings": [],
            "dimensions": {"brief_coverage": 53, "chapter_unit_flow": 61, "dialogue_fullness": 66},
            "chapter_unit_report": {"unit_count": 14, "score": 61},
        }
    )
    if failure.get("category") != "structure_rewrite" or failure.get("recommended_revision_mode") != "rewrite":
        failures.append(f"structural_failure_not_rewrite:{failure}")

    if failures:
        for failure in failures:
            print(failure)
        return 1
    print("production-blueprint-regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
