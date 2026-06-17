from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.system_trash import SlimPolicy, apply_auto_slim_plan, apply_trash_plan, build_auto_slim_plan, build_trash_plan


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan and quarantine disposable AI Novel System artifacts.")
    parser.add_argument("--apply", action="store_true", help="move safe candidates into data/trash/<timestamp>")
    parser.add_argument("--auto-slim", action="store_true", help="apply automatic retention policy for backups/trash/regression artifacts")
    parser.add_argument("--include-logs", action="store_true", help="include old data/logs/*.log files")
    parser.add_argument("--log-retention-days", type=int, default=7, help="only move logs at least this old")
    parser.add_argument("--max-workspace-mb", type=int, default=450, help="auto-slim target workspace size in MB")
    parser.add_argument("--backup-keep-latest", type=int, default=5, help="production backups to keep")
    parser.add_argument("--backup-retention-days", type=int, default=3, help="production backup minimum retention")
    parser.add_argument("--trash-retention-days", type=int, default=2, help="old trash batch retention")
    parser.add_argument("--label", default="", help="optional label for the trash folder when --apply is used")
    parser.add_argument("--json", action="store_true", help="print full JSON plan/result")
    parser.add_argument(
        "--model-review",
        action="store_true",
        help="ask the configured review model to explain review-only candidates; never changes move decisions",
    )
    args = parser.parse_args()

    if args.auto_slim:
        policy = SlimPolicy(
            max_workspace_bytes=max(1, args.max_workspace_mb) * 1024 * 1024,
            backup_keep_latest=max(0, args.backup_keep_latest),
            backup_retention_days=max(0, args.backup_retention_days),
            trash_retention_days=max(0, args.trash_retention_days),
            include_logs=True,
            log_retention_days=max(0, args.log_retention_days),
        )
        plan = build_auto_slim_plan(root=ROOT, policy=policy)
    else:
        plan = build_trash_plan(
            root=ROOT,
            include_logs=args.include_logs,
            log_retention_days=args.log_retention_days,
        )
    if args.apply:
        result = (
            apply_auto_slim_plan(root=ROOT, plan=plan, label=args.label or "auto-slim")
            if args.auto_slim
            else apply_trash_plan(root=ROOT, plan=plan, label=args.label)
        )
        payload = {"status": "applied", "result": result, "plan": plan.to_dict()}
    else:
        payload = {"status": "dry_run", "plan": plan.to_dict()}

    if args.model_review:
        payload["model_review"] = _model_review(plan)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        summary = payload["plan"]["summary"]
        print(f"status={payload['status']}")
        print(f"auto_count={summary['auto_count']}")
        print(f"auto_size_bytes={summary['auto_size_bytes']}")
        print(f"review_count={summary['review_count']}")
        if args.apply:
            print(f"trash_dir={payload['result']['trash_dir']}")
            print(f"manifest_path={payload['result']['manifest_path']}")
            print(f"moved_count={payload['result']['moved_count']}")
            if args.auto_slim:
                print(f"purged_count={payload['result'].get('purged_count', 0)}")
                print(f"purged_size_bytes={payload['result'].get('purged_size_bytes', 0)}")
        if args.model_review:
            review = payload["model_review"]
            print(f"model_review_status={review['status']}")
            if review.get("path"):
                print(f"model_review_path={review['path']}")
            if review.get("error"):
                print(f"model_review_error={review['error']}")
        for item in plan.candidates[:20]:
            print(f"AUTO\t{item.category}\t{item.size_bytes}\t{item.path}\t{item.reason}")
        for item in plan.review_only[:20]:
            print(f"REVIEW\t{item.category}\t{item.path}\t{item.reason}")
    return 0


def _model_review(plan) -> dict:
    if not plan.review_only:
        return {"status": "skipped", "reason": "no review-only candidates"}
    try:
        from app.core.config import settings
        from app.llm.providers import ArkOpenAIProvider

        provider = ArkOpenAIProvider(timeout=30)
        review_items = [
            {
                "path": item.path,
                "category": item.category,
                "reason": item.reason,
                "size_bytes": item.size_bytes,
            }
            for item in plan.review_only[:30]
        ]
        prompt = (
            "你是代码库治理审阅员。下面是自动瘦身系统不敢自动移动、只标记为 review-only 的对象。\n"
            "请只输出 JSON，字段为 summary、safe_actions、risks、do_not_auto_delete。\n"
            "要求：不要建议直接删除源码；只能建议人工合并、重构、归档或保留。\n"
            f"对象列表：{json.dumps(review_items, ensure_ascii=False)}"
        )
        response = provider.generate(
            prompt,
            max_tokens=900,
            temperature=0,
            model=settings.llm_review_model,
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}

    out_dir = ROOT / "outputs" / "system_trash_model_reviews"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"review-{stamp}.json"
    payload = {
        "status": "completed",
        "provider": response.provider,
        "model": response.model,
        "request_id": response.request_id,
        "review_text": response.text,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": "completed", "path": str(path.relative_to(ROOT)), "model": response.model}


if __name__ == "__main__":
    raise SystemExit(main())
