from __future__ import annotations

import json
from datetime import datetime, timedelta

from app.db.session import session_scope
from app.models.entities import Book, Chapter, GenerationTask
from app.services.chapter_samples import (
    TASK_TYPE_CHAPTER_SAMPLE,
    _sample1_uses_banned_entry,
    _sample_adoption_text,
    _sample_diversity_report,
    _uses_actor_shortcut,
    latest_chapter_samples,
)
from app.services.writer_loop import sample_failure_director
from regression_db import isolated_database


def main() -> int:
    isolated_database("chapter-sample-conflict-regression")
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
        "轻量方向约束",
        "探索轴",
        "开篇保留短期目标、阻碍、身体反应和误判",
        "配角要制造阻碍",
        "信息释放先给可见证据",
        "章末钩子来自本章行动后果",
        "不要求逐字复刻小样原文",
        "避免无必要整章重写",
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

    stale_result = _check_stale_running_sample()
    failures.extend(stale_result["failures"])

    print(
        json.dumps(
            {
                "status": "fail" if failures else "pass",
                "failures": failures,
                "sample_report": report,
                "director": director,
                "stale_running": stale_result,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if failures else 0


def _check_stale_running_sample() -> dict:
    failures: list[str] = []
    with session_scope() as session:
        cleanup_ids: list[int] = []
        book = Book(title=f"stale-sample-regression-{datetime.utcnow().timestamp()}", genre="玄幻", target_platform="test")
        session.add(book)
        session.flush()
        cleanup_ids.append(book.id)
        chapter = Chapter(book_id=book.id, chapter_number=1, title="第一章", status="draft")
        session.add(chapter)
        session.flush()
        completed = GenerationTask(
            book_id=book.id,
            task_type=TASK_TYPE_CHAPTER_SAMPLE,
            status="completed",
            input_json=json.dumps({"chapter_number": 1}, ensure_ascii=False),
            output_json=json.dumps({"samples": [_sample_fixture(1)], "gate_passed": True}, ensure_ascii=False),
            created_at=datetime.utcnow() - timedelta(minutes=30),
        )
        stale = GenerationTask(
            book_id=book.id,
            task_type=TASK_TYPE_CHAPTER_SAMPLE,
            status="running",
            input_json=json.dumps({"chapter_number": 1}, ensure_ascii=False),
            output_json="{}",
            created_at=datetime.utcnow() - timedelta(minutes=20),
        )
        session.add_all([completed, stale])
        session.flush()
        try:
            latest = latest_chapter_samples(session, book_id=book.id, chapter_number=1)
            refreshed = session.get(GenerationTask, stale.id)
            if refreshed.status != "failed":
                failures.append("stale_running_sample_not_marked_failed")
            if latest.get("status") != "failed":
                failures.append("latest_stale_status_not_failed")
            if latest.get("fallback_task_id") != completed.id:
                failures.append("stale_running_fallback_missing")
            if latest.get("error_category") != "stale_running":
                failures.append("stale_running_error_category_missing")
            return {
                "failures": failures,
                "latest_status": latest.get("status"),
                "fallback_task_id": latest.get("fallback_task_id"),
                "stale_status": refreshed.status,
                "error_category": latest.get("error_category"),
            }
        finally:
            for task in (completed, stale):
                session.delete(task)
            session.delete(chapter)
            session.delete(book)


def _sample_fixture(index: int) -> dict:
    return {
        "index": index,
        "title": "可用小样",
        "exploration_axis": "人物处境",
        "experiment_hypothesis": "用现场压力推动选择",
        "direction": "主角在压力下主动选择",
        "opening": "药铺后堂的灯油快尽了，陈默先闻到血腥味，才看见账本边缘那道湿痕。掌柜没有求他救人，只把门闩往下一扣，问他昨夜为什么偏偏从后巷回来。陈默喉咙发紧，手指却先按住账页缺口。他知道自己不能解释来历，只能用眼前能看见的血迹和脚印，换一个不被赶出去的机会。",
        "scene_plan": ["验血迹", "掌柜逼问", "章末发现账页缺口"],
        "difference_from_existing": "从现场压力切入。",
        "anti_ai_flavor_strategy": "用灯油、血痕和门闩承载冲突。",
        "pov_strategy": "先闻到、再看见、最后误判。",
        "precision_strategy": "推断只来自可见物证。",
    }


if __name__ == "__main__":
    raise SystemExit(main())
