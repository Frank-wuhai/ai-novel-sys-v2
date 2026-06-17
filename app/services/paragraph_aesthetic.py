from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ParagraphAestheticReport:
    score: int
    status: str
    weak_paragraphs: list[dict[str, Any]]
    issues: list[str]
    revision_targets: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "status": self.status,
            "weak_paragraphs": self.weak_paragraphs,
            "issues": self.issues,
            "revision_targets": self.revision_targets,
        }


def evaluate_paragraph_aesthetic(text: str) -> ParagraphAestheticReport:
    paragraphs = _paragraphs(text)
    weak: list[dict[str, Any]] = []
    issues: list[str] = []
    for index, paragraph in enumerate(paragraphs, start=1):
        row_issues = _paragraph_issues(paragraph)
        if row_issues:
            weak.append(
                {
                    "index": index,
                    "chars": len(paragraph),
                    "issues": row_issues,
                    "excerpt": paragraph[:120],
                }
            )
            issues.extend(row_issues)
    penalty = min(55, len(weak) * 6 + len(set(issues)) * 5)
    if paragraphs:
        abstract_ratio = sum(1 for paragraph in paragraphs if "abstract_summary" in _paragraph_issues(paragraph)) / len(paragraphs)
        if abstract_ratio >= 0.35:
            penalty += 12
            issues.append("abstract_summary_ratio_high")
    score = max(0, min(100, 100 - penalty))
    targets = _revision_targets(weak)
    return ParagraphAestheticReport(
        score=score,
        status="pass" if score >= 70 and not weak[:2] else "attention",
        weak_paragraphs=weak[:8],
        issues=list(dict.fromkeys(issues))[:12],
        revision_targets=targets,
    )


def format_paragraph_aesthetic_contract(report: dict[str, Any]) -> str:
    if not report or report.get("status") == "pass":
        return ""
    lines = ["段落级审美修订："]
    for item in report.get("revision_targets") or []:
        lines.append(f"- {item}")
    return "\n".join(lines)


def _paragraphs(text: str) -> list[str]:
    raw = [item.strip() for item in re.split(r"\n\s*\n|\r\n\s*\r\n", str(text or "")) if item.strip()]
    if len(raw) <= 1:
        raw = [item.strip() for item in re.split(r"(?<=。)", str(text or "")) if len(item.strip()) >= 40]
    return raw[:36]


def _paragraph_issues(paragraph: str) -> list[str]:
    issues: list[str] = []
    if len(paragraph) < 60:
        return issues
    if _abstract_density(paragraph) >= 4 and _sensory_count(paragraph) < 2:
        issues.append("abstract_summary")
    if _action_count(paragraph) < 2 and len(paragraph) >= 120:
        issues.append("low_action")
    if _sensory_count(paragraph) < 1 and len(paragraph) >= 120:
        issues.append("low_embodied_pov")
    if _dialogue_marker_count(paragraph) == 0 and len(paragraph) >= 220:
        issues.append("dialogue_absent_long_paragraph")
    if any(marker in paragraph for marker in ("冷", "沉默", "阴影", "黑暗", "血迹")) and _positive_motion_count(paragraph) == 0:
        issues.append("cold_grim_tone_without_release")
    return issues


def _abstract_density(text: str) -> int:
    markers = ("仿佛", "似乎", "一种", "某种", "气息", "意味", "感觉", "压迫", "沉默", "冰冷", "深处", "本能")
    return sum(text.count(marker) for marker in markers)


def _action_count(text: str) -> int:
    markers = ("走", "退", "伸", "按", "推", "拦", "递", "拿", "放", "问", "笑", "咳", "看", "听", "握", "转")
    return sum(1 for marker in markers if marker in text)


def _sensory_count(text: str) -> int:
    markers = ("掌心", "指尖", "胸口", "喉", "汗", "疼", "痛", "烫", "麻", "冷", "热", "声", "味", "光", "影", "看见", "听见")
    return sum(1 for marker in markers if marker in text)


def _dialogue_marker_count(text: str) -> int:
    return text.count("“") + text.count("\"")


def _positive_motion_count(text: str) -> int:
    markers = ("笑", "亮", "热", "闹", "喊", "掌声", "酒", "茶", "灯", "人声", "风")
    return sum(1 for marker in markers if marker in text)


def _revision_targets(weak: list[dict[str, Any]]) -> list[str]:
    rows: list[str] = []
    for item in weak[:6]:
        issues = set(item.get("issues") or [])
        prefix = f"第{item.get('index')}段"
        if "abstract_summary" in issues:
            rows.append(f"{prefix}: 把抽象判断改成空间、物件、声音、气味、动作和人物反应。")
        if "low_action" in issues:
            rows.append(f"{prefix}: 补主角或配角的可见动作，不要只写结论。")
        if "low_embodied_pov" in issues:
            rows.append(f"{prefix}: 补身体/感官视角，让读者从角色当下进入场景。")
        if "dialogue_absent_long_paragraph" in issues:
            rows.append(f"{prefix}: 加一处能改变局面的对白或短交锋。")
        if "cold_grim_tone_without_release" in issues:
            rows.append(f"{prefix}: 压抑氛围必须释放到行动、收益或人物趣味，不能只冷下去。")
    return list(dict.fromkeys(rows))[:8]
