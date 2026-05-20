from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parents[2]
load_dotenv(ROOT_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///data/novel.db")
    ark_api_key: str = os.getenv("ARK_API_KEY", "")
    ark_base_url: str = os.getenv("ARK_BASE_URL", "")
    model_name: str = os.getenv("MODEL_NAME", "deepseek-v3.2")
    outputs_dir: Path = ROOT_DIR / "outputs"


settings = Settings()

