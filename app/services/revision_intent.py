from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Chapter, ChapterBrief, ChapterVersion, QualityReport


REVISION_MODE_POLISH = "polish"
REVISION_MODE_LOCAL_PATCH = "local_patch"
REVISION_MODE_TARGETED = "targeted"
REVISION_MODE_REWRITE = "rewrite"
REVISION_MODE_FRESH = "fresh"
REVISION_MODES = {
    REVISION_MODE_POLISH,
    REVISION_MODE_LOCAL_PATCH,
    REVISION_MODE_TARGETED,
    REVISION_MODE_REWRITE,
    REVISION_MODE_FRESH,
}


@dataclass(frozen=True)
class RevisionIntentDecision:
    mode: str
    confidence: int
    reason: str
    preserve: list[str]
    replace: list[str]
    escalation_rule: str
    signals: list[str]

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "confidence": self.confidence,
            "reason": self.reason,
            "preserve": self.preserve,
            "replace": self.replace,
            "escalation_rule": self.escalation_rule,
            "signals": self.signals,
        }

    def contract_prefix(self) -> str:
        return "\n".join(
            [
                f"修订模式:{self.mode}",
                "系统修订判定:",
                f"- 处理强度:{self.mode}",
                f"- 置信度:{self.confidence}",
                f"- 判定理由:{self.reason}",
                f"- 保留:{'；'.join(self.preserve) if self.preserve else '以最新骨架为准'}",
                f"- 替换:{'；'.join(self.replace) if self.replace else '按人工意见命中范围处理'}",
                f"- 升级规则:{self.escalation_rule}",
                "原始意见:",
            ]
        )


def decide_revision_intent(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    suggestion_text: str,
    requested_mode: str = "",
) -> RevisionIntentDecision:
    explicit = normalize_revision_mode(requested_mode)
    if explicit:
        return _explicit_decision(explicit, suggestion_text=suggestion_text)

    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    latest_brief = _latest_brief(session, chapter_id=chapter.id) if chapter else None
    latest_version = _latest_version(session, chapter_id=chapter.id) if chapter else None
    quality_report = _latest_quality_report(session, version_id=latest_version.id) if latest_version else None
    quality_data = _quality_data(quality_report)

    text = _normalize_text(suggestion_text)
    signals: list[str] = []
    candidates: list[str] = []

    if _has_any(text, ("不要参考旧稿", "旧稿废弃", "完全偏了", "方向完全不对", "重启本章", "重新来过")):
        candidates.append(REVISION_MODE_FRESH)
        signals.append("explicit_fresh_intent")
    elif _has_any(text, ("整章重写", "重写这一章", "这一章重写", "结构性重写", "大改")):
        candidates.append(REVISION_MODE_REWRITE)
        signals.append("explicit_rewrite_intent")

    if _has_any(text, ("这句话", "这一句", "某句", "这个词", "错字", "名字", "称呼", "标点", "删掉这", "替换这")):
        candidates.append(REVISION_MODE_LOCAL_PATCH)
        signals.append("localized_language_target")
    if _has_any(text, ("这一段", "这段", "局部", "只改", "不要整章", "别整章")):
        candidates.append(REVISION_MODE_LOCAL_PATCH)
        signals.append("localized_scope_request")
    if _has_any(text, ("文风", "太ai", "ai味", "啰嗦", "润色", "表达", "对白僵", "不自然", "翻译腔", "段落")):
        candidates.append(REVISION_MODE_POLISH)
        signals.append("style_or_prose_request")
    if _has_any(text, ("主角不主动", "钩子", "章末", "承接", "爽点", "行动链", "代价", "选择", "场景", "节奏")):
        candidates.append(REVISION_MODE_TARGETED)
        signals.append("scene_or_reader_experience_request")
    if _has_any(text, ("不像我想要", "读者体验不对", "没写出感觉", "整体不对", "整章方向")):
        candidates.append(REVISION_MODE_REWRITE)
        signals.append("chapter_level_reader_experience_request")

    quality_mode = _mode_from_quality(quality_data)
    if quality_mode:
        candidates.append(quality_mode)
        signals.append(f"quality_requires_{quality_mode}")

    if latest_version and latest_version.status == "approved":
        candidates.append(REVISION_MODE_LOCAL_PATCH)
        signals.append("approved_version_prefers_minimal_change")

    if _brief_contains_fresh(latest_brief):
        candidates.append(REVISION_MODE_FRESH)
        signals.append("latest_brief_requires_fresh")

    if not candidates:
        candidates.append(REVISION_MODE_TARGETED)
        signals.append("default_author_feedback_targeted")

    mode = _select_mode(candidates=candidates, signals=signals)
    confidence = _confidence(mode=mode, signals=signals, quality_data=quality_data, latest_version=latest_version)
    reason = _reason(mode=mode, signals=signals)
    preserve = _preserve_items(mode=mode, latest_version=latest_version)
    replace = _replace_items(mode=mode, text=text, quality_data=quality_data)
    escalation = _escalation_rule(mode)
    return RevisionIntentDecision(
        mode=mode,
        confidence=confidence,
        reason=reason,
        preserve=preserve,
        replace=replace,
        escalation_rule=escalation,
        signals=signals,
    )


def normalize_revision_mode(mode: str) -> str:
    value = (mode or "").strip().lower()
    aliases = {
        "auto": "",
        "": "",
        "minor": REVISION_MODE_POLISH,
        "light": REVISION_MODE_POLISH,
        "partial": REVISION_MODE_TARGETED,
        "local": REVISION_MODE_TARGETED,
        "patch": REVISION_MODE_LOCAL_PATCH,
        "minimal": REVISION_MODE_LOCAL_PATCH,
        "minimal_patch": REVISION_MODE_LOCAL_PATCH,
        "target": REVISION_MODE_TARGETED,
        "targeted_revision": REVISION_MODE_TARGETED,
        "rebuild": REVISION_MODE_REWRITE,
        "structural": REVISION_MODE_REWRITE,
        "restart": REVISION_MODE_FRESH,
        "fresh_rewrite": REVISION_MODE_FRESH,
        "latest_skeleton": REVISION_MODE_FRESH,
    }
    value = aliases.get(value, value)
    return value if value in REVISION_MODES else ""


def extract_revision_decision(text: str) -> dict:
    lines = (text or "").splitlines()
    if "系统修订判定:" not in lines:
        return {}
    result: dict[str, str | list[str]] = {}
    for line in lines[lines.index("系统修订判定:") + 1 :]:
        stripped = line.strip()
        if stripped == "原始意见:":
            break
        if not stripped.startswith("- ") or ":" not in stripped:
            continue
        key, value = stripped[2:].split(":", 1)
        if key in {"保留", "替换"}:
            result[key] = [item for item in value.split("；") if item]
        else:
            result[key] = value
    return result


def _explicit_decision(mode: str, *, suggestion_text: str) -> RevisionIntentDecision:
    return RevisionIntentDecision(
        mode=mode,
        confidence=100,
        reason="作者或上游流程已显式指定处理强度。",
        preserve=_preserve_items(mode=mode, latest_version=None),
        replace=_replace_items(mode=mode, text=_normalize_text(suggestion_text), quality_data={}),
        escalation_rule=_escalation_rule(mode),
        signals=["explicit_revision_mode"],
    )


def _latest_brief(session: Session, *, chapter_id: int) -> ChapterBrief | None:
    return session.scalar(select(ChapterBrief).where(ChapterBrief.chapter_id == chapter_id).order_by(ChapterBrief.id.desc()))


def _latest_version(session: Session, *, chapter_id: int) -> ChapterVersion | None:
    return session.scalar(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter_id).order_by(ChapterVersion.id.desc()))


def _latest_quality_report(session: Session, *, version_id: int) -> QualityReport | None:
    return session.scalar(select(QualityReport).where(QualityReport.chapter_version_id == version_id).order_by(QualityReport.id.desc()))


def _quality_data(report: QualityReport | None) -> dict:
    if not report or not report.report:
        return {}
    try:
        data = json.loads(report.report)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _mode_from_quality(data: dict) -> str:
    if not data:
        return ""
    dimensions = data.get("dimensions") if isinstance(data.get("dimensions"), dict) else {}
    issues = [str(item) for item in data.get("issues", []) if item]
    hard_gate = data.get("hard_gate") if isinstance(data.get("hard_gate"), dict) else {}
    intent_score = int(dimensions.get("author_intent") or 0)
    brief_score = int(dimensions.get("brief_coverage") or 0)
    hook_score = int(dimensions.get("hook_strength") or 0)
    choice_score = int(dimensions.get("choice_and_cost") or 0)
    production_score = int(dimensions.get("production_standard") or 0)

    if any(issue.startswith(("setting_contradiction", "bias_blocker")) for issue in issues):
        return REVISION_MODE_FRESH
    if hard_gate.get("status") == "FAIL" and production_score < 50:
        return REVISION_MODE_REWRITE
    if intent_score and intent_score < 45:
        return REVISION_MODE_REWRITE
    if brief_score and brief_score < 45:
        return REVISION_MODE_REWRITE
    if (hook_score and hook_score < 55) or (choice_score and choice_score < 55):
        return REVISION_MODE_TARGETED
    if any("ai_flavor" in issue or "translationese" in issue for issue in issues):
        return REVISION_MODE_POLISH
    return ""


def _select_mode(*, candidates: list[str], signals: list[str]) -> str:
    if REVISION_MODE_FRESH in candidates:
        return REVISION_MODE_FRESH
    if REVISION_MODE_REWRITE in candidates:
        return REVISION_MODE_REWRITE
    if any(signal in signals for signal in ("localized_language_target", "localized_scope_request")):
        return REVISION_MODE_LOCAL_PATCH
    if "style_or_prose_request" in signals and "scene_or_reader_experience_request" not in signals:
        return REVISION_MODE_POLISH
    if REVISION_MODE_TARGETED in candidates:
        return REVISION_MODE_TARGETED
    if REVISION_MODE_POLISH in candidates:
        return REVISION_MODE_POLISH
    return REVISION_MODE_LOCAL_PATCH


def _confidence(*, mode: str, signals: list[str], quality_data: dict, latest_version: ChapterVersion | None) -> int:
    score = 55 + min(30, len(set(signals)) * 7)
    if quality_data:
        score += 8
    if latest_version:
        score += 5
    if any(signal.startswith("explicit_") for signal in signals):
        score += 10
    if mode == REVISION_MODE_TARGETED and "default_author_feedback_targeted" in signals:
        score -= 8
    return max(35, min(100, score))


def _reason(*, mode: str, signals: list[str]) -> str:
    if mode == REVISION_MODE_LOCAL_PATCH:
        return "意见命中句段、称谓或局部范围，优先最小改动，避免破坏已成立章节事实。"
    if mode == REVISION_MODE_POLISH:
        return "意见主要指向表达、文风、对白或自然度，章节结构暂不需要推翻。"
    if mode == REVISION_MODE_TARGETED:
        return "意见命中读者体验、行动链、承接或章末钩子，旧稿结构仍有保留价值。"
    if mode == REVISION_MODE_REWRITE:
        return "意见或质检显示章节级兑现不足，需要在最新骨架内结构性重写。"
    return "意见或质检显示旧稿方向会污染后续创作，需要按最新骨架重启本章。"


def _preserve_items(*, mode: str, latest_version: ChapterVersion | None) -> list[str]:
    if mode == REVISION_MODE_FRESH:
        return ["最新 Story Bible", "最新 Canon", "最新章节 brief"]
    if mode == REVISION_MODE_REWRITE:
        return ["最新 Story Bible", "已登记 Canon", "人工明确认可的事实"]
    if mode == REVISION_MODE_TARGETED:
        return ["可用开篇", "已成立 Canon", "有效场景顺序", "章末事实除非意见要求改变"]
    if mode == REVISION_MODE_POLISH:
        return ["章节结构", "场景顺序", "人物关系", "章末事实"]
    return ["未命中的正文", "场景顺序", "人物关系", "章末事实"]


def _replace_items(*, mode: str, text: str, quality_data: dict) -> list[str]:
    items: list[str] = []
    if "钩子" in text or "章末" in text:
        items.append("章末钩子和最后300字")
    if "主角" in text and ("主动" in text or "被动" in text):
        items.append("主角主动选择段落")
    if "承接" in text:
        items.append("场景承接段落")
    if "对白" in text:
        items.append("对白和人物反应")
    if "文风" in text or "ai" in text or "啰嗦" in text:
        items.append("表达密度和AI味段落")
    dimensions = quality_data.get("dimensions") if isinstance(quality_data.get("dimensions"), dict) else {}
    if int(dimensions.get("chapter_unit_flow") or 100) < 55:
        items.append("失败小单元")
    if int(dimensions.get("brief_coverage") or 100) < 50:
        items.append("未兑现的章节目标")
    if not items:
        if mode == REVISION_MODE_LOCAL_PATCH:
            items.append("人工意见命中的句子或短段落")
        elif mode == REVISION_MODE_POLISH:
            items.append("表达、节奏和自然分段")
        elif mode == REVISION_MODE_TARGETED:
            items.append("明确不合格的场景或单元")
        else:
            items.append("开篇牵引、行动链、信息释放和章末钩子")
    return _dedupe(items)[:6]


def _escalation_rule(mode: str) -> str:
    if mode == REVISION_MODE_LOCAL_PATCH:
        return "验收仍命中同一问题时升级为targeted。"
    if mode == REVISION_MODE_POLISH:
        return "表达修复后仍影响读者体验时升级为targeted。"
    if mode == REVISION_MODE_TARGETED:
        return "定点修订后author_intent或brief_coverage仍低于60时升级为rewrite。"
    if mode == REVISION_MODE_REWRITE:
        return "结构性重写后仍偏离Story Bible或Canon时升级为fresh。"
    return "fresh仍失败时停止正文生产，回到作品骨架和章节brief修复。"


def _brief_contains_fresh(brief: ChapterBrief | None) -> bool:
    if not brief:
        return False
    merged = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
    return "修订模式:fresh" in merged or "旧稿已废弃" in merged


def _normalize_text(value: str) -> str:
    return (value or "").strip().lower().replace("：", ":")


def _has_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle.lower() in text for needle in needles)


def _dedupe(items: list[str]) -> list[str]:
    result: list[str] = []
    for item in items:
        if item and item not in result:
            result.append(item)
    return result
