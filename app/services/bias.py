from __future__ import annotations

from dataclasses import dataclass

from app.services.book_profile import (
    BookProfile,
    infer_book_profile_from_context,
)


LIVING_WUXIA_MARKERS = (
    "真实武侠",
    "真实存在",
    "有血有肉",
    "像穿越",
    "近似穿越",
    "门派",
    "江湖",
    "修炼",
    "拜师",
    "套路触发",
)

MODEL_DRIFT_MARKERS = (
    "打怪升级",
    "刷怪",
    "刷经验",
    "刷副本",
    "经验值",
    "任务大厅",
    "系统任务",
    "任务链",
    "任务 NPC",
    "机械 NPC",
    "NPC发布",
    "击杀奖励",
    "送经验",
    "经验反派",
    "等级提升",
    "属性面板",
    "副本入口",
)

MODEL_DRIFT_LOCAL_REPLACEMENTS = {
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

SYSTEM_CONTEXT_POLLUTION_MARKERS = (
    "依据质检报告",
    "上次质检分数",
    "采纳二审建议",
    "修复质检问题",
)

NEGATION_PREFIXES = (
    "禁止",
    "不是",
    "不靠",
    "不能",
    "不要",
    "不得",
    "没有",
    "避免",
    "而不是",
    "别写成",
    "不要写成",
    "不能写成",
    "不写成",
    "不可写成",
)


@dataclass(frozen=True)
class BiasReport:
    system_bias_hits: list[str]
    model_bias_hits: list[str]
    blockers: list[str]
    warnings: list[str]

    @property
    def passed(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "system_bias_hits": self.system_bias_hits,
            "model_bias_hits": self.model_bias_hits,
            "blockers": self.blockers,
            "warnings": self.warnings,
        }


def evaluate_generation_bias(
    *,
    content: str,
    goal: str = "",
    required_beats: str = "",
    constraints: str = "",
    canon_context: str = "",
    profile: BookProfile | None = None,
) -> BiasReport:
    context = "\n".join([goal or "", required_beats or "", constraints or "", canon_context or ""])
    active_profile = profile or infer_book_profile_from_context(context)
    model_hits = _unsafe_hits(content, active_profile.model_drift_markers or MODEL_DRIFT_MARKERS)
    system_hits = _unsafe_hits(context, SYSTEM_CONTEXT_POLLUTION_MARKERS)
    blockers: list[str] = []
    warnings: list[str] = []
    if active_profile.model_drift_markers and model_hits:
        blockers.append("model_default_drift:" + ",".join(model_hits))
    if system_hits:
        warnings.append("system_context_pollution:" + ",".join(system_hits))
    if active_profile.is_living_wuxia and not _has_any(content, ("江湖", "门派", "人情", "恩怨", "修炼", "代价", "后果", "套路触发")):
        warnings.append("living_world_underexpressed")
    return BiasReport(
        system_bias_hits=system_hits,
        model_bias_hits=model_hits,
        blockers=blockers,
        warnings=warnings,
    )


def build_bias_guard_block(*, constraints: str = "", author_preferences: str = "", story_context: str = "") -> str:
    profile = infer_book_profile_from_context(constraints, author_preferences, story_context)
    return profile.bias_guard_block()


def apply_model_drift_local_patch(
    content: str,
    hits: list[str] | None = None,
    *,
    profile: BookProfile | None = None,
) -> tuple[str, list[dict]]:
    patched = content or ""
    replacements: list[dict] = []
    active_profile = profile or infer_book_profile_from_context(patched)
    replacement_map = active_profile.drift_replacements or MODEL_DRIFT_LOCAL_REPLACEMENTS
    markers = hits or _unsafe_hits(patched, tuple(replacement_map.keys()) or MODEL_DRIFT_MARKERS)
    for marker in markers:
        replacement = replacement_map.get(marker)
        if not replacement or marker not in patched:
            continue
        patched = patched.replace(marker, replacement)
        replacements.append({"from": marker, "to": replacement})
    return patched, replacements


def _has_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in (text or "") for marker in markers)


def _unsafe_hits(text: str, markers: tuple[str, ...]) -> list[str]:
    value = text or ""
    hits: list[str] = []
    for marker in markers:
        start = 0
        unsafe = False
        while True:
            index = value.find(marker, start)
            if index < 0:
                break
            if not _is_negated(value, index):
                unsafe = True
                break
            start = index + len(marker)
        if unsafe:
            hits.append(marker)
    return hits


def _is_negated(text: str, marker_index: int) -> bool:
    prefix = text[max(0, marker_index - 48) : marker_index]
    return any(item in prefix for item in NEGATION_PREFIXES)
