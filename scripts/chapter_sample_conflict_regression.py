from __future__ import annotations

import json

from app.services.chapter_samples import (
    _sample1_uses_banned_entry,
    _sample_adoption_text,
    _sample_diversity_report,
    _uses_actor_shortcut,
)
from app.services.writer_loop import sample_failure_director


def main() -> int:
    failures: list[str] = []

    background_only = "陈默想起自己曾在横店跑过龙套，但这点经历没有帮他认出眼前刀伤。他只能蹲下去看血迹方向，又被药铺掌柜一句话逼得改口。"
    shortcut = "陈默靠演技稳住众人，凭龙套经验一眼看穿对方在表演，于是用导演教过的方法解决了盘问。"
    old_entry = "陈默在出租屋戴上头盔刚登录，眼前又闪过横店片场和替身费的账单。"

    if _uses_actor_shortcut(background_only):
        failures.append("background_actor_history_misread_as_shortcut")
    if not _uses_actor_shortcut(shortcut):
        failures.append("actor_shortcut_not_detected")
    if _sample1_uses_banned_entry(background_only):
        failures.append("background_actor_history_misread_as_old_entry")
    if not _sample1_uses_banned_entry(old_entry):
        failures.append("old_reality_login_entry_not_detected")

    samples = [
        {
            "index": 1,
            "title": "药铺血账",
            "exploration_axis": "人物处境",
            "experiment_hypothesis": "用伤者与账本制造压力",
            "direction": "主角先被现实代价压住",
            "opening": background_only * 6,
            "scene_plan": ["验血迹", "被掌柜逼债", "章末发现账本缺页"],
            "difference_from_existing": "不从出租屋登录起步，而从江湖现场压力起步。",
            "anti_ai_flavor_strategy": "用血迹、账本和掌柜反应承载设定。",
            "pov_strategy": "先写主角看错和手心发汗。",
            "precision_strategy": "推断只来自近处可见血迹。",
        },
        {
            "index": 2,
            "title": "渡口错认",
            "exploration_axis": "规则误判",
            "experiment_hypothesis": "用路引规则纠正玩家误判",
            "direction": "主角为一次误判付出代价",
            "opening": "渡口的风把路引吹得贴在木桩上，陈默伸手去捡，船夫却先踩住纸角。他以为对方只是要钱，直到看见差役腰牌后的泥痕，才明白这张纸不是通行证，而是有人故意留给他的套。喉咙发紧时，他没有搬出旧经历，只问纸角为什么是湿的。",
            "scene_plan": ["试探船夫", "发现路引破绽", "章末差役认出假印"],
            "difference_from_existing": "从规则误判切入，不走药铺账本。",
            "anti_ai_flavor_strategy": "用纸角、泥痕和脚步推动。",
            "pov_strategy": "把误判写在手和喉咙反应里。",
            "precision_strategy": "只让角色看见近处纸角与腰牌。",
        },
        {
            "index": 3,
            "title": "灯下人情",
            "exploration_axis": "关系压力",
            "experiment_hypothesis": "用配角私心推动选择",
            "direction": "主角被迫在救人与自保间选择",
            "opening": "后堂灯芯噼啪一响，少年把短刀藏进袖口，先说的却不是求救，而是问陈默能不能替死人作证。陈默闻到药渣里的腥甜，知道这不是普通伤病；可少年眼里的狠劲也不像演出来的。他退半步，背撞上冷墙，才发现门闩已经被人从外面扣住。",
            "scene_plan": ["少年求证", "主角发现门被扣", "章末死人账册露出名字"],
            "difference_from_existing": "从陌生关系压力切入，不复用渡口规则。",
            "anti_ai_flavor_strategy": "让少年先有私心，再有求救。",
            "pov_strategy": "用气味、后退和冷墙贴住视角。",
            "precision_strategy": "门闩与短刀都在可见动作里出现。",
        },
    ]
    report = _sample_diversity_report(samples)
    if "actor_shortcut_reused:samples=1" in report.get("issues", []):
        failures.append("background_actor_history_blocks_sample_group")
    if "sample1_reuses_reality_actor_template" in report.get("issues", []):
        failures.append("background_actor_history_blocks_sample1")
    if report.get("recommended_sample_index") != 1:
        failures.append("recommended_sample_index_missing")
    if 1 not in set(report.get("usable_sample_indices") or []):
        failures.append("usable_sample_indices_missing")
    adoption_text = _sample_adoption_text(task_id=999, sample=samples[0])
    for marker in (
        "必须继承的叙事发动机合同",
        "必须保留探索轴",
        "必须让开篇压力在前500字内落地",
        "必须让关键配角承担功能",
        "必须按小样的信息释放方式推进",
        "必须让章末钩子来自本章行动后果",
        "逐项对应叙事发动机合同",
    ):
        if marker not in adoption_text:
            failures.append(f"adoption_contract_missing:{marker}")

    director = sample_failure_director(
        {
            "score": 42,
            "status": "attention",
            "issues": ["sample1_uses_banned_old_entry", "actor_shortcut_reused:samples=2"],
            "repeated_motifs": ["现实片场", "出租屋登录"],
        },
        chapter_number=1,
    )
    joined_directives = "\n".join(director.get("rewrite_directives") or [])
    if "禁用现实片场" in joined_directives or "不得用横店" in joined_directives:
        failures.append("director_uses_hard_ban_for_setting_terms")

    print(
        json.dumps(
            {
                "status": "fail" if failures else "pass",
                "failures": failures,
                "sample_report": report,
                "director": director,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
