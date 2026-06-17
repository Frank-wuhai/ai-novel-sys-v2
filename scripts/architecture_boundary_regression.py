from __future__ import annotations

import ast
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


MAX_LINES = {
    "app/services/production.py": 220,
    "scripts/run_local_dashboard.py": 3100,
    "app/dashboard_assets.py": 3300,
    "app/services/author_runner.py": 180,
}

FORBIDDEN_DEFS = {
    "app/services/production.py": {
        "draft_chapter",
        "revise_chapter",
        "review_chapter",
        "execute_publish_job",
        "publish_job_dry_run",
    },
    "scripts/run_local_dashboard.py": {
        "_acceptance_points",
        "_point_covered",
        "_author_terminal_status",
        "_author_background_timeout_seconds",
    },
}

REQUIRED_DEFS = {
    "app/services/intent_acceptance.py": {"evaluate_author_intent"},
    "app/services/readability.py": {"evaluate_readability"},
    "app/services/production_state.py": {"latest_story_brief"},
    "app/services/author_runner.py": {"run_author_mode", "author_terminal_status"},
    "app/services/continuity.py": {"default_chapter_continuity_summary"},
}


def main() -> int:
    failures: list[str] = []
    for relative_path, max_lines in MAX_LINES.items():
        path = ROOT / relative_path
        count = _line_count(path)
        if count > max_lines:
            failures.append(f"{relative_path} has {count} lines, expected <= {max_lines}")
    for relative_path, forbidden in FORBIDDEN_DEFS.items():
        names = _defined_function_names(ROOT / relative_path)
        hits = sorted(names & forbidden)
        if hits:
            failures.append(f"{relative_path} defines boundary-owned functions: {', '.join(hits)}")
    for relative_path, required in REQUIRED_DEFS.items():
        names = _defined_function_names(ROOT / relative_path)
        missing = sorted(required - names)
        if missing:
            failures.append(f"{relative_path} is missing required service entry points: {', '.join(missing)}")
    status = "fail" if failures else "pass"
    print(json.dumps({"status": status, "failures": failures}, ensure_ascii=False, indent=2))
    return 1 if failures else 0


def _line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def _defined_function_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


if __name__ == "__main__":
    raise SystemExit(main())
