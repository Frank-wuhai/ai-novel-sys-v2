from __future__ import annotations

import argparse
import json

from app.db.session import session_scope
from app.services.chapter_samples import latest_chapter_samples


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect latest chapter sample diversity.")
    parser.add_argument("--book-id", type=int, default=2)
    parser.add_argument("--chapter-number", type=int, default=1)
    parser.add_argument("--min-score", type=int, default=65)
    args = parser.parse_args()

    with session_scope() as session:
        latest = latest_chapter_samples(
            session,
            book_id=args.book_id,
            chapter_number=args.chapter_number,
            limit=3,
        )
    report = latest.get("diversity_report") or latest.get("fallback_diversity_report") or {}
    score = int(report.get("score") or 0)
    latest_failed = latest.get("status") == "failed"
    status = "pass" if not latest_failed and score >= args.min_score and not report.get("issues") else "attention"
    print(
        json.dumps(
            {
                "status": status,
                "book_id": args.book_id,
                "chapter_number": args.chapter_number,
                "task_id": latest.get("task_id"),
                "latest_task_status": latest.get("status"),
                "latest_error": latest.get("error", ""),
                "fallback_task_id": latest.get("fallback_task_id"),
                "score": score,
                "threshold": args.min_score,
                "diversity_report": report,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
