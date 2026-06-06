from __future__ import annotations


def attribute_generation_failure(
    *,
    preflight: dict | None = None,
    bias: dict | None = None,
    intent: dict | None = None,
    quality: dict | None = None,
) -> dict:
    reasons: list[str] = []
    actions: list[str] = []
    category = "unknown"

    preflight = preflight or {}
    bias = bias or {}
    intent = intent or {}
    quality = quality or {}

    if preflight.get("blockers"):
        category = "context"
        reasons.extend(str(item) for item in preflight.get("blockers", []))
        actions.append("先处理生产前体检阻断项，再继续生产。")
    if bias.get("model_bias_hits"):
        category = "model_drift"
        reasons.append("模型滑回默认套路：" + "，".join(bias.get("model_bias_hits", [])))
        actions.append("优先使用 local_patch 或 targeted_revision，只清除偏差表达。")
    if intent.get("blockers") or (intent.get("score") is not None and int(intent.get("score") or 0) < 60):
        if category == "unknown":
            category = "intent_underfulfilled"
        reasons.append(f"作者意图兑现不足：{intent.get('score', 0)} 分")
        actions.append("补强章节导演单，明确必须保留、删除、新增和章末期待。")
    if quality and quality.get("passed") is False:
        if category == "unknown":
            category = "quality"
        reasons.append(f"质检未通过：{quality.get('score', '')} 分")
        actions.append("按质检硬问题修订；若只是软维度低，交给作者判断。")
    if category == "unknown":
        category = "ready_or_manual_judgment"
        actions.append("当前没有明确系统阻断项，建议作者阅读后决定通过、局部改或重写。")
    return {
        "category": category,
        "reasons": _dedupe(reasons),
        "recommended_actions": _dedupe(actions),
    }


def _dedupe(items: list[str]) -> list[str]:
    seen = set()
    result = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result
