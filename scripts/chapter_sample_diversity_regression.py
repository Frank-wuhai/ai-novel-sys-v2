from __future__ import annotations

import argparse
import json
from datetime import datetime

from app.models.entities import Book, Chapter, ChapterBrief, GenerationTask
from app.services.chapter_samples import TASK_TYPE_CHAPTER_SAMPLE
from app.services.chapter_samples import _sample_diversity_report
from app.db.session import session_scope
from app.services.chapter_samples import adopt_chapter_sample, latest_chapter_samples
from regression_db import isolated_database


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect latest chapter sample diversity.")
    parser.add_argument("--book-id", type=int, default=2)
    parser.add_argument("--chapter-number", type=int, default=1)
    parser.add_argument("--min-score", type=int, default=65)
    args = parser.parse_args()

    isolated_database("chapter-sample-diversity-regression")
    with session_scope() as session:
        book_id, chapter_number = _seed_sample_fixture(session)
        latest = latest_chapter_samples(
            session,
            book_id=book_id,
            chapter_number=chapter_number,
            limit=3,
        )
        adopted = adopt_chapter_sample(
            session,
            task_id=int(latest.get("task_id") or 0),
            sample_index=1,
            revision_mode="targeted",
        )
        adopted_brief = session.get(ChapterBrief, adopted.brief_id)
        adopted_text = "\n".join(
            [adopted_brief.goal or "", adopted_brief.required_beats or "", adopted_brief.constraints or ""]
        ) if adopted_brief else ""
        no_usable_report = _sample_diversity_report(_thin_distinct_samples())
    report = latest.get("diversity_report") or latest.get("fallback_diversity_report") or {}
    score = int(report.get("score") or 0)
    latest_failed = latest.get("status") == "failed"
    no_usable_guard_ok = no_usable_report.get("status") == "attention" and "no_usable_sample" in (
        no_usable_report.get("issues") or []
    )
    adoption_fingerprint_ok = (
        "写作指纹继承" in adopted_text
        and "视角距离" in adopted_text
        and "句段节奏" in adopted_text
        and "场景展开" in adopted_text
    )
    status = (
        "pass"
        if not latest_failed and score >= args.min_score and not report.get("issues") and no_usable_guard_ok and adoption_fingerprint_ok
        else "attention"
    )
    print(
        json.dumps(
            {
                "status": status,
                "book_id": book_id,
                "chapter_number": chapter_number,
                "task_id": latest.get("task_id"),
                "latest_task_status": latest.get("status"),
                "latest_error": latest.get("error", ""),
                "fallback_task_id": latest.get("fallback_task_id"),
                "score": score,
                "threshold": args.min_score,
                "diversity_report": report,
                "no_usable_guard": no_usable_report,
                "adoption_fingerprint_ok": adoption_fingerprint_ok,
                "attention_explanation": _attention_explanation(latest=latest, report=report, threshold=args.min_score),
                "trial_impact": "blocks_trial" if latest_failed else ("safe_to_trial_with_review" if status == "attention" else "safe_to_trial"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _seed_sample_fixture(session) -> tuple[int, int]:
    book = Book(title=f"sample-diversity-regression-{datetime.utcnow().timestamp()}", genre="真实武侠", target_platform="manual")
    session.add(book)
    session.flush()
    chapter = Chapter(book_id=book.id, chapter_number=1, title="第一章", status="draft")
    session.add(chapter)
    samples = [
        _sample_fixture(1, "药铺血账", "现场压力", "药铺账本和门闩逼主角选择"),
        _sample_fixture(2, "渡口错认", "规则误判", "路引、船夫和差役制造误判"),
        _sample_fixture(3, "灯下人情", "关系压力", "少年私心和死人账册推动选择"),
    ]
    task = GenerationTask(
        book_id=book.id,
        task_type=TASK_TYPE_CHAPTER_SAMPLE,
        status="completed",
        input_json=json.dumps({"chapter_number": 1}, ensure_ascii=False),
        output_json=json.dumps({"samples": samples, "gate_passed": True}, ensure_ascii=False),
    )
    session.add(task)
    session.flush()
    return book.id, chapter.chapter_number


def _sample_fixture(index: int, title: str, axis: str, opening_seed: str) -> dict:
    return {
        "index": index,
        "title": title,
        "exploration_axis": axis,
        "experiment_hypothesis": opening_seed,
        "direction": "主角在现场压力下主动选择并付出代价",
        "opening": opening_seed * 10,
        "scene_plan": ["现场异常", "人物试探", "章末新线索"],
        "difference_from_existing": f"{title}采用不同入口和压力源。",
        "anti_ai_flavor_strategy": "用具体物件和人物反应承载信息。",
        "pov_strategy": "贴住主角误判、观察和身体反应。",
        "precision_strategy": "只让推断来自可见证据。",
    }


def _thin_distinct_samples() -> list[dict]:
    return [
        {
            "index": 1,
            "title": "扫帚与落叶",
            "exploration_axis": "规则误判型",
            "experiment_hypothesis": "测试物理证据触发。",
            "direction": "观察证据。",
            "opening": "林默被扫堂腿撂倒，爬起来看见落叶绕着扫帚打旋。他盯住竹柄上的磨痕，觉得这场景有点熟。",
            "scene_plan": ["观察", "试探", "触发"],
            "difference_from_existing": "换成物理证据。",
            "anti_ai_flavor_strategy": "用物件写。",
            "pov_strategy": "贴住身体感受。",
            "precision_strategy": "判断来自可见证据。",
        },
        {
            "index": 2,
            "title": "欠条与饭钱",
            "exploration_axis": "利益交换型",
            "experiment_hypothesis": "测试欠条关系。",
            "direction": "写欠条换机会。",
            "opening": "报名桌后的人要三钱银子，林默摸了摸空口袋，只能拿出草稿纸，问能不能写欠条。",
            "scene_plan": ["报名", "写欠条", "交换"],
            "difference_from_existing": "换成利益交换。",
            "anti_ai_flavor_strategy": "用账册写。",
            "pov_strategy": "贴住窘迫。",
            "precision_strategy": "账目清楚。",
        },
        {
            "index": 3,
            "title": "后山与铜片",
            "exploration_axis": "信息悬疑型",
            "experiment_hypothesis": "测试升维线索。",
            "direction": "从铜片发现异常。",
            "opening": "柴房角落露出半截铜片，边缘发黑，背面刻着云中君三个小字。林默握在手里，掌心忽然一烫。",
            "scene_plan": ["劈柴", "拾铜片", "异常"],
            "difference_from_existing": "换成物件谜题。",
            "anti_ai_flavor_strategy": "用触感写。",
            "pov_strategy": "贴住疼痛。",
            "precision_strategy": "物件尺寸明确。",
        },
    ]


def _attention_explanation(*, latest: dict, report: dict, threshold: int) -> list[str]:
    reasons: list[str] = []
    if latest.get("status") == "failed":
        reasons.append(f"latest_sample_task_failed:{latest.get('latest_error') or latest.get('error') or ''}")
    score = int(report.get("score") or 0)
    if score < threshold:
        reasons.append(f"diversity_score_low:{score}<{threshold}")
    for issue in report.get("issues") or []:
        reasons.append(f"diversity_issue:{issue}")
    return reasons


if __name__ == "__main__":
    raise SystemExit(main())
