from __future__ import annotations

import json

from app.services.skeleton_governance import audit_skeleton_sources, repair_skeleton_draft


def main() -> int:
    current_skeleton = {
        "premise": "陈默是横店龙套演员，靠演技进入真实武侠世界骗取奇遇。",
        "reader_promise": "看陈默靠演员经验和表演让NPC配合他刷奇遇。",
        "world_engine": "《大江湖》是真实武侠世界，但主角可以制造坠崖桥段刷奖励。",
        "protagonist_engine": "陈默靠演技和龙套经验解决所有危机。",
        "conflict_engine": "不断制造桥段骗取资源。",
        "forbidden_rules": "",
        "style_guide": "",
        "volume_summary": "第一卷反复制造坠崖桥段骗取奇遇。",
        "arc_goal": "刷奇遇。",
        "arc_climax": "坠崖奇遇。",
        "arc_turn": "继续刷。",
    }
    current = audit_skeleton_sources({f"current.{key}": value for key, value in current_skeleton.items()})
    clean = audit_skeleton_sources(
        {
            "premise": "陈默进入真实武侠世界，凭现场证据、武侠套路知识和风险选择触发机缘，同时被现实同步危机追上。",
            "reader_promise": "看主角在真实江湖里用判断和代价换主动权，每个桥段都来自人物因果。",
            "world_engine": "世界是真实江湖，本地人有利益和恐惧；触发机缘只能事后识别，不能刷取或让人配合表演。",
            "protagonist_engine": "陈默擅长观察和试探，但会误判；他必须用证据、交易和承担后果推进。",
            "conflict_engine": "长期压力来自门派追查、人情债、身份暴露和现实同步危机的连锁升级。",
            "forbidden_rules": "禁止万能职业经验、刷奇遇、NPC配合、主动制造坠崖；所有收益必须有代价。",
            "style_guide": "正文优先写具体场景、人物立场、感官压力和章末钩子。",
            "volume_summary": "第一卷轮换求医、护送、身份误认、门派追查、资源交换和现实危机。",
            "arc_goal": "前五章建立真实江湖规则、主角误判修正链、第一笔人情债和现实同步隐患。",
            "arc_climax": "门派追查、身份误认、资源交换和护送失物同时压到主角面前。",
            "arc_turn": "陈默发现桥段不是可刷任务，而是会改变人物关系和门派追查的真实因果。",
        }
    )
    current_codes = {issue.code for issue in current.issues}
    failures = []
    if "protagonist_crutch_overdefined" not in current_codes:
        failures.append("current_missing_protagonist_crutch")
    if "world_logic_conflict" not in current_codes:
        failures.append("current_missing_world_logic_conflict")
    if not clean.passed:
        failures.append("clean_skeleton_should_pass")
    rebirth = audit_skeleton_sources(
        {
            "premise": "主角重生回十年前，靠未来记忆轻松抓住所有风口。",
            "reader_promise": "看主角用重生记忆稳赚不赔，直接解决商业危机。",
            "world_engine": "商业世界真实残酷，但主角只要想起前世信息就能碾压竞争者。",
            "protagonist_engine": "主角的未来记忆几乎万能，不会失败。",
            "arc_goal": "前五章反复用重生记忆买股票、囤房、打脸旧敌。",
            "forbidden_rules": "禁止无代价暴富，收益必须有信息误差和现实风险。",
        }
    )
    rebirth_codes = {issue.code for issue in rebirth.issues}
    if "sellpoint_crutch_overdefined:rebirth_memory" not in rebirth_codes:
        failures.append("rebirth_sellpoint_crutch_missing")
    rebirth_skeleton = {
        "premise": "主角重生回十年前，靠未来记忆轻松抓住所有风口。",
        "reader_promise": "看主角用重生记忆稳赚不赔，直接解决商业危机。",
        "world_engine": "商业世界真实残酷，但主角只要想起前世信息就能碾压竞争者。",
        "protagonist_engine": "主角的未来记忆几乎万能，不会失败。",
        "conflict_engine": "竞争对手永远慢一步。",
        "forbidden_rules": "禁止无代价暴富，收益必须有信息误差和现实风险。",
        "style_guide": "",
        "volume_summary": "第一卷反复靠未来记忆投资成功。",
        "arc_goal": "前五章反复用重生记忆买股票、囤房、打脸旧敌。",
        "arc_climax": "靠前世记忆买中最大风口。",
        "arc_turn": "主角再次证明前世记忆不会错。",
    }
    rebirth_repaired = repair_skeleton_draft(rebirth_skeleton, rebirth)
    rebirth_repaired_report = audit_skeleton_sources({f"rebirth_repaired.{key}": value for key, value in rebirth_repaired.items()})
    if not rebirth_repaired_report.passed:
        failures.append("rebirth_repaired_should_pass")
    system = audit_skeleton_sources(
        {
            "premise": "主角绑定签到系统，靠系统任务和奖励一路秒杀。",
            "reader_promise": "系统面板给出最优答案，主角只要签到就能轻松变强。",
            "world_engine": "修仙世界残酷，但系统奖励无代价碾压所有限制。",
            "protagonist_engine": "主角靠系统直接解决修炼、资源和敌人问题。",
            "arc_goal": "前五章每天签到领奖励。",
        }
    )
    if "sellpoint_crutch_overdefined:system_panel" not in {issue.code for issue in system.issues}:
        failures.append("system_sellpoint_crutch_missing")
    dramatic_system = audit_skeleton_sources(
        {
            "premise": "主角获得剧情演绎系统，遇到事件时复刻经典桥段，复刻程度越高奖励越好。",
            "reader_promise": "看主角在桥段复刻中拿奖励，也会因为参演人员好感度下降和失败无奖励承担后果。",
            "world_engine": "剧情演绎系统只在人物因果和场景条件吻合时触发，不能给最优答案，也不能自动解题。",
            "protagonist_engine": "主角必须主动判断桥段是否适合，错误复刻会造成关系错位和收益污染。",
            "conflict_engine": "长期压力来自桥段错账、好感度变化、现实同步副作用和玩家竞争。",
            "forbidden_rules": "禁止签到、抽奖、加点、系统面板最优答案和无代价奖励。",
        }
    )
    if "sellpoint_crutch_overdefined:system_panel" in {issue.code for issue in dramatic_system.issues}:
        failures.append("dramatic_system_misread_as_panel_crutch")
    dramatic_performance = audit_skeleton_sources(
        {
            "form.premise": "主角激活桥段复刻能力，必须在真实NPC因果中自然演出经典武侠桥段。",
            "form.reader_promise": "每次复刻像即兴演出，成功获得武学与同步奖励，失败则搞砸关系、闹出笑话。",
            "form.world_engine": "桥段复刻必须符合场景逻辑，不能凭空触发；NPC会记仇、报恩和传八卦。",
            "form.protagonist_engine": "主角太想复刻桥段，用力过猛会留下表演痕迹，让NPC信任度下降。",
            "form.conflict_engine": "长期压力来自桥段错账、关系复杂化、收益污染和现实同步副作用。",
        }
    )
    if "sellpoint_crutch_overdefined:actor_craft" in {issue.code for issue in dramatic_performance.issues}:
        failures.append("dramatic_performance_misread_as_actor_crutch")
    repaired = repair_skeleton_draft(current_skeleton, current)
    repaired_report = audit_skeleton_sources({f"repaired.{key}": value for key, value in repaired.items()})
    if not repaired_report.passed:
        failures.append("repaired_skeleton_should_pass")
    result = {
        "status": "pass" if not failures else "attention",
        "failures": failures,
        "current": current.to_dict(),
        "clean": clean.to_dict(),
        "rebirth": rebirth.to_dict(),
        "rebirth_repaired": rebirth_repaired_report.to_dict(),
        "system": system.to_dict(),
        "repaired": repaired_report.to_dict(),
        "repaired_skeleton": repaired,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
