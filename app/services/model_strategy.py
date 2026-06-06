from __future__ import annotations

from app.core.config import settings


def build_model_strategy() -> dict:
    plan = "coding_plan" if settings.llm_require_coding_plan else settings.llm_plan
    roles = {
        "planning": {
            "model": settings.llm_planning_model,
            "temperature": settings.llm_planning_temperature,
            "use_for": "骨架、brief、导演单、诊断和短结构化任务",
        },
        "draft": {
            "model": settings.llm_draft_model,
            "temperature": settings.llm_draft_temperature,
            "max_tokens": settings.llm_draft_max_tokens,
            "use_for": "正文草稿和新章节创作",
        },
        "revision": {
            "model": settings.llm_revision_model,
            "temperature": settings.llm_revision_temperature,
            "max_tokens": settings.llm_revision_max_tokens,
            "use_for": "定点修订、结构重写和 fresh 重启",
        },
        "review": {
            "model": settings.llm_review_model,
            "temperature": settings.llm_review_temperature,
            "max_tokens": settings.llm_review_max_tokens,
            "use_for": "主编审稿、可读性判断和软问题诊断",
        },
        "local_patch": {
            "model": "none",
            "temperature": 0,
            "use_for": "明确偏差词或短句替换，优先确定性处理，不调用模型",
        },
    }
    warnings = []
    if len({roles["planning"]["model"], roles["draft"]["model"], roles["revision"]["model"], roles["review"]["model"]}) == 1:
        warnings.append("所有任务使用同一模型；可运行，但成本和稳定性未做分层优化。")
    if settings.llm_draft_temperature > 0.75 or settings.llm_revision_temperature > 0.75:
        warnings.append("正文或修订温度偏高，可能增加跑偏和不稳定。")
    return {
        "llm_plan": plan,
        "roles": roles,
        "warnings": warnings,
        "recommendations": [
            "Agent Plan 生产时优先把搜索、Embedding 和多模态能力接入 Evidence/Canon/反馈层。",
            "规划/诊断使用低温稳定模型。",
            "正文和整章重写使用更强模型。",
            "审稿使用低温判断型模型。",
            "局部补丁优先不用模型。",
        ],
    }
