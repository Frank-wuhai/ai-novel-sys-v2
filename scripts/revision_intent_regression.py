from __future__ import annotations

import json

from app.db.session import session_scope
from app.models.entities import QualityReport
from app.services.feedback import create_feedback_adjustment, record_platform_feedback, submit_revision_suggestion
from app.services.planning import plan_chapters
from app.services.production import create_book, create_chapter_brief, create_manual_chapter_version
from app.services.revision_intent import (
    REVISION_MODE_FRESH,
    REVISION_MODE_LOCAL_PATCH,
    REVISION_MODE_POLISH,
    REVISION_MODE_REWRITE,
    REVISION_MODE_TARGETED,
    decide_revision_intent,
    extract_revision_decision,
)
from regression_db import isolated_database


def main() -> int:
    failures: list[str] = []
    isolated_database("revision-intent-regression")

    with session_scope() as session:
        book = create_book(session, title="Revision Intent Regression", genre="玄幻都市", platform="manual")
        create_chapter_brief(
            session,
            book_id=book.id,
            chapter_number=1,
            goal="主角发现能力收益和记忆代价绑定。",
            required_beats="压力,选择,代价,章末钩子",
            constraints="保持已登记Canon。",
        )
        version = create_manual_chapter_version(
            session,
            book_id=book.id,
            chapter_number=1,
            title="第1章",
            content="主角站在门口，发现异常，却一直被旁人推着走。章末没有新的危险。",
        )
        session.add(
            QualityReport(
                chapter_version_id=version.id,
                score=58,
                passed=False,
                report=json.dumps(
                    {
                        "status": "FAIL",
                        "score": 58,
                        "hard_gate": {"status": "PASS"},
                        "dimensions": {
                            "author_intent": 62,
                            "brief_coverage": 61,
                            "hook_strength": 35,
                            "choice_and_cost": 40,
                            "production_standard": 72,
                        },
                        "issues": [],
                    },
                    ensure_ascii=False,
                ),
            )
        )
        session.flush()

        cases = [
            ("这句话太像说明书，只改这一句。", REVISION_MODE_LOCAL_PATCH),
            ("文风太AI，对白不自然，润色一下。", REVISION_MODE_POLISH),
            ("章末钩子不够明确，主角还是太被动。", REVISION_MODE_TARGETED),
            ("这一章没写出我要的感觉，读者体验整体不对。", REVISION_MODE_REWRITE),
            ("完全偏了，不要参考旧稿，按最新设定重新来。", REVISION_MODE_FRESH),
        ]
        for text, expected in cases:
            decision = decide_revision_intent(session, book_id=book.id, chapter_number=1, suggestion_text=text)
            if decision.mode != expected:
                failures.append(f"route:{expected}:got:{decision.mode}")
            if not decision.reason or not decision.escalation_rule:
                failures.append(f"route_missing_explain:{expected}")

        feedback = record_platform_feedback(
            session,
            book_id=book.id,
            chapter_number=1,
            platform="manual",
            metric_name="comment",
            raw_text="读者反馈：章末钩子可以更明确。",
        )
        adjustment = create_feedback_adjustment(
            session,
            book_id=book.id,
            target_chapter_number=1,
            feedback_ids=[feedback.id],
        )
        feedback_decision = extract_revision_decision(adjustment.adjustment_text)
        if feedback_decision.get("处理强度") != REVISION_MODE_TARGETED:
            failures.append("feedback_adjustment_not_auto_targeted")
        if "修订方向:" not in adjustment.adjustment_text:
            failures.append("feedback_adjustment_missing_machine_suggestion")

        _feedback, editorial_adjustment, brief, version = submit_revision_suggestion(
            session,
            book_id=book.id,
            chapter_number=1,
            suggestion_text="主角还是太被动，章末钩子不够明确。",
            platform="editorial_revision",
        )
        editorial_decision = extract_revision_decision(editorial_adjustment.adjustment_text)
        if editorial_decision.get("处理强度") != REVISION_MODE_TARGETED:
            failures.append("editorial_suggestion_not_auto_targeted")
        if "revision_mode:targeted" not in brief.constraints:
            failures.append("revision_brief_missing_auto_mode")
        if version and version.status != "needs_revision":
            failures.append("latest_version_not_reopened")

        readable_book = create_book(session, title="Approval Reopen Regression", genre="武侠网游", platform="manual")
        create_chapter_brief(
            session,
            book_id=readable_book.id,
            chapter_number=1,
            goal="主角获得内测资格并进入万象江湖。",
            required_beats="进入游戏,触发桥段,获得奖励,留下现实同步钩子",
            constraints="保持武侠向仙侠升维路线。",
        )
        readable_version = create_manual_chapter_version(
            session,
            book_id=readable_book.id,
            chapter_number=1,
            title="第1章",
            content="主角进入万象江湖，触发剧情演绎任务，并在结尾察觉现实同步。",
        )
        readable_version.status = "reviewed_pass"
        session.add(
            QualityReport(
                chapter_version_id=readable_version.id,
                score=82,
                passed=True,
                report=json.dumps(
                    {
                        "passed": True,
                        "score": 82,
                        "hard_gate": {"passed": True},
                        "llm_review": {"status": "completed", "verdict": "pass", "score": 82},
                    },
                    ensure_ascii=False,
                ),
            )
        )
        session.flush()
        _feedback, _adjustment, readable_brief, reopened_version = submit_revision_suggestion(
            session,
            book_id=readable_book.id,
            chapter_number=1,
            suggestion_text="开头还不够吸引人，增强进入游戏后的第一处具体奇遇。",
            platform="editorial_revision",
            revision_mode="targeted",
        )
        planned = plan_chapters(session, book_id=readable_book.id, start=1, count=1)[0]
        if reopened_version and reopened_version.status != "needs_revision":
            failures.append(f"approval_reopen_status:{reopened_version.status}")
        if readable_brief.status != "revision_ready":
            failures.append(f"approval_reopen_brief_status:{readable_brief.status}")
        if planned.next_action != "revise_chapter":
            failures.append(f"approval_reopen_plan:{planned.next_action}:{planned.latest_version_status}")
        planned_again = plan_chapters(session, book_id=readable_book.id, start=1, count=1)[0]
        if planned_again.next_action != "revise_chapter":
            failures.append(f"approval_reopen_second_plan:{planned_again.next_action}:{planned_again.latest_version_status}")
        if readable_brief.status != "revision_ready":
            failures.append(f"approval_reopen_brief_superseded:{readable_brief.status}")

    print(json.dumps({"status": "fail" if failures else "pass", "failures": failures}, ensure_ascii=False, indent=2))
    return 1 if failures else 0

if __name__ == "__main__":
    raise SystemExit(main())
