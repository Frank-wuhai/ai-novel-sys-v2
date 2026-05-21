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


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///data/novel.db")
    ark_api_key: str = os.getenv("ARK_API_KEY", "")
    ark_base_url: str = os.getenv("ARK_BASE_URL", "")
    model_name: str = os.getenv("MODEL_NAME", "deepseek-v3.2")
    llm_temperature: float = _float_env("LLM_TEMPERATURE", 0.7)
    llm_draft_max_tokens: int = _int_env("LLM_DRAFT_MAX_TOKENS", 3000)
    llm_revision_max_tokens: int = _int_env("LLM_REVISION_MAX_TOKENS", 3000)
    llm_smoke_max_tokens: int = _int_env("LLM_SMOKE_MAX_TOKENS", 20)
    llm_input_price_per_1m_tokens: float = _float_env("LLM_INPUT_PRICE_PER_1M_TOKENS", 0.0)
    llm_output_price_per_1m_tokens: float = _float_env("LLM_OUTPUT_PRICE_PER_1M_TOKENS", 0.0)
    outputs_dir: Path = ROOT_DIR / "outputs"


settings = Settings()
