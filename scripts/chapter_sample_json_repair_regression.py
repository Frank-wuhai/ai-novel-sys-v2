from __future__ import annotations

import json

from app.db.session import session_scope
from app.llm.providers import LLMResponse
from app.models.entities import Book, Chapter, ChapterBrief, PlatformFeedback, StoryArc, StoryFoundation
from app.services import chapter_samples
from regression_db import isolated_database


class BrokenJsonProvider:
    name = "broken-json"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, prompt: str, **kwargs) -> LLMResponse:
        self.calls += 1
        return LLMResponse(
            text='{"samples":[{"index":1,"title":"坏小样" "opening":"少了逗号"}]}',
            provider=self.name,
            model=str(kwargs.get("model") or "broken-json-model"),
            request_id=f"broken-json-{self.calls}",
            elapsed_ms=1,
        )


def main() -> int:
    isolated_database("chapter-sample-json-repair-regression")
    failures: list[str] = []
    provider = BrokenJsonProvider()
    original_get_provider = chapter_samples.get_provider
    chapter_samples.get_provider = lambda dry_run=False: provider
    try:
        with session_scope() as session:
            book = Book(title="sample json repair", genre="test", target_platform="manual")
            session.add(book)
            session.flush()
            _approve_skeleton(session, book_id=book.id)
            chapter = Chapter(book_id=book.id, chapter_number=1, title="第1章", status="draft")
            session.add(chapter)
            session.flush()
            session.add(
                ChapterBrief(
                    chapter_id=chapter.id,
                    goal="第1章小样测试",
                    required_beats="开场压力；人物选择；章末钩子。",
                    constraints="3000-4500 中文字符。",
                    status="ready",
                )
            )
            session.flush()
            task = chapter_samples.generate_chapter_samples(
                session,
                book_id=book.id,
                chapter_number=1,
                sample_count=3,
                dry_run=False,
                max_attempts=3,
            )
            output = json.loads(task.output_json or "{}")
            attempts = output.get("attempts") or []
            if task.status != "failed":
                failures.append(f"task_not_failed:{task.status}")
            if output.get("error_category") != "json_repair_failed":
                failures.append(f"wrong_error_category:{output}")
            if output.get("retryable") is not True:
                failures.append(f"not_retryable:{output}")
            if len(attempts) != 3:
                failures.append(f"attempts_not_recorded:{attempts}")
            if not all(item.get("status") == "parse_failed" for item in attempts):
                failures.append(f"attempt_status_not_parse_failed:{attempts}")
    finally:
        chapter_samples.get_provider = original_get_provider

    if failures:
        for failure in failures:
            print(failure)
        return 1
    print("chapter-sample-json-repair-regression: PASS")
    return 0


def _approve_skeleton(session, *, book_id: int) -> None:
    values = {
        "premise": "测试 premise",
        "reader_promise": "测试 reader promise",
        "world_engine": "测试 world",
        "protagonist_engine": "测试 protagonist",
        "conflict_engine": "测试 conflict",
        "arc_goal": "测试 arc goal",
        "arc_climax": "测试 arc climax",
        "arc_turn": "测试 arc turn",
    }
    session.add(
        StoryFoundation(
            book_id=book_id,
            premise=values["premise"],
            reader_promise=values["reader_promise"],
            world_engine=values["world_engine"],
            protagonist_engine=values["protagonist_engine"],
            conflict_engine=values["conflict_engine"],
        )
    )
    session.add(StoryArc(book_id=book_id, arc_number=1, start_chapter=1, end_chapter=5, goal=values["arc_goal"], climax=values["arc_climax"], turn=values["arc_turn"]))
    for key, value in values.items():
        session.add(PlatformFeedback(book_id=book_id, platform="system", metric_name="skeleton_approval", metric_value=key, raw_text=value))


if __name__ == "__main__":
    raise SystemExit(main())
