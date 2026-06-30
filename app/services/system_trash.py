from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


TRASH_ROOT = Path("data/trash")
LEGACY_MARKERS = (
    "陈默",
    "大江湖",
    "修订合同:",
    "依据质检报告",
    "原始机器修订建议",
    "医疗机构",
    "科技公司",
    "资本实验",
    "资本收购",
)
PRODUCTION_SUFFIXES = {".py", ".html", ".md", ".txt"}
AUTO_CACHE_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


@dataclass(frozen=True)
class TrashCandidate:
    path: str
    category: str
    reason: str
    action: str
    safe_to_move: bool
    size_bytes: int


@dataclass(frozen=True)
class TrashPlan:
    root: str
    created_at: str
    candidates: list[TrashCandidate]
    review_only: list[TrashCandidate]

    def to_dict(self) -> dict:
        return {
            "root": self.root,
            "created_at": self.created_at,
            "candidates": [asdict(item) for item in self.candidates],
            "review_only": [asdict(item) for item in self.review_only],
            "summary": {
                "auto_count": len(self.candidates),
                "review_count": len(self.review_only),
                "auto_size_bytes": sum(item.size_bytes for item in self.candidates),
            },
        }


@dataclass(frozen=True)
class SlimPolicy:
    max_workspace_bytes: int = 450 * 1024 * 1024
    min_free_bytes_to_reclaim: int = 128 * 1024 * 1024
    backup_keep_latest: int = 5
    backup_retention_days: int = 3
    trash_retention_days: int = 2
    include_logs: bool = True
    log_retention_days: int = 7


def build_trash_plan(
    *,
    root: Path,
    include_logs: bool = False,
    log_retention_days: int = 7,
) -> TrashPlan:
    root = root.resolve()
    tracked = _tracked_paths(root)
    candidates: list[TrashCandidate] = []
    review_only: list[TrashCandidate] = []
    now = datetime.now(timezone.utc).timestamp()

    for path in sorted(root.rglob("*")):
        if _skip_path(root, path):
            continue
        relative = path.relative_to(root).as_posix()
        if path.is_dir() and path.name in AUTO_CACHE_DIRS:
            candidates.append(_candidate(root, path, "cache", f"{path.name} cache directory", "move_to_trash", True))
            continue
        if not path.is_file():
            continue
        if _is_regression_database(relative):
            candidates.append(_candidate(root, path, "regression_db", "isolated regression/smoke database", "move_to_trash", True))
            continue
        if include_logs and relative.startswith("data/logs/") and path.suffix == ".log":
            age_days = (now - path.stat().st_mtime) / 86400
            if age_days >= max(0, log_retention_days):
                candidates.append(_candidate(root, path, "log", f"log older than {log_retention_days} days", "move_to_trash", True))
            continue
        if relative not in tracked and _looks_like_generated_temp(relative):
            candidates.append(_candidate(root, path, "generated_temp", "untracked generated/temp artifact", "move_to_trash", True))
            continue
        if path.suffix in PRODUCTION_SUFFIXES and relative in tracked and _contains_legacy_marker(path):
            review_only.append(_candidate(root, path, "legacy_marker", "tracked source contains legacy marker; review before editing", "report_only", False))

    candidates = _dedupe_nested_candidates(candidates)
    return TrashPlan(
        root=str(root),
        created_at=datetime.now(timezone.utc).isoformat(),
        candidates=candidates,
        review_only=review_only,
    )


def build_auto_slim_plan(*, root: Path, policy: SlimPolicy | None = None) -> TrashPlan:
    policy = policy or SlimPolicy()
    root = root.resolve()
    base = build_trash_plan(root=root, include_logs=policy.include_logs, log_retention_days=policy.log_retention_days)
    candidates = list(base.candidates)
    review_only = list(base.review_only)
    candidates.extend(_backup_candidates(root, policy=policy))
    candidates.extend(_old_trash_candidates(root, policy=policy))
    candidates = _dedupe_nested_candidates(candidates)
    candidates = _prioritize_slim_candidates(candidates)
    total_size = _path_size(root)
    planned_size = sum(item.size_bytes for item in candidates)
    if total_size <= policy.max_workspace_bytes and planned_size < policy.min_free_bytes_to_reclaim:
        candidates = []
    return TrashPlan(
        root=str(root),
        created_at=datetime.now(timezone.utc).isoformat(),
        candidates=candidates,
        review_only=review_only,
    )


def apply_auto_slim_plan(*, root: Path, plan: TrashPlan, label: str = "auto-slim") -> dict:
    root = root.resolve()
    purge_candidates = [item for item in plan.candidates if item.category == "old_trash_batch"]
    move_candidates = [item for item in plan.candidates if item.category != "old_trash_batch"]
    move_plan = TrashPlan(
        root=plan.root,
        created_at=plan.created_at,
        candidates=move_candidates,
        review_only=plan.review_only,
    )
    result = apply_trash_plan(root=root, plan=move_plan, label=label)
    purged: list[dict] = []
    for item in purge_candidates:
        source = (root / item.path).resolve()
        if not source.exists() or not _is_purgeable_trash_batch(root, source):
            continue
        size = _path_size(source)
        shutil.rmtree(source)
        purged.append({"path": item.path, "size_bytes": size, "reason": item.reason})
    result["purged_count"] = len(purged)
    result["purged_size_bytes"] = sum(item["size_bytes"] for item in purged)
    result["purged"] = purged
    manifest_path = Path(result["manifest_path"])
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["auto_slim_purged"] = purged
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def apply_trash_plan(*, root: Path, plan: TrashPlan, label: str = "") -> dict:
    root = root.resolve()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = f"-{_safe_label(label)}" if label else ""
    trash_dir = root / TRASH_ROOT / f"{stamp}{suffix}"
    trash_dir.mkdir(parents=True, exist_ok=True)
    moved: list[dict] = []
    for item in plan.candidates:
        source = (root / item.path).resolve()
        if not source.exists() or not _is_within(root, source):
            continue
        target = trash_dir / item.path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        moved.append({"from": item.path, "to": str(target.relative_to(root)), "reason": item.reason})
    manifest = {
        **plan.to_dict(),
        "applied_at": datetime.now(timezone.utc).isoformat(),
        "trash_dir": str(trash_dir.relative_to(root)),
        "moved": moved,
    }
    manifest_path = trash_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"trash_dir": str(trash_dir), "manifest_path": str(manifest_path), "moved_count": len(moved)}


def _candidate(root: Path, path: Path, category: str, reason: str, action: str, safe_to_move: bool) -> TrashCandidate:
    return TrashCandidate(
        path=path.relative_to(root).as_posix(),
        category=category,
        reason=reason,
        action=action,
        safe_to_move=safe_to_move,
        size_bytes=_path_size(path),
    )


def _tracked_paths(root: Path) -> set[str]:
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=str(root),
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return set()
    if result.returncode != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _skip_path(root: Path, path: Path) -> bool:
    relative = path.relative_to(root).as_posix()
    return (
        relative == ".git"
        or relative.startswith(".git/")
        or relative == "data/trash"
        or relative.startswith("data/trash/")
        or relative.startswith("data/backups/")
        or relative.startswith("venv/")
        or relative.startswith(".venv/")
        or (relative.startswith("scripts/") and relative.endswith("_regression.py"))
        or relative.startswith("docs/archive/")
    )


def _backup_candidates(root: Path, *, policy: SlimPolicy) -> list[TrashCandidate]:
    backup_dir = root / "data" / "backups"
    if not backup_dir.exists():
        return []
    files = [path for path in backup_dir.iterdir() if path.is_file()]
    production = sorted(
        [path for path in files if path.name.startswith("novel-") and path.suffix == ".db"],
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    keep = set(production[: max(0, policy.backup_keep_latest)])
    now = datetime.now(timezone.utc).timestamp()
    candidates: list[TrashCandidate] = []
    for path in files:
        age_days = (now - path.stat().st_mtime) / 86400
        if path in keep:
            continue
        if path.name.startswith("novel-") and age_days < max(0, policy.backup_retention_days):
            continue
        if path.suffix not in {".db", ".sqlite", ".sqlite3"}:
            continue
        reason = (
            f"database backup outside retention policy "
            f"(keep_latest={policy.backup_keep_latest}, retention_days={policy.backup_retention_days})"
        )
        candidates.append(_candidate(root, path, "database_backup", reason, "move_to_trash", True))
    return candidates


def _old_trash_candidates(root: Path, *, policy: SlimPolicy) -> list[TrashCandidate]:
    trash_root = root / TRASH_ROOT
    if not trash_root.exists():
        return []
    now = datetime.now(timezone.utc).timestamp()
    candidates: list[TrashCandidate] = []
    for path in sorted(trash_root.iterdir()):
        if not path.is_dir():
            continue
        age_days = (now - path.stat().st_mtime) / 86400
        if age_days < max(0, policy.trash_retention_days):
            continue
        candidates.append(
            _candidate(
                root,
                path,
                "old_trash_batch",
                f"trash batch older than {policy.trash_retention_days} days",
                "move_to_trash",
                True,
            )
        )
    return candidates


def _prioritize_slim_candidates(candidates: list[TrashCandidate]) -> list[TrashCandidate]:
    priority = {
        "regression_db": 10,
        "cache": 20,
        "log": 30,
        "generated_temp": 40,
        "old_trash_batch": 50,
        "database_backup": 60,
    }
    return sorted(candidates, key=lambda item: (priority.get(item.category, 99), item.path))


def _is_regression_database(relative: str) -> bool:
    path = Path(relative)
    if path.parent.as_posix() != "data":
        return False
    name = path.name
    return path.suffix in {".db", ".sqlite", ".sqlite3"} and (
        "regression" in name or name.startswith("smoke-") or name.endswith("-debug.db")
    )


def _looks_like_generated_temp(relative: str) -> bool:
    path = Path(relative)
    if path.suffix in {".pyc", ".pyo", ".tmp", ".temp", ".bak", ".swp"}:
        return True
    return path.name.endswith("~")


def _contains_legacy_marker(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return False
    return any(marker in text for marker in LEGACY_MARKERS)


def _dedupe_nested_candidates(candidates: list[TrashCandidate]) -> list[TrashCandidate]:
    selected: list[TrashCandidate] = []
    selected_dirs: list[str] = []
    for item in candidates:
        if any(item.path.startswith(prefix + "/") for prefix in selected_dirs):
            continue
        selected.append(item)
        if Path(item.path).suffix == "":
            selected_dirs.append(item.path)
    return selected


def _path_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    if path.is_dir():
        return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    return 0


def _is_within(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _is_purgeable_trash_batch(root: Path, path: Path) -> bool:
    trash_root = (root / TRASH_ROOT).resolve()
    if not path.is_dir() or path.parent != trash_root:
        return False
    return _is_within(root, path)


def _safe_label(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in value).strip("-")[:60] or "cleanup"
