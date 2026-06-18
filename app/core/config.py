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
    llm_draft_max_tokens: int = _int_env("LLM_DRAFT_MAX_TOKENS", 8000)
    llm_revision_max_tokens: int = _int_env("LLM_REVISION_MAX_TOKENS", 8000)
    llm_review_max_tokens: int = _int_env("LLM_REVIEW_MAX_TOKENS", 2200)
    llm_smoke_max_tokens: int = _int_env("LLM_SMOKE_MAX_TOKENS", 20)
    llm_request_timeout_seconds: int = _int_env("LLM_REQUEST_TIMEOUT_SECONDS", 150)
    llm_input_price_per_1m_tokens: float = _float_env("LLM_INPUT_PRICE_PER_1M_TOKENS", 0.0)
    llm_output_price_per_1m_tokens: float = _float_env("LLM_OUTPUT_PRICE_PER_1M_TOKENS", 0.0)
    outputs_dir: Path = ROOT_DIR / "outputs"


settings = Settings()
