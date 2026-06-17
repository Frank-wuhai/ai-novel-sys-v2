from __future__ import annotations

from pathlib import Path


DASHBOARD_HTML = (Path(__file__).with_name("dashboard.html")).read_text(encoding="utf-8")
