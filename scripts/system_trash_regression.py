from __future__ import annotations

import json
import tempfile
from pathlib import Path

import os
import time

from app.services.system_trash import SlimPolicy, apply_auto_slim_plan, apply_trash_plan, build_auto_slim_plan, build_trash_plan


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        (root / "data").mkdir()
        (root / "app/services").mkdir(parents=True)
        (root / "scripts/__pycache__").mkdir(parents=True)
        (root / "data/book2-style-flow-regression.db").write_text("db", encoding="utf-8")
        (root / "scripts/__pycache__/x.cpython-311.pyc").write_bytes(b"cache")
        source = root / "app/services/example.py"
        source.write_text('PROMPT = "陈默进入大江湖"\n', encoding="utf-8")

        plan = build_trash_plan(root=root)
        auto_paths = {item.path for item in plan.candidates}
        review_paths = {item.path for item in plan.review_only}
        if "data/book2-style-flow-regression.db" not in auto_paths:
            print("regression database not detected")
            print(plan.to_dict())
            return 1
        if "scripts/__pycache__" not in auto_paths:
            print("cache directory not detected")
            print(plan.to_dict())
            return 1
        if "app/services/example.py" in auto_paths:
            print("source file should not be auto moved")
            print(plan.to_dict())
            return 1
        if "app/services/example.py" in review_paths:
            print("untracked source should not be reported as tracked legacy marker")
            print(plan.to_dict())
            return 1

        result = apply_trash_plan(root=root, plan=plan, label="regression")
        manifest = Path(result["manifest_path"])
        if result["moved_count"] != 2 or not manifest.exists():
            print("apply did not move expected candidates")
            print(result)
            return 1
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        moved_from = {item["from"] for item in payload["moved"]}
        if moved_from != {"data/book2-style-flow-regression.db", "scripts/__pycache__"}:
            print("manifest moved paths mismatch")
            print(payload)
            return 1
        if source.exists() is False:
            print("source file was moved unexpectedly")
            return 1

        backup_dir = root / "data/backups"
        backup_dir.mkdir(parents=True)
        old_backup = backup_dir / "novel-20260101-000000-restart-book-2-from-ch1.db"
        new_backup = backup_dir / "novel-20260616-000000-restart-book-2-from-ch1.db"
        old_backup.write_text("old", encoding="utf-8")
        new_backup.write_text("new", encoding="utf-8")
        old_time = time.time() - 10 * 86400
        os.utime(old_backup, (old_time, old_time))
        trash_batch = root / "data/trash/20260101-000000-old"
        trash_batch.mkdir(parents=True)
        (trash_batch / "artifact.tmp").write_text("trash", encoding="utf-8")
        os.utime(trash_batch, (old_time, old_time))
        slim = build_auto_slim_plan(
            root=root,
            policy=SlimPolicy(
                max_workspace_bytes=1,
                backup_keep_latest=1,
                backup_retention_days=3,
                trash_retention_days=2,
            ),
        )
        slim_paths = {item.path for item in slim.candidates}
        if "data/backups/novel-20260101-000000-restart-book-2-from-ch1.db" not in slim_paths:
            print("old backup not detected by auto slim")
            print(slim.to_dict())
            return 1
        if "data/backups/novel-20260616-000000-restart-book-2-from-ch1.db" in slim_paths:
            print("latest backup should be retained")
            print(slim.to_dict())
            return 1
        if "data/trash/20260101-000000-old" not in slim_paths:
            print("old trash batch not detected")
            print(slim.to_dict())
            return 1
        slim_result = apply_auto_slim_plan(root=root, plan=slim, label="auto-slim-regression")
        if old_backup.exists():
            print("old backup should be quarantined")
            print(slim_result)
            return 1
        if not new_backup.exists():
            print("latest backup should remain in place")
            print(slim_result)
            return 1
        if trash_batch.exists():
            print("old trash batch should be purged by auto slim")
            print(slim_result)
            return 1
        if slim_result.get("purged_count") != 1:
            print("auto slim purge count mismatch")
            print(slim_result)
            return 1

    print("system-trash-regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
