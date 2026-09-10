from __future__ import annotations

import json

from app.services.intent_acceptance import evaluate_author_intent
from app.services.prompt_isolation import isolate_generation_inputs
from app.services.quality import _coverage_score, coverage_points_for_brief, evaluate_chapter


def main() -> int:
    text = _chapter_text()
    goal = "第2章：承接第1章结尾压力，写陈默进一步确认《大江湖》不是机械游戏，而是活的江湖。"
    required = "；".join(
        [
            "开场先承接第1章后果、伤势、追兵或梅引压力",
            "沈青梧或托孤者必须有具体恐惧、选择和门派恩怨，不是发任务工具人",
            "陈默要判断这段论坛热梗为何在真实江湖里成立，并为贪这份奇遇付出代价",
            "让奖励、线索或遗物引出新的江湖关系",
            "章末自然推向第3章的追兵、守洞人或主动试探桥段",
            "拟人化小单元修复：当前单元流评分 63，共 13 个单元；修订必须按 300-700 字小单元重建目标、阻碍、动作后果和承接点",
            "第1单元需局部重修：目标不清、动作链弱、阻碍不足、后果没落地；保留本单元有效信息，补清目标、阻碍、动作后果和下一单元承接点。",
            "局部修订闭环：优先修复 imageable_paragraphs=56，不要整章换方向。",
        ]
    )
    constraints = "通用章节生产标准: 正文字数:3000-4500中文字符；主角行动链:目标->阻碍->主动选择->可见代价->结果变化。"
    coverage = _coverage_score(text, coverage_points_for_brief(goal, required, constraints))
    quality = evaluate_chapter(
        text,
        goal=goal,
        required_beats=required,
        constraints=constraints,
        min_chars=3000,
        max_chars=8000,
    )
    report = json.loads(quality.report)
    failures: list[str] = []
    if coverage < 50:
        failures.append(f"coverage_still_blocked:{coverage}")
    if quality.dimensions.get("brief_coverage", 0) < 50:
        failures.append(f"quality_brief_coverage_low:{quality.dimensions.get('brief_coverage')}")
    if any(str(issue).startswith("brief_coverage_underfulfilled") for issue in quality.issues):
        failures.append("brief_coverage_issue_not_filtered")
    book6_text = _book6_style_text()
    book6_required = _book6_style_required()
    book6_points = coverage_points_for_brief("第1章完整成章", book6_required, "")
    book6_coverage = _coverage_score(book6_text, book6_points)
    book6_intent = evaluate_author_intent(content=book6_text, goal="第1章完整成章", required_beats=book6_required)
    joined_points = "\n".join(book6_points)
    for marker in ("剧情基线", "内测", "《列子·汤问》", "形为影之质", "现实的担子"):
        if marker in joined_points:
            failures.append(f"book6_background_leaked_into_coverage:{marker}")
    if book6_coverage < 85:
        failures.append(f"book6_structured_coverage_low:{book6_coverage}")
    if book6_intent.score < 90 or book6_intent.blockers:
        failures.append(f"book6_intent_low:{book6_intent.score}:{book6_intent.blockers}")
    isolation = isolate_generation_inputs(
        goal="阅读评估重建第1章",
        required_beats="\n".join([
            "[LONG_TERM_STATE]",
            "可现在是现实。",
            "热流还在。",
            "他深吸一口气，试着用游戏里的法子，用意念带着那股热流往小臂推。",
            "[/LONG_TERM_STATE]",
            "本章剧情承诺：主角在具体外部压力下主动选择并承担可见代价；章末出现改变下一章局面的具体变化。",
        ]),
        constraints="游戏与现实彻底隔离。禁游戏修为外溢现实。凡人阶段绝不修真。禁掌心发热。禁经脉热流。",
        canon_context="游戏与现实彻底隔离。禁游戏修为外溢现实。",
        strict_authority_fields=True,
    )
    if any(marker in isolation.required_beats for marker in ("[LONG_TERM_STATE]", "可现在是现实", "热流还在", "用意念")):
        failures.append("authority_conflicting_long_term_state_not_removed")
    polluted_quality = evaluate_chapter(
        "沈渡扣紧头盔，因房租压力进入蜀山世界。他在武馆选择留下打杂，磨破手背换来一次站桩机会。"
        "陈松鹤让他明早继续练，章末头盔异常提示下次登录将重启追踪。",
        goal="第1章完整成章",
        required_beats="\n".join([
            "[LONG_TERM_STATE]",
            "可现在是现实。",
            "热流还在。",
            "攥拳的时候，指关节咔咔响了几声。",
            "[/LONG_TERM_STATE]",
            "本章剧情承诺：主角在具体外部压力下主动选择并承担可见代价；章末出现改变下一章局面的具体变化。",
        ]),
        constraints="游戏与现实彻底隔离。禁游戏修为外溢现实。凡人阶段绝不修真。禁掌心发热。禁经脉热流。",
        canon_context="游戏与现实彻底隔离。禁游戏修为外溢现实。",
        min_chars=20,
        max_chars=8000,
    )
    polluted_report = json.loads(polluted_quality.report)
    polluted_intent = polluted_report.get("intent_acceptance") or {}
    if any("热流还在" in point or "指关节咔咔" in point or "LONG_TERM_STATE" in point for point in polluted_intent.get("missing_points", [])):
        failures.append(f"quality_evaluate_chapter_kept_stale_state:{polluted_intent}")
    conflicted_intent = evaluate_author_intent(
        content=(
            "沈渡在出租屋扣上头盔，因房租压力进入蜀山世界；"
            "他选择赌一次，在武馆打杂练拳，代价是手背磨破还欠下一顿工钱；"
            "最后他拿到一枚玉佩线索，决定明天继续追查。"
        ),
        goal="第1章完整成章",
        required_beats="\n".join([
            "热流还在。",
            "攥拳的时候，指关节咔咔响了几声。",
            "本章剧情承诺：主角在具体外部压力下主动选择并承担可见代价；章末出现改变下一章局面的具体变化。",
        ]),
        constraints="游戏与现实彻底隔离。禁游戏修为外溢现实。凡人阶段绝不修真。禁掌心发热。禁经脉热流。",
        canon_context="游戏与现实彻底隔离。禁游戏修为外溢现实。",
    )
    if any("热流还在" in point or "指关节咔咔" in point for point in conflicted_intent.missing_points):
        failures.append(f"authority_conflicting_intent_counted_missing:{conflicted_intent.to_dict()}")
    if conflicted_intent.score < 60 or conflicted_intent.blockers:
        failures.append(f"authority_conflicting_intent_too_low:{conflicted_intent.to_dict()}")
    payload = {
        "status": "fail" if failures else "pass",
        "failures": failures,
        "coverage": coverage,
        "brief_coverage": quality.dimensions.get("brief_coverage"),
        "book6_coverage": book6_coverage,
        "book6_intent": book6_intent.to_dict(),
        "issues": quality.issues,
        "warnings": report.get("warnings", [])[:6],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if failures else 0


def _book6_style_required() -> str:
    return "\n".join([
        "本章剧情承诺：主角在具体外部压力下主动选择并承担可见代价；核心能力必须通过行动触发并产生明确回报；章末出现改变下一章局面的具体变化。",
        "第1章硬性交付①：第一句必须从门外逼问 / 现场盘问 / 交易催促 / 冲突后果 / 人物动作开场；不得以醒来、睁眼、摸手机、宿舍回忆、系统菜单或环境确认开场。",
        "第1章硬性交付②：前700字内必须出现具体外部压力或关系盘问，不得只写醒来、问路和环境确认。",
        "第1章硬性交付③：桥段复刻必须在前1500字内触发，中段完成一次行动尝试，且让清虚观人物或现场旁观者因主角演法产生误判、试探或反应。",
        "第1章硬性交付④：结尾前必须写出明确奖励或能力痕迹；最后300字必须同步出现现实或身体层面的副作用线索。",
        "第1章硬性交付⑤：章末钩子必须来自本次复刻的后果，不得只用远处响动、泛泛麻烦或任务刚触发收尾。",
        "剧情基线：二本大三学生顾晚，为一笔内测奖金进入武侠网游《入梦》，加入落魄的清虚观。每强一分，现实的担子就更扛不动一分。",
        "可复用素材：《列子·汤问》：形为影之质，影为形之用；影身练，真身受；松风十三剑；绵掌。",
    ])


def _book6_style_text() -> str:
    return (
        "拂尘抵到鼻尖前三寸，顾晚后脑勺磕在门框上。老道盯着他腰间木牌，问哪来的，又问谁让他来的。"
        "顾晚决定赌一把，编说山门口有人指路，话到嘴边又不接死，只说清虚观认牌不认人。"
        "老道沉默三息，让他试松风剑法第一式。顾晚握剑先歪了一下，又照着松枝承雪的势子刺出，剑尖抖了三下。"
        "老道问你以前练过，随即说要么是天才，要么是麻烦，最后让他留下，明天卯时练剑。"
        "他食指发麻，眼前同步率跳到0.3%，下一次同步将在子时触发，并会抽取精气。现实里醒来时，指腹还在发麻。"
    )

def _chapter_text() -> str:
    unit = (
        "陈默把铜片压进腰带，掌心被边缘割出一道热辣的血线。院门被雨水撞开，灰袖追兵举着灯笼进来，"
        "光从破瓦缝里扫过草席，照见老人嘴角还没干的黑血。瘦高个问他是哪一支亲眷，陈默先顺着话认错，"
        "又借肺痨遮住老人腮侧的硬块。他知道这不是游戏任务，梅引两个字压在铜片背面，意味着一笔真实旧债。"
        "瘦高个临走前报出柳条巷，逼他在交出铜片保命和赌一次线索之间做选择。陈默选了后者，代价是脚踝扭伤，"
        "还被回风馆记住脸。章末他听见巷口脚步回转，铜片上的梅引二字在雨水里发冷。"
    )
    return "\n".join([unit] * 14)


if __name__ == "__main__":
    raise SystemExit(main())
