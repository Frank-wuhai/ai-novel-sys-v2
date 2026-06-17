from __future__ import annotations

import json
import sys
from pathlib import Path

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.session import session_scope
from app.models.entities import (
    Chapter,
    ChapterBrief,
    ChapterVersion,
    FeedbackAdjustment,
    GenerationTask,
    KnowledgeEmbedding,
    PlatformFeedback,
    StoryArc,
    StoryFoundation,
)
from app.services.production_gate import pending_skeleton_approval_labels
from app.services.canon import add_character, add_power_system, add_world_rule
from app.services.production import create_book, create_foundation, seed_prompts
from regression_db import isolated_database
from scripts.run_local_dashboard import _perform_action


OLD_MARKERS = ("林默", "江湖志", "旧门派欠条")

NEW_SKELETON = {
    "premise": "陈默获得全真虚拟现实武侠网游《江湖》内测资格，核心卖点是剧情演绎能力；他在武侠向仙侠升维的世界里复刻桥段并让游戏实力同步现实。",
    "reader_promise": "看陈默用观察、选择和桥段复刻把真实江湖事件演成奇遇，同时承受人情、误判和现实同步的连锁代价。",
    "world_engine": "《江湖》先是高拟真武侠江湖，随后显露仙侠层级；能力不能强迫人物配合，失败会损失好感、错过奖励并改变后续因果。",
    "protagonist_engine": "陈默聪明但不万能，必须判断现场人物欲望、环境条件和桥段可复刻度，主动承担失败后的关系代价。",
    "conflict_engine": "长期外部压力来自五种发动机：人物关系债、门派资源交换、玩家竞争、现实同步异常、武侠向仙侠升维的规则惩罚。",
    "forbidden_rules": "禁止沿用旧主角名、旧游戏名、旧门派欠条和旧扫地桥段；禁止写成被门派追杀或被现实机构盯上的俗套逃亡线。",
    "style_guide": "武侠爽文兼网游奇遇冒险；场景要有烟火气、招式身法、人情交锋和轻松吐槽，不走阴冷悬疑和冷硬压抑笔触。",
    "aesthetic_profile": "题材主味是热闹真实的武侠网游奇遇，氛围明快有江湖烟火，允许危机但不压成悬疑旧案。",
    "story_dna": "每章以具体处境触发可演绎桥段，陈默先观察后选择，复刻越自然回报越高，失败会改变参演者好感与后续因果。",
    "volume_title": "第一卷 内测入江湖",
    "volume_summary": "陈默进入《江湖》，从武侠新手村事件认识剧情演绎能力，并看到游戏力量同步现实的第一道裂缝。",
    "arc_title": "入局与初演",
    "arc_goal": "前五章分别写内测资格、首次登录、人物关系债、资源交换选择、第一次失败代价和现实同步钩子，避免重复同一桥段。",
    "arc_climax": "陈默在一次真实江湖冲突中完成高复刻桥段，却发现奖励正在改变现实身体状态。",
    "arc_turn": "他意识到《江湖》不是单纯游戏，桥段演绎会把人物因果和现实后果一起带出来。",
}


def main() -> int:
    database_url = isolated_database("skeleton-context-reset-regression")
    with session_scope() as session:
        seed_prompts(session)
        book = create_book(session, title="Skeleton Context Reset Regression", genre="网游武侠", platform="番茄小说")
        create_foundation(
            session,
            book_id=book.id,
            premise="林默获得《江湖志》内测资格，并背上旧门派欠条。",
            reader_promise="林默在江湖志里还债升级。",
            world_engine="江湖志围绕旧门派欠条推进。",
            protagonist_engine="林默靠旧扫地桥段破局。",
            conflict_engine="旧门派欠条持续追债。",
        )
        add_character(session, book_id=book.id, name="林默", role="protagonist", ability="旧扫地桥段", background="江湖志旧设定")
        add_world_rule(session, book_id=book.id, category="旧世界", rule_text="江湖志和旧门派欠条必须保留。")
        add_power_system(session, book_id=book.id, name="旧外挂", rules="林默靠旧门派欠条触发奖励。")
        chapter = Chapter(book_id=book.id, chapter_number=1, title="旧第一章", status="needs_revision")
        session.add(chapter)
        session.flush()
        version = ChapterVersion(
            chapter_id=chapter.id,
            version_number=1,
            title="旧第一章",
            content="林默进入江湖志，拿到旧门派欠条。",
            status="needs_revision",
            source="regression",
        )
        session.add(version)
        brief = ChapterBrief(
            chapter_id=chapter.id,
            goal="继续写林默和江湖志。",
            required_beats="旧门派欠条必须出现。",
            constraints="不得改成陈默。",
            status="revision_ready",
        )
        session.add(brief)
        session.add(
            KnowledgeEmbedding(
                book_id=book.id,
                source_type="chapter",
                source_ref_id=str(chapter.id),
                source_label="old chapter",
                text="林默 江湖志 旧门派欠条",
                embedding_json="[0.1,0.2]",
                model="dry-run-hash",
                dimensions=2,
            )
        )
        old_feedback = PlatformFeedback(
            book_id=book.id,
            platform="dashboard",
            metric_name="skeleton_approval",
            metric_value="premise",
            raw_text="林默和江湖志旧骨架",
        )
        session.add(old_feedback)
        session.add(FeedbackAdjustment(book_id=book.id, target_chapter_number=1, adjustment_text="继续保留林默和江湖志", status="ready"))
        session.add(
            GenerationTask(
                book_id=book.id,
                task_type="draft_chapter",
                status="pending",
                input_json=json.dumps({"chapter_number": 1, "old": "林默 江湖志"}, ensure_ascii=False),
            )
        )
        session.flush()
        book_id = book.id

    with session_scope() as session:
        applied = _perform_action(
            session,
            {
                "action": "apply_story_skeleton_repair",
                "book_id": book_id,
                "current_skeleton": {},
                "repaired_skeleton": NEW_SKELETON,
            },
        )
        if applied.get("status") != "applied":
            print("apply_story_skeleton_repair did not apply")
            print(applied)
            return 1
        reset = applied.get("context_reset") or {}
        if reset.get("status") != "reset" or not reset.get("backup_path"):
            print("context reset was not reported")
            print(applied)
            return 1

        checks = {
            "active_briefs": "\n".join(
                f"{item.goal}\n{item.required_beats}\n{item.constraints}"
                for item in session.scalars(select(ChapterBrief).where(ChapterBrief.status.in_(["ready", "revision_ready"])))
            ),
            "memory": "\n".join(item.text for item in session.scalars(select(KnowledgeEmbedding).where(KnowledgeEmbedding.book_id == book_id))),
            "foundation": "\n".join(item.premise for item in session.scalars(select(StoryFoundation).where(StoryFoundation.book_id == book_id))),
            "skeleton_feedback": "\n".join(
                item.raw_text
                for item in session.scalars(
                    select(PlatformFeedback).where(
                        PlatformFeedback.book_id == book_id,
                        PlatformFeedback.metric_name == "skeleton_approval",
                    )
                )
            ),
        }
        for label, text in checks.items():
            if any(marker in text for marker in OLD_MARKERS):
                print(f"old marker survived in {label}")
                print(text)
                return 1
        if session.query(ChapterVersion).join(Chapter).filter(Chapter.book_id == book_id).count() != 0:
            print("old chapter versions were not cleaned")
            return 1
        if session.query(FeedbackAdjustment).filter_by(book_id=book_id).filter(FeedbackAdjustment.status != "superseded").count():
            print("old feedback adjustments were not superseded")
            return 1
        if session.query(GenerationTask).filter_by(book_id=book_id, status="pending").count():
            print("old pending generation task was not canceled")
            return 1
        if session.query(ChapterBrief).join(Chapter).filter(Chapter.book_id == book_id, ChapterBrief.status == "ready").count() < 1:
            print("clean chapter briefs were not recreated")
            return 1
        arc = session.scalars(select(StoryArc).where(StoryArc.book_id == book_id, StoryArc.arc_number == 1)).first()
        latest_approvals = {}
        for item in session.scalars(
            select(PlatformFeedback)
            .where(PlatformFeedback.book_id == book_id, PlatformFeedback.metric_name == "skeleton_approval")
            .order_by(PlatformFeedback.id.desc())
        ):
            latest_approvals.setdefault(item.metric_value, item.raw_text or "")
        if not arc or latest_approvals.get("arc_goal") != arc.goal:
            print("arc_goal approval does not match persisted story arc")
            print("approval=", latest_approvals.get("arc_goal"))
            print("arc.goal=", arc.goal if arc else "")
            return 1
        pending = pending_skeleton_approval_labels(session, book_id=book_id)
        if pending:
            print("production gate still has pending skeleton approvals")
            print(pending)
            return 1

    print("skeleton-context-reset-regression: PASS")
    print(f"database={database_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
