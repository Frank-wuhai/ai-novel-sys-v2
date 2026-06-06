from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AntiAIFlavorReport:
    score: int
    checks: dict[str, int]
    issues: list[str]
    recommendations: list[str]

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "checks": self.checks,
            "issues": self.issues,
            "recommendations": self.recommendations,
        }


def evaluate_anti_ai_flavor(*, design, prose_voice, humanized) -> AntiAIFlavorReport:
    """Combine design texture, prose voice, and humanized delivery into one editor-facing signal."""
    design_checks = getattr(design, "checks", {}) or {}
    voice_checks = getattr(prose_voice, "checks", {}) or {}
    humanized_checks = getattr(humanized, "checks", {}) or {}
    checks = {
        "designed_texture": min(
            int(getattr(design, "score", 0) or 0),
            int(design_checks.get("designed_nomenclature", 0) or 0),
        ),
        "imageability": min(
            int(design_checks.get("visual_staging", 0) or 0),
            int(design_checks.get("imageable_paragraphs", 0) or 0),
        ),
        "native_prose": int(voice_checks.get("native_chinese_flow", 0) or 0),
        "dialogue_voice": min(
            int(voice_checks.get("dialogue_fullness", 0) or 0),
            int(voice_checks.get("character_voice", 0) or 0),
        ),
        "human_scene_delivery": min(
            int(humanized_checks.get("interaction_reaction", 0) or 0),
            int(humanized_checks.get("embedded_setting", 0) or 0),
        ),
    }
    score = round(sum(checks.values()) / len(checks)) if checks else 0
    issues = [f"{name}={value}" for name, value in checks.items() if value < _threshold(name)]
    return AntiAIFlavorReport(
        score=max(0, min(100, score)),
        checks=checks,
        issues=issues,
        recommendations=_recommendations(checks),
    )


def _threshold(name: str) -> int:
    return {
        "designed_texture": 65,
        "imageability": 55,
        "native_prose": 60,
        "dialogue_voice": 50,
        "human_scene_delivery": 60,
    }[name]


def _recommendations(checks: dict[str, int]) -> list[str]:
    rows: list[str] = []
    if checks.get("designed_texture", 100) < 65:
        rows.append("去AI味儿：补足专名、物件和场景的来源、功能、利益关系和代价，不要像临时生成的标签。")
    if checks.get("imageability", 100) < 55:
        rows.append("去AI味儿：把抽象推进改成可分镜场景，写清空间、光源、站位、关键物件和动作轨迹。")
    if checks.get("native_prose", 100) < 60:
        rows.append("去AI味儿：删掉翻译腔、分析腔和逻辑标签，把判断落到动作、感官、误判和即时反应里。")
    if checks.get("dialogue_voice", 100) < 50:
        rows.append("去AI味儿：关键对白不能只承担功能，要带出人物的身份、情绪、顾虑、试探或威胁。")
    if checks.get("human_scene_delivery", 100) < 60:
        rows.append("去AI味儿：减少说明书式设定，让人物在互动、冲突和后果里自然暴露信息。")
    return rows
