from __future__ import annotations

import json
from pathlib import Path


FANQIE_PLATFORM_ALIASES = {"番茄", "番茄小说", "番茄小说网", "fanqie", "fanqie_novel"}
FANQIE_DEFAULT_WRITER_URL = "https://fanqienovel.com/author"


def is_fanqie_platform(platform: str) -> bool:
    return platform.strip().lower() in FANQIE_PLATFORM_ALIASES or "番茄" in platform


def write_fanqie_publish_plan(
    *,
    artifact_dir: Path,
    mode: str,
    title: str,
    content: str,
    target_config: dict,
) -> Path:
    work_identifier = str(target_config.get("work_identifier") or target_config.get("book_id") or "")
    plan = {
        "platform": "番茄小说",
        "mode": mode,
        "writer_url": target_config.get("writer_url", FANQIE_DEFAULT_WRITER_URL),
        "work_identifier": work_identifier,
        "chapter_title": title,
        "content_chars": len(content),
        "publish_mode": target_config.get("publish_mode", "immediate"),
        "schedule_at": target_config.get("schedule_at", ""),
        "safety": {
            "fill_only": mode != "confirmed",
            "real_publish_requires_enable_real_publish": True,
            "real_publish_enabled": bool(target_config.get("enable_real_publish", False)),
        },
        "browser": {
            "cdp_url": target_config.get("cdp_url", ""),
            "headless": bool(target_config.get("headless", False)),
            "user_data_dir": target_config.get("user_data_dir", ""),
        },
        "selectors": _fanqie_selectors(target_config),
    }
    path = artifact_dir / "fanqie_publish_plan.json"
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def fanqie_script_command(*, artifact_dir: Path, confirm: bool) -> list[str]:
    cmd = [
        "python",
        "scripts/publish_fanqie.py",
        "--artifact-dir",
        str(artifact_dir),
    ]
    if confirm:
        cmd.append("--confirm")
    return cmd


def _fanqie_selectors(config: dict) -> dict:
    selectors = config.get("selectors", {})
    if not isinstance(selectors, dict):
        selectors = {}
    return {
        "title": selectors.get("title", "[placeholder*='章节名'], input"),
        "editor": selectors.get("editor", "[contenteditable='true'], textarea"),
        "next_button": selectors.get("next_button", "text=下一步"),
        "publish_button": selectors.get("publish_button", "text=发布"),
        "confirm_button": selectors.get("confirm_button", "text=确认"),
    }
