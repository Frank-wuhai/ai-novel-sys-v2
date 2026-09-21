from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parents[2]
load_dotenv(ROOT_DIR / ".env")


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "")
    if not raw:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///data/novel.db")
    llm_plan: str = os.getenv("LLM_PLAN", os.getenv("ARK_PLAN", "agent_plan")).strip().lower() or "agent_plan"
    ark_api_key: str = os.getenv("ARK_API_KEY", "")
    ark_agent_plan_api_key: str = os.getenv("ARK_AGENT_PLAN_API_KEY", os.getenv("AGENT_PLAN_API_KEY", ""))
    ark_search_api_key: str = os.getenv("ARK_SEARCH_API_KEY", os.getenv("AGENT_PLAN_SEARCH_API_KEY", ""))
    ark_search_base_url: str = os.getenv("ARK_SEARCH_BASE_URL", "")
    tavily_api_key: str = os.getenv("TAVILY_API_KEY", "")
    web_search_provider_order: str = os.getenv("WEB_SEARCH_PROVIDER_ORDER", "tavily,agent_plan_manual").strip()
    agent_plan_search_monthly_limit: int = _int_env("AGENT_PLAN_SEARCH_MONTHLY_LIMIT", 150)
    tavily_search_monthly_limit: int = _int_env("TAVILY_SEARCH_MONTHLY_LIMIT", 1000)
    ark_base_url: str = os.getenv("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/plan/v3")
    ark_embedding_model: str = os.getenv("ARK_EMBEDDING_MODEL", "doubao-embedding-vision-251215")
    ark_vision_model: str = os.getenv("ARK_VISION_MODEL", "doubao-seed-2.0-lite")
    ark_image_model: str = os.getenv("ARK_IMAGE_MODEL", "doubao-seedream-5.0-lite")
    ark_video_model: str = os.getenv("ARK_VIDEO_MODEL", "doubao-seedance-2.0-fast")
    auto_live_embedding: bool = _bool_env("AUTO_LIVE_EMBEDDING", True)
    auto_live_embedding_max_chunks: int = _int_env("AUTO_LIVE_EMBEDDING_MAX_CHUNKS", 160)
    llm_require_coding_plan: bool = _bool_env("LLM_REQUIRE_CODING_PLAN", False)
    model_name: str = os.getenv("MODEL_NAME", "deepseek-v4-pro")
    llm_planning_model: str = os.getenv("LLM_PLANNING_MODEL", os.getenv("MODEL_NAME", "deepseek-v4-flash"))
    llm_draft_model: str = os.getenv("LLM_DRAFT_MODEL", os.getenv("MODEL_NAME", "deepseek-v4-pro"))
    llm_revision_model: str = os.getenv("LLM_REVISION_MODEL", os.getenv("MODEL_NAME", "deepseek-v4-pro"))
    llm_review_model: str = os.getenv("LLM_REVIEW_MODEL", os.getenv("MODEL_NAME", "deepseek-v4-flash"))
    llm_temperature: float = _float_env("LLM_TEMPERATURE", 0.55)
    llm_planning_temperature: float = _float_env("LLM_PLANNING_TEMPERATURE", 0.4)
    llm_draft_temperature: float = _float_env("LLM_DRAFT_TEMPERATURE", _float_env("LLM_TEMPERATURE", 0.55))
    llm_revision_temperature: float = _float_env("LLM_REVISION_TEMPERATURE", _float_env("LLM_TEMPERATURE", 0.55))
    llm_review_temperature: float = _float_env("LLM_REVIEW_TEMPERATURE", 0.35)
    # 主编评审多次采样次数：LLM 评分在 70-86 间抖动会让 75 门禁沦为抛硬币。
    # 采样 N 次取中位数分数+多数 verdict 消除单次波动，保证入库判定可复现。
    # 默认 3；设为 1 退化为单次采样（旧行为）。
    llm_review_samples: int = _int_env("LLM_REVIEW_SAMPLES", 3)
    # intent_acceptance 语义复核采样数: 与 llm_review 同源抖动(2026-09-18 同文两次
    # 评审意图分 71 vs 33, 全是单发复核的随机性), 逐点多数票, 默认 3; 设 1 退化旧行为。
    intent_acceptance_samples: int = _int_env("INTENT_ACCEPTANCE_SAMPLES", 3)
    llm_draft_max_tokens: int = _int_env("LLM_DRAFT_MAX_TOKENS", 5000)
    # 默认 16000: 修订调用要装下全章正文+质检报告(~10KB)+thinking 推理。
    # 9000/8000 在 kimi-k3 真机上重写模式连败（2026-09-21 ch3 重建实测，
    # 推理烧完预算返回空/纯思维链），16000 一次成功；与判卷修复同型。
    llm_revision_max_tokens: int = _int_env("LLM_REVISION_MAX_TOKENS", 16000)
    # 空文本兜底模型(2026-09-21): 指向非 thinking 模型名; 留空时按 -thinking 后缀
    # 自动推导, 推导不出(如 kimi-k3)则同模型抬预算重发。见 production_llm._empty_text_fallback。
    llm_fallback_model: str = os.getenv("LLM_FALLBACK_MODEL", "")
    llm_review_max_tokens: int = _int_env("LLM_REVIEW_MAX_TOKENS", 2200)
    # 成文判据判卷 (2026-09-10 第3步): prose_judgement_v1 要求温度 0、固定判卷 prompt、
    # 固定模型，产出缺口表 (无 verdict/score，不自动 FAIL)。
    prose_judge_model: str = os.getenv("PROSE_JUDGE_MODEL", os.getenv("MODEL_NAME", "deepseek-v4-flash"))
    prose_judge_temperature: float = _float_env("PROSE_JUDGE_TEMPERATURE", 0.0)
    # 默认 16000: thinking 类模型(如 kimi-k3)判卷 reasoning 实测 7400-11000+ Token,
    # 成功那次用掉 7921/8000(踩在悬崖上), 8000 与 12000 上限均实测 StructuredOutputError,
    # 16000 才稳定 (2026-09-18 第4步/v6 复审三次真机对照, 2026-09-20 第 4.5 步提默认)。
    prose_judge_max_tokens: int = _int_env("PROSE_JUDGE_MAX_TOKENS", 16000)
    llm_smoke_max_tokens: int = _int_env("LLM_SMOKE_MAX_TOKENS", 20)
    llm_request_timeout_seconds: int = _int_env("LLM_REQUEST_TIMEOUT_SECONDS", 300)
    llm_revision_prompt_max_chars: int = _int_env("LLM_REVISION_PROMPT_MAX_CHARS", 9000)
    llm_revision_candidate_count: int = _int_env("LLM_REVISION_CANDIDATE_COUNT", 1)
    revision_persistent_max_full_revisions: int = _int_env("REVISION_PERSISTENT_MAX_FULL_REVISIONS", 2)
    production_auto_revision_loop_max_rounds: int = _int_env("PRODUCTION_AUTO_REVISION_LOOP_MAX_ROUNDS", 2)
    paradigm_refine_enabled: bool = _bool_env("PARADIGM_REFINE_ENABLED", False)
    b_pipeline_enabled: bool = _bool_env("B_PIPELINE_ENABLED", False)
    draft_inline_quality_loop_enabled: bool = _bool_env("DRAFT_INLINE_QUALITY_LOOP_ENABLED", False)
    draft_llm_repair_enabled: bool = _bool_env("DRAFT_LLM_REPAIR_ENABLED", False)
    b_pipeline_model: str = os.getenv("B_PIPELINE_MODEL", "deepseek-v4-pro-thinking")
    production_profile: str = os.getenv("PRODUCTION_PROFILE", "standard").strip().lower() or "standard"
    production_mode: str = os.getenv("PRODUCTION_MODE", "trial").strip().lower() or "trial"
    llm_input_price_per_1m_tokens: float = _float_env("LLM_INPUT_PRICE_PER_1M_TOKENS", 0.0)
    llm_output_price_per_1m_tokens: float = _float_env("LLM_OUTPUT_PRICE_PER_1M_TOKENS", 0.0)
    outputs_dir: Path = ROOT_DIR / "outputs"


settings = Settings()
