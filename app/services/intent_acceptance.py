from __future__ import annotations

from dataclasses import dataclass

from app.services.bias import evaluate_generation_bias
from app.services.book_profile import infer_book_profile_from_context

LOCAL_REVISION_MODES = {"local_patch", "polish", "targeted"}
REVISION_META_MARKERS = ("修订模式", "修订执行摘要", "反馈调整#", "验收清单", "修订合同")
INTENT_KEYWORDS = (
    "主角",
    "追兵",
    "守洞人",
    "守谷人",
    "老妇",
    "真实武侠",
    "武侠世界",
    "江湖",
    "门派",
    "恩怨",
    "修炼",
    "拜师",
    "交易",
    "冒险",
    "套路触发",
    "套路",
    "观察",
    "交涉",
    "话术",
    "表演",
    "演员",
    "代价",
    "后果",
    "桥段",
    "活人",
    "人情",
    "危险",
    "压力",
    "现实",
    "穿越",
    "痛",
    "怕",
    "血",
    "伤",
    "账册",
    "旧债",
    "线索",
    "遗物",
)
NEGATIVE_FORBIDDEN_TERMS = (
    "打怪升级",
    "刷经验",
    "刷副本",
    "机械任务",
    "机械 NPC",
    "任务 NPC",
    "任务NPC",
    "任务工具人",
    "游戏工具人",
    "系统任务",
    "后台说明",
    "系统说明",
    "作者解释",
    "系统提示",
)
CONCEPT_ALIASES = (
    (("真实武侠", "武侠世界", "活的江湖"), ("江湖", "门派", "规矩", "恩怨", "账册", "轻功")),
    (("近似穿越", "进入游戏", "现实同步"), ("醒来", "疼", "痛", "血", "受伤", "外来者", "现实", "游戏")),
    (("演员", "观察力", "临场表演", "表演", "话术"), ("演员", "群演", "片场", "观察", "判断", "表演", "江湖笑容", "跪")),
    (("观察", "判断"), ("看见", "低头", "盯", "听见", "心里", "判断", "眼角余光")),
    (("交涉", "江湖话术", "话术"), ("前辈", "开口", "问", "说", "笑容", "跪", "送人", "那句话")),
    (("冒险", "破局", "保命"), ("保命", "跪", "兵器", "追兵", "站直", "主动", "开口", "喉前")),
    (("托孤", "遗物", "旧债"), ("托孤", "遗物", "账册", "旧债", "追兵", "油布包", "信物")),
    (("武侠套路", "套路触发器", "论坛热梗", "经典桥段"), ("套路", "桥段", "论坛", "热梗", "经典", "触发")),
    (("试探套路触发器", "试探", "触发器"), ("跪", "前辈", "江湖笑容", "送人", "经典", "桥段", "主动")),
    (("门派恩怨", "门派", "恩怨", "真实动机"), ("门派", "恩怨", "旧债", "账册", "守谷", "追兵", "恨意")),
    (("人情", "人物关系", "新关系"), ("人情", "欠", "债", "账册", "托孤", "老妇", "承诺")),
    (("代价", "后果", "承担"), ("代价", "后果", "痛", "疼", "血", "欠", "三日不能动气", "旧债", "追兵")),
    (("追兵", "危险", "压力", "外部压力"), ("追兵", "脚步", "火把", "危险", "兵器", "喉前", "裂缝", "杀气")),
    (("章末", "钩子", "新机会", "新危险"), ("追兵", "信物", "内谷", "旧债", "黑暗", "更近", "门外", "机会", "危险")),
    (("托孤者", "守洞人", "守谷人"), ("老妇", "守谷人", "守洞人", "老人", "兵器")),
    (("承接压力", "现场压力"), ("门外", "台阶", "搜", "追", "逼", "必须", "刀柄", "铁尺", "差役")),
    (("主动试探", "主角主动试探"), ("故意", "反问", "没有答死", "先问", "试探", "把话递", "问")),
    (("人物互动", "人物关系"), ("问", "说", "答", "掌柜", "弟子", "少年", "船夫", "差役", "老妇")),
    (("可见代价",), ("代价", "欠账", "添下", "名字", "痛", "疼", "伤", "失去", "留下")),
    (("新线索", "章末新线索"), ("浮出", "名字", "梅字", "信物", "账纸", "证人", "旧印", "无灯小船")),
)

# Sprint 2 P1-4 stage-2: URBAN_CONCEPT_ALIASES — map review-language keywords
# to concrete urban-scene tokens so `_point_covered` no longer fails when the
# brief says "外部压力" and the prose actually shows "刘芸站在他工位入口".
# Extends CONCEPT_ALIASES for `is_urban` book profiles (see book_profile.py
# URBAN archetype).  See docs/sprint2/p1_optimization_plan.md for the full
# analysis of the 45-pt intent_underfulfilled root cause.
URBAN_CONCEPT_ALIASES = (
    # Review-language "外部压力/威胁" → 都市场景压力词
    (("外部压力", "压力", "威胁", "危险", "紧张", "被逼", "被迫"),
     ("堵", "逼", "催", "威胁", "警告", "责问", "质问", "冷笑", "冷声", "站在", "拦", "盯着", "拉", "推", "锁上", "关上",
      "邮件", "微信", "短信", "电话", "监控", "刘芸", "老板", "上司", "同事", "领导", "客户", "债主", "警察", "陌生人",
      "催款", "追债", "解雇", "开除", "辞退", "开单", "扣钱", "扣分")),

    # Review-language "主角主动选择/破局" → 都市主动动作
    (("主角", "主动", "破局", "选择", "决定", "行动"),
     ("站起来", "站直", "站稳", "推开", "拿起", "打开", "关掉", "拨通", "点开", "翻开", "翻抽屉", "掏出", "捡起", "放下",
      "决定", "选择", "开口", "主动", "抢先", "先", "反问", "回问", "转身", "走过去", "上前", "拒绝", "同意")),

    # Review-language "核心能力/触发/回报" → 都市异能触发词
    (("核心能力", "能力", "触发", "回报", "收获", "启动", "运转"),
     ("笔记本", "本子", "本册", "字迹", "笔迹", "文字", "预知", "看见", "读到", "感应", "浮现", "翻到", "打开",
      "情绪", "念头", "命运", "未来", "画面", "闪回", "记忆", "碎片", "细节", "线索")),

    # Review-language "代价/承担/后果" → 都市能力代价词
    (("代价", "承担", "后果", "损失", "失去", "牺牲", "付出"),
     ("忘记", "忘掉", "记不起", "想不起", "空白", "抽走", "掉了", "丢失", "少了", "碎裂", "裂痕", "裂缝", "破了",
      "疼", "痛", "刺", "划伤", "血", "伤口", "淤青", "指尖", "指腹", "手指", "手心",
      "透支", "透不过气", "喘", "眩晕", "苍白", "疲惫", "冷汗")),

    # Review-language "章末/局面变化/下一章" → 都市章末动作/悬念
    (("章末", "章尾", "结尾", "下一章", "局面", "变化", "改变", "推进", "转折"),
     ("下一步", "下一个", "接下来", "第二天", "明天", "凌晨", "深夜", "现在", "此刻", "转身", "关门", "锁门", "离开",
      "拉黑", "已读", "未读", "对话框", "屏幕", "消息", "陌生号码", "回拨", "挂断", "陌生地址",
      "追", "跑", "逃", "赶", "冲出", "楼下", "电梯", "门外", "走廊", "楼梯", "路口")),

    # Review-language "命运/身边人/改变" → 都市人际影响
    (("命运", "身边人", "身边的人", "改变", "影响", "关系"),
     ("同事", "朋友", "家人", "母亲", "父亲", "妹妹", "弟弟", "妻子", "丈夫", "女友", "男友", "室友",
      "刘敏", "陈渡", "周远", "赵岩", "赵立诚", "陈末",  # 常见人名兜底（多章会复用）
      "帮", "救", "劝", "拉住", "陪", "陪着", "牵", "接住", "留住", "推开", "赶走", "隔开")),

    # Review-language "冲突/矛盾/张力" → 都市冲突场景词
    (("冲突", "矛盾", "对峙", "对立", "张力", "紧张"),
     ("盯着", "沉默", "对视", "对峙", "顶", "顶回去", "怼", "反驳", "质问", "冷笑", "皱眉", "咬牙",
      "拳头", "攥紧", "攥", "手心出汗", "后退", "上前", "堵", "拦", "推开", "拽")),

    # Review-language "追读/钩子/悬念" → 都市悬念词
    (("追读", "钩子", "悬念", "期待", "好奇"),
     ("到底", "究竟", "为什么", "怎么会", "怎么可能", "陌生", "从没", "第一次", "从来没", "突然", "忽然",
      "陌生号码", "未接来电", "未读消息", "陌生地址", "陌生笔迹", "谁", "什么人", "什么时候")),

    # Review-language "生活细节/画面感/氛围" → 都市感官描写词
    (("画面", "画面感", "氛围", "细节", "场景", "空间"),
     ("阳光", "灯", "灯光", "日光灯", "屏幕", "键盘", "抽屉", "桌面", "工位", "隔板", "地板", "地砖", "窗帘", "窗户",
      "咖啡", "豆浆", "油条", "包子", "水杯", "纸巾", "笔", "手机", "电脑",
      "嗡", "咔", "嘀", "叮", "响", "声音", "脚步声", "键盘声", "呼吸声")),
)


@dataclass(frozen=True)
class IntentAcceptanceReport:
    score: int
    passed: bool
    covered_points: list[str]
    missing_points: list[str]
    blockers: list[str]
    recommendations: list[str]

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "passed": self.passed,
            "covered_points": self.covered_points,
            "missing_points": self.missing_points,
            "blockers": self.blockers,
            "recommendations": self.recommendations,
        }


def evaluate_author_intent(
    *,
    content: str,
    goal: str = "",
    required_beats: str = "",
    constraints: str = "",
    canon_context: str = "",
    author_preferences: str = "",
) -> IntentAcceptanceReport:
    mode_text = "\n".join([goal or "", required_beats or "", constraints or ""])
    revision_mode = _revision_mode(mode_text)
    bias = evaluate_generation_bias(
        content=content,
        goal=goal,
        required_beats=required_beats,
        constraints=constraints + "\n" + author_preferences,
        canon_context=canon_context,
    )
    if revision_mode in LOCAL_REVISION_MODES:
        return _evaluate_local_revision_intent(content=content, revision_mode=revision_mode, bias=bias)
    points = _intent_points(goal, required_beats)
    # Sprint 2 P1-4 stage-2: compute profile once from full brief context
    # (goal + required_beats + constraints + canon) so `_point_covered` can
    # dispatch to URBAN_CONCEPT_ALIASES for urban books.  Per-point inference
    # in `_coverage_units` misclassifies review-language points ("外部压力/
    # 核心能力/章末") as generic because they lack domain keywords.
    profile = infer_book_profile_from_context(
        goal or "",
        required_beats or "",
        constraints or "",
        author_preferences or "",
        canon_context or "",
        content or "",
    )
    if not points:
        blockers = list(bias.blockers)
        return IntentAcceptanceReport(
            score=100 if not blockers else 45,
            passed=not blockers,
            covered_points=["当前 brief 没有独立剧情承诺，不用后台修订术语扣减作者意图分。"],
            missing_points=[],
            blockers=blockers,
            recommendations=["下一份章节 brief 应显式写入“本章剧情承诺”。"],
        )
    covered = [point for point in points if _point_covered(content, point, profile=profile)]
    missing = [point for point in points if point not in covered]
    total = len(points) or 1
    score = round((len(covered) / total) * 100)
    blockers = list(bias.blockers)
    if points and score < 45:
        blockers.append("intent_underfulfilled")
    recommendations: list[str] = []
    if missing:
        recommendations.append("下一版优先补足未兑现的本章目标，不要只修辞句。")
    if bias.model_bias_hits:
        recommendations.append("发现模型默认套路偏差，优先用 local_patch 或 targeted_revision 清除。")
    if score < 65 and not bias.model_bias_hits:
        recommendations.append("正文可读但与 brief 兑现不足，建议重做章节导演单或结构重修。")
    return IntentAcceptanceReport(
        score=score,
        passed=not blockers and score >= 60,
        covered_points=covered[:12],
        missing_points=missing[:12],
        blockers=blockers,
        recommendations=recommendations,
    )


def _intent_points(goal: str, required_beats: str) -> list[str]:
    raw = "\n".join([goal or "", required_beats or ""])
    marked = _marked_story_points(raw)
    if marked:
        return marked[:18]
    pieces = raw.replace("\n", "；").replace("，", "；").replace(",", "；").split("；")
    result: list[str] = []
    diagnostic_markers = (
        "依据质检报告",
        "上次质检分数",
        "质量门禁",
        "修订合同:",
        "原始机器修订建议",
        "意见理解规则",
        "按本次修订要求验收",
        "不扩大修改范围",
        "目标读者体验:",
        "必须满足:",
        "禁止:",
        "验收清单:",
        "正文必须兑现本章写作说明",
        "不要只改标题",
        "修复质检问题",
        "删除系统提示",
        "重新检查连续性",
        "具体处理以最新生产骨架",
        "旧稿已废弃",
        "节奏过快",
        "建议从",
        "建议用",
        "建议让",
        "建议把",
        "建议压缩",
        "审稿建议",
        "上一轮",
        "先判断用户真正不满意",
        "读者体验、人物动机",
        "场景选择、节奏",
        "阅读评估自动修订",
        "reading_assessment_auto_quality#",
        "当前阅读层级",
        "源版本锁定",
        "自动修订预算",
        "system_revision_",
        "换策略修订",
        "恢复底稿",
        "当前版本层级",
        "升华修订",
        "必须保留",
        "本轮只解决",
        "把作者承诺写进",
        "把氛围从概括词",
        "补齐章节 brief",
        "强化本章不可替代",
        "补足可画面化",
        "压缩说明性内心独白",
        "让对白承担",
        "章末钩子要具体",
        "前300字",
        "删除开篇重复",
        "不得换开场",
        "不得新开故事线",
    )
    for piece in pieces:
        text = " ".join(piece.split())
        if len(text) < 4:
            continue
        if text.startswith(("修订模式:", "验收方式：", "验收方式:", "执行修订合同")):
            continue
        if any(marker in text for marker in diagnostic_markers):
            continue
        if "本章按最新" in text and "承接" not in text:
            continue
        if any(marker in text for marker in ("不要把修订说明", "self_check", "质检术语")):
            continue
        if text not in result:
            result.append(text[:120])
    return result[:18]


def _marked_story_points(raw: str) -> list[str]:
    # Sprint 2 P1-4 stage-3: only extract "本章剧情承诺" (per-chapter goals);
    # skip "剧情基线" because 剧情基线 is book-level background/style guide,
    # not per-chapter deliverables.  Prior behaviour dragged background lines
    # into intent_points where they were nearly guaranteed to miss on any
    # given chapter (~10% of 45-pt fires on Ch1-15 stemmed from this alone).
    markers = ("本章剧情承诺:", "本章剧情承诺：")
    points: list[str] = []
    for line in (raw or "").splitlines():
        text = " ".join(line.split())
        marker = next((item for item in markers if item in text), "")
        if not marker:
            continue
        payload = text.split(marker, 1)[1].strip()
        for piece in payload.replace("，", "；").replace(",", "；").split("；"):
            point = piece.strip(" -")
            if len(point) >= 4 and point not in points:
                points.append(point[:120])
    return points


def _point_covered(content: str, point: str, profile=None) -> bool:
    if point in content:
        return True
    if _negative_point_satisfied(content, point):
        return True
    units = _coverage_units(point, profile=profile)
    if not units:
        tokens = [item.strip() for item in point.replace("/", "；").replace("、", "；").split("；") if len(item.strip()) >= 2]
        hits = sum(1 for token in tokens if token in content)
        return hits >= max(1, min(2, len(tokens)))
    hits = sum(1 for aliases in units if any(alias in content for alias in aliases))
    return hits >= _required_unit_hits(len(units))


def _evaluate_local_revision_intent(*, content: str, revision_mode: str, bias) -> IntentAcceptanceReport:
    blockers = list(bias.blockers)
    leaked_terms = [marker for marker in REVISION_META_MARKERS if marker in (content or "")]
    for marker in leaked_terms:
        blockers.append(f"revision_meta_leak:{marker}")
    covered = [f"局部修订模式已识别:{revision_mode}", "局部修订未扩大为整章重写"]
    missing: list[str] = []
    if revision_mode == "polish":
        if _has_paragraph_breaks(content):
            covered.append("正文已按自然段排版")
        else:
            blockers.append("polish_underfulfilled:paragraph_breaks")
            missing.append("补足正文自然分段")
    if not bias.model_bias_hits:
        covered.append("模型默认套路偏差已清除")
    else:
        missing.append("清除正文中的模型默认套路表达")
    if leaked_terms:
        missing.append("清除正文中的修订元信息")
    recommendations = []
    if bias.model_bias_hits:
        recommendations.append("继续使用 local_patch 或 targeted 修订，只替换仍命中的模型默认套路表达。")
    if leaked_terms:
        recommendations.append("删除正文中的修订合同、验收清单或后台术语。")
    if "polish_underfulfilled:paragraph_breaks" in blockers:
        recommendations.append("把大段正文拆成连续自然段，保留原有剧情内容。")
    return IntentAcceptanceReport(
        score=95 if not blockers else 45,
        passed=not blockers,
        covered_points=covered,
        missing_points=missing,
        blockers=blockers,
        recommendations=recommendations,
    )


def _revision_mode(text: str) -> str:
    normalized = (text or "").replace("：", ":")
    marker = "修订模式:"
    if marker not in normalized:
        return ""
    tail = normalized.split(marker, 1)[1].strip()
    value = []
    for ch in tail:
        if ch.isascii() and (ch.isalpha() or ch == "_"):
            value.append(ch)
            continue
        break
    return "".join(value)


def _has_paragraph_breaks(content: str) -> bool:
    paragraphs = [line.strip() for line in (content or "").splitlines() if line.strip()]
    return len(paragraphs) >= 8


def _negative_point_satisfied(content: str, point: str) -> bool:
    negative_markers = ("禁止", "不得", "不能", "不要", "不是", "不靠", "不能靠", "不写", "不出现", "不输出")
    if not any(marker in point for marker in negative_markers):
        return False
    forbidden = [term for term in NEGATIVE_FORBIDDEN_TERMS if term in point]
    if "后台" in point or "说明" in point:
        forbidden.extend(["后台说明", "系统说明", "作者解释", "系统提示"])
    if "任务" in point or "工具人" in point or "NPC" in point:
        forbidden.extend(["机械 NPC", "任务 NPC", "任务NPC", "任务工具人", "游戏工具人", "系统任务", "发任务"])
    if not forbidden:
        return False
    return not any(term and term in content for term in set(forbidden))


def _coverage_units(point: str, profile=None) -> list[tuple[str, ...]]:
    units: list[tuple[str, ...]] = []
    # Sprint 2 P1-4 stage-2: prefer caller-supplied profile (evaluated with
    # full brief context), fall back to per-point inference for callers that
    # don't have context.  Per-point inference can misclassify urban books as
    # generic because review-language points ("外部压力/核心能力/章末") lack
    # domain keywords.
    if profile is None:
        profile = infer_book_profile_from_context(point)
    for marker in profile.core_markers:
        if marker in point:
            units.append((marker,))
    alias_table = URBAN_CONCEPT_ALIASES if profile.is_urban else CONCEPT_ALIASES
    for needles, aliases in alias_table:
        if any(needle in point for needle in needles):
            units.append(tuple(dict.fromkeys(aliases)))
    marker_tokens = [token for token in INTENT_KEYWORDS if token in point]
    for token in marker_tokens:
        if any(token in aliases for aliases in units):
            continue
        units.append((token,))
    return units


def _required_unit_hits(total: int) -> int:
    if total <= 2:
        return total
    if total <= 5:
        return max(2, round(total * 0.5))
    return max(3, round(total * 0.45))
