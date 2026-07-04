from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Book, StoryBible, StoryFoundation


WUXIA_CORE_MARKERS = (
    "真实武侠",
    "真实存在",
    "穿越",
    "有血有肉",
    "门派",
    "恩怨",
    "修炼",
    "拜师",
    "交易",
    "冒险",
    "江湖",
    "套路触发",
    "生活逻辑",
)

WUXIA_AVOID_MARKERS = (
    "打怪升级",
    "刷经验",
    "刷副本",
    "经验值",
    "任务 NPC",
    "机械 NPC",
    "系统任务",
    "任务大厅",
    "杀毒软件",
    "觉醒者",
)

WUXIA_DRIFT_REPLACEMENTS = {
    "打怪升级": "靠江湖历练涨本事",
    "刷怪": "找对手试招",
    "刷经验": "捞江湖阅历",
    "刷副本": "闯险地",
    "经验值": "江湖阅历",
    "任务大厅": "消息堂口",
    "系统任务": "江湖差事",
    "任务链": "一串江湖因果",
    "任务 NPC": "江湖中人",
    "机械 NPC": "麻木的江湖过客",
    "NPC发布": "有人递出消息",
    "击杀奖励": "搏命后的收获",
    "送经验": "送上磨刀石",
    "经验反派": "磨刀石式对手",
    "等级提升": "功力精进",
    "属性面板": "自身状态",
    "副本入口": "险地入口",
}

# Sprint 2 P1-4 stage-1: urban archetype (added for book=3 都市 baseline).
# Rationale: prior generic core_markers/CONCEPT_ALIASES could not match urban
# story vocabulary (工位, 邮箱, 抽屉, 数据, 通讯录, 转账, 电梯...) against
# abstract review-language points (外部压力, 核心能力, 代价, 章末...),
# starving `_point_covered` and forcing 45-pt intent_underfulfilled blockers
# on 106/1149 Ch1-15 versions (~9%).  URBAN archetype ships domain-specific
# core markers, drift substitutions, and (paired with URBAN_CONCEPT_ALIASES
# in intent_acceptance.py) an alias table that maps review-language keywords
# to concrete urban-scene tokens.
URBAN_CORE_MARKERS = (
    "都市",
    "现代",
    "职场",
    "办公",
    "工位",
    "同事",
    "老板",
    "客户",
    "手机",
    "微信",
    "地铁",
    "电梯",
    "小区",
    "出租屋",
    "笔记本",
    "邮箱",
    "转账",
    "笔迹",
    "记忆",
    "情绪",
    "预知",
    "命运",
    "选择",
    "代价",
    "承担",
    "身边人",
)

URBAN_AVOID_MARKERS = (
    "打怪升级",
    "刷经验",
    "刷副本",
    "经验值",
    "任务大厅",
    "系统任务",
    "属性面板",
    "副本入口",
    "宗门",
    "门派",
    "修真",
    "灵气",
    "元神",
    "结丹",
    "筑基",
    "御剑",
    "飞升",
)

URBAN_DRIFT_REPLACEMENTS = {
    "打怪升级": "在工作/生活里主动化解一个具体难题",
    "刷经验": "在具体人际或职场情境里积累判断力",
    "刷副本": "闯一个可名可指的现实困局",
    "经验值": "阅历",
    "任务大厅": "任务清单/工位便签",
    "系统任务": "一件具体差事",
    "任务链": "一连串因果动作",
    "任务 NPC": "身边的人",
    "机械 NPC": "面目模糊的过客",
    "属性面板": "自己的状态/清单",
    "副本入口": "陌生场所入口",
    "宗门": "公司/圈子",
    "门派": "小圈子/团伙",
    "修真": "职场磨砺",
    "御剑": "驱车/搭地铁",
    "结丹": "熬过关键节点",
    "筑基": "在职场站稳脚",
    "飞升": "升职/翻身",
}

GENERIC_META_AVOID_MARKERS = (
    "系统提示",
    "作者说明",
    "后台说明",
    "质检报告",
    "修订合同",
    "JSON",
)

PROFILE_KEYWORDS = (
    "重生",
    "系统",
    "面板",
    "推演",
    "模拟",
    "随身空间",
    "末日",
    "修仙",
    "玄幻",
    "都市",
    "悬疑",
    "商战",
    "科幻",
    "异能",
    "江湖",
    "门派",
    "武侠",
    "游戏",
    "玩家",
    "穿越",
    "经营",
    "家族",
    "学院",
    "宗门",
)


@dataclass(frozen=True)
class BookProfile:
    book_id: int
    title: str
    genre: str
    archetype: str
    core_markers: tuple[str, ...]
    avoid_markers: tuple[str, ...]
    model_drift_markers: tuple[str, ...]
    drift_replacements: dict[str, str]
    guard_lines: tuple[str, ...]
    sample_axes: tuple[str, ...]
    sample_banned_terms: tuple[str, ...]

    @property
    def is_living_wuxia(self) -> bool:
        return self.archetype == "living_wuxia"

    @property
    def is_urban(self) -> bool:
        return self.archetype == "urban"

    def bias_guard_block(self) -> str:
        if not self.guard_lines:
            return ""
        return "\n".join(("题材偏差护栏：", *self.guard_lines))

    def sample_prompt_block(self) -> str:
        axes = "；".join(self.sample_axes)
        banned = "、".join(self.sample_banned_terms)
        lines = [
            "本书 Book Profile：",
            f"- 类型识别：{self.archetype}",
            f"- 核心方向词：{'、'.join(self.core_markers[:16])}",
            f"- 小样探索轴：{axes}",
            "- 三个小样必须分配到不同 exploration_axis，不得只是换地点或换人名。",
        ]
        if banned:
            lines.append(f"- 本书小样旧模板警戒词：{banned}")
            lines.append("- 警戒词不是设定硬禁；若属于本书已登记背景，只能作为代价、误读或压力来源，不能当开场捷径或万能解法。")
        if self.guard_lines:
            lines.extend(f"- {line}" for line in self.guard_lines)
        return "\n".join(lines)


def build_book_profile(session: Session, *, book_id: int) -> BookProfile:
    book = session.get(Book, book_id)
    if not book:
        raise ValueError(f"book not found: {book_id}")
    foundation = session.scalar(
        select(StoryFoundation).where(StoryFoundation.book_id == book_id).order_by(StoryFoundation.id.desc())
    )
    bible = session.scalar(select(StoryBible).where(StoryBible.book_id == book_id).order_by(StoryBible.id.desc()))
    context = "\n".join(
        [
            book.title or "",
            book.genre or "",
            foundation.premise if foundation else "",
            foundation.reader_promise if foundation else "",
            foundation.world_engine if foundation else "",
            foundation.protagonist_engine if foundation else "",
            foundation.conflict_engine if foundation else "",
            bible.positioning if bible else "",
            bible.reader_promise if bible else "",
            bible.main_plot if bible else "",
            bible.protagonist_arc if bible else "",
            bible.power_curve if bible else "",
            bible.forbidden_rules if bible else "",
            bible.style_guide if bible else "",
        ]
    )
    return infer_book_profile(book_id=book.id, title=book.title, genre=book.genre, context=context)


def infer_book_profile(*, book_id: int = 0, title: str = "", genre: str = "", context: str = "") -> BookProfile:
    source = "\n".join([title or "", genre or "", context or ""])
    if _looks_like_living_wuxia(source):
        return BookProfile(
            book_id=book_id,
            title=title,
            genre=genre,
            archetype="living_wuxia",
            core_markers=WUXIA_CORE_MARKERS,
            avoid_markers=WUXIA_AVOID_MARKERS,
            model_drift_markers=tuple(dict.fromkeys((*WUXIA_AVOID_MARKERS, *WUXIA_DRIFT_REPLACEMENTS.keys()))),
            drift_replacements=dict(WUXIA_DRIFT_REPLACEMENTS),
            guard_lines=(
                "本书的“游戏世界”必须按真实武侠异世界写，不按网游关卡写。",
                "江湖人物先是活人，其次才可能被玩家误认为 NPC；他们必须有利益、恐惧、恩怨、门派关系和生活逻辑。",
                "主角提升必须来自修炼、拜师、交易、冒险、观察规则和承担后果，不得来自刷怪、刷经验、刷副本、任务大厅或击杀奖励。",
                "套路触发器只能在真实桥段和真实风险后被识别，不能变成系统任务链。",
            ),
            sample_axes=(
                "游戏内具体困境",
                "人物关系压力",
                "规则误判与代价",
                "信息悬疑",
                "道德选择",
            ),
            sample_banned_terms=("横店", "剧组", "演员", "龙套", "片场", "出租屋", "头盔", "内测资格"),
        )
    if _looks_like_urban(source):
        return BookProfile(
            book_id=book_id,
            title=title,
            genre=genre,
            archetype="urban",
            core_markers=URBAN_CORE_MARKERS,
            avoid_markers=URBAN_AVOID_MARKERS,
            model_drift_markers=tuple(dict.fromkeys((*URBAN_AVOID_MARKERS, *URBAN_DRIFT_REPLACEMENTS.keys()))),
            drift_replacements=dict(URBAN_DRIFT_REPLACEMENTS),
            guard_lines=(
                "本书是当代都市题材，全部行动和后果必须落在现代都市场景里（工位、公寓、地铁、商圈、家庭），不得滑向修真/宗门/系统任务/网游关卡。",
                "主角推进故事必须依靠观察、判断、交涉、选择、承担现代都市里的具体后果（丢工作、欠债、失去信任、失去记忆、被排挤），不得靠属性面板或经验值。",
                "章节钩子和代价必须具象为读者可指认的都市细节：邮件、微信消息、转账、监控画面、抽屉里的物件、电梯里的沉默、深夜的电话。",
                "如出现超自然能力（预知/读心/笔记本），必须绑定明确代价（失忆、身体损伤、关系破裂）并逐章递进。",
            ),
            sample_axes=(
                "职场/生活具体困境",
                "人际压力与关系变化",
                "能力触发与代价",
                "信息不对称与悬念",
                "道德选择与承担",
            ),
            sample_banned_terms=("宗门", "门派", "灵气", "结丹", "筑基", "御剑", "飞升", "打怪升级", "系统任务"),
        )
    core = _generic_core_markers(title=title, genre=genre, context=context)
    avoid = _generic_avoid_markers(context)
    return BookProfile(
        book_id=book_id,
        title=title,
        genre=genre,
        archetype="generic",
        core_markers=core,
        avoid_markers=avoid,
        model_drift_markers=avoid,
        drift_replacements={},
        guard_lines=_generic_guard_lines(core, avoid),
        sample_axes=("处境压力", "人物关系", "规则代价", "信息悬疑", "道德选择"),
        sample_banned_terms=tuple(marker for marker in avoid if marker not in GENERIC_META_AVOID_MARKERS),
    )


def infer_book_profile_from_context(*parts: str) -> BookProfile:
    return infer_book_profile(context="\n".join(str(part or "") for part in parts))


def _looks_like_living_wuxia(text: str) -> bool:
    markers = ("真实武侠", "真实江湖", "大江湖", "有血有肉", "套路触发", "江湖", "门派", "拜师", "修炼")
    hits = sum(1 for marker in markers if marker in (text or ""))
    return hits >= 2


def _looks_like_urban(text: str) -> bool:
    """Detect contemporary-urban archetype from title/genre/context.

    Two-signal rule keeps false positives low: (a) require an explicit genre
    token (都市/现代/职场/白领/都会/都市异能/都市修真-negated), OR
    (b) require >=3 concrete urban-scene tokens.
    """
    source = text or ""
    if not source:
        return False
    genre_hits = sum(
        1
        for token in ("都市", "现代都市", "职场", "白领", "现代言情", "都市异能", "都市悬疑", "都市推理")
        if token in source
    )
    scene_tokens = (
        "工位", "同事", "老板", "客户", "地铁", "电梯", "小区", "出租屋", "写字楼",
        "手机", "微信", "邮箱", "邮件", "转账", "银行卡", "外卖", "打工", "公司",
        "上班", "下班", "加班", "职场", "写字楼", "股市", "股票", "房贷", "考研", "创业",
    )
    scene_hits = sum(1 for token in scene_tokens if token in source)
    if genre_hits >= 1:
        return True
    return scene_hits >= 3


def _generic_core_markers(*, title: str, genre: str, context: str) -> tuple[str, ...]:
    values: list[str] = []
    for token in [title, genre]:
        token = str(token or "").strip()
        if 2 <= len(token) <= 12:
            values.append(token)
    source = "\n".join([title or "", genre or "", context or ""])
    values.extend(marker for marker in PROFILE_KEYWORDS if marker in source)
    values.extend(_quoted_terms(source, limit=6))
    result = tuple(dict.fromkeys(item for item in values if item))
    return result or ("主角", "冲突", "代价", "选择", "钩子")


def _generic_avoid_markers(context: str) -> tuple[str, ...]:
    source = context or ""
    values = list(GENERIC_META_AVOID_MARKERS)
    for marker in PROFILE_KEYWORDS:
        if any(prefix + marker in source for prefix in ("禁止", "不要", "不得", "不能", "避免")):
            values.append(marker)
    return tuple(dict.fromkeys(values))


def _generic_guard_lines(core: tuple[str, ...], avoid: tuple[str, ...]) -> tuple[str, ...]:
    lines = [
        "核心卖点只能扩大选择空间，不能替代人物动机、场景行动和后果。",
        "每个收益必须绑定信息差、失败概率、资源消耗、关系变化或明确代价。",
    ]
    meaningful_avoid = [item for item in avoid if item not in GENERIC_META_AVOID_MARKERS]
    if meaningful_avoid:
        lines.append("禁区词只在否定/约束语境出现，不能滑成正文默认套路：" + "、".join(meaningful_avoid[:8]) + "。")
    if core:
        lines.append("章节 brief 和正文至少显式承接部分核心方向词：" + "、".join(core[:8]) + "。")
    return tuple(lines)


def _quoted_terms(text: str, *, limit: int) -> list[str]:
    result: list[str] = []
    for left, right in (("《", "》"), ("“", "”"), ("「", "」")):
        start = 0
        while len(result) < limit:
            begin = text.find(left, start)
            if begin < 0:
                break
            end = text.find(right, begin + 1)
            if end < 0:
                break
            value = text[begin + 1 : end].strip()
            if 2 <= len(value) <= 12:
                result.append(value)
            start = end + 1
    return result
