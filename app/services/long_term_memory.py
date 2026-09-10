"""长期记忆增量同步 · 在章节 approve 后由 hook 触发。

流程：
  1) 取该 chapter 最新 approved cv 对应的 chapter_exit_states 行
  2) 从中派生 3 张长期记忆表的增量：character_states / world_settings / foreshadows
  3) 如果 chapter_exit_states 还没有对应行（backfill_exit_states 还没跑），静默跳过

设计：无 LLM 调用·纯规则解析·失败不阻断 approve 主流程（try/except 兜底）。
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import text

from app.services.continuity import ensure_chapter_exit_state_table
from sqlalchemy.orm import Session

__all__ = ["sync_long_term_memory_for_version"]


def _split_facts(new_facts: str) -> list[str]:
    if not new_facts:
        return []
    parts = re.split(r"[；;。]\s*", new_facts)
    return [
        p.strip().strip("'\"“”").rstrip("；;。")
        for p in parts
        if p and len(p.strip()) >= 4
    ]


def _load_exit_state(session: Session, *, chapter_version_id: int) -> dict[str, Any] | None:
    ensure_chapter_exit_state_table(session)
    row = session.execute(
        text(
            """
            SELECT es.main_character_state, es.relationship_delta, es.plot_hook,
                   es.new_facts, es.physical_location, es.time_marker,
                   es.chapter_version_id,
                   c.id AS chapter_id, c.chapter_number, c.book_id
            FROM chapter_exit_states es
            JOIN chapter_versions cv ON cv.id = es.chapter_version_id
            JOIN chapters c ON c.id = cv.chapter_id
            WHERE es.chapter_version_id = :v
            ORDER BY es.id DESC LIMIT 1
            """
        ),
        {"v": chapter_version_id},
    ).fetchone()
    if not row:
        return None
    return {
        "main_character_state": row[0] or "",
        "relationship_delta": row[1] or "",
        "plot_hook": row[2] or "",
        "new_facts": row[3] or "",
        "physical_location": row[4] or "",
        "time_marker": row[5] or "",
        "chapter_version_id": row[6] or chapter_version_id,
        "chapter_id": row[7],
        "chapter_number": row[8],
        "book_id": row[9],
    }


def _get_protagonist_id(session: Session, *, book_id: int) -> int | None:
    row = session.execute(
        text(
            "SELECT id FROM characters WHERE book_id=:b AND role='protagonist' "
            "ORDER BY id LIMIT 1"
        ),
        {"b": book_id},
    ).fetchone()
    return row[0] if row else None


def _derive_exit_state_from_content(session: Session, *, chapter_version_id: int) -> dict[str, Any] | None:
    """★ 兜底:无 chapter_exit_states 时从 content 末段 200 字派生。

    生产管道(E 修)目前不调 backfill_exit_states —— 新章节常常没 exit_state。
    这里从 content 末段抓 200 字 + 抓首个物理位置/时间标记,保证长期记忆 + 分层摘要至少能落库。
    """
    try:
        row = session.execute(
            text("SELECT content, chapter_id FROM chapter_versions WHERE id = :cvid"),
            {"cvid": chapter_version_id},
        ).fetchone()
        if not row:
            return None
        content = row[0] or ""
        chapter_id = row[1]
        if not content:
            return None
        # 取末段 300 字
        tail = content[-300:].strip()
        import re as _re
        # 抓首个时间标记
        time_m = _re.search(r"(今天|次日|当晚|明天|后天|三天后|一周后|上午|下午|晚上|凌晨|清晨|傍晚)\S{0,8}", content)
        time_marker = time_m.group(0) if time_m else ""
        # 抓首个物理位置(从开头 500 字里找地点词)
        location_m = _re.search(r"(在|于)([\u4e00-\u9fff]{2,8}(大学|中学|学校|医院|公司|公寓|别墅|仓库|工厂|餐厅|酒店|街上|路上|门口|家里|房间|楼里|街口|站|广场|机场|车站|码头|巷|城|省|市|区|镇|村))", content[:500])
        physical_location = location_m.group(2) if location_m else ""
        # 取 chapter_id 拿到 chapter_number / book_id
        ch_row = session.execute(
            text("SELECT chapter_number, book_id FROM chapters WHERE id = :cid"),
            {"cid": chapter_id},
        ).fetchone()
        if not ch_row:
            return None
        return {
            "main_character_state": tail[:200],
            "relationship_delta": "",
            "plot_hook": tail[-100:],
            "new_facts": "",
            "physical_location": physical_location,
            "time_marker": time_marker,
            "chapter_version_id": chapter_version_id,
            "chapter_id": chapter_id,
            "chapter_number": ch_row[0],
            "book_id": ch_row[1],
        }
    except Exception:
        return None


def _upsert_character_state(session: Session, *, es: dict[str, Any]) -> int:
    protagonist_id = _get_protagonist_id(session, book_id=es["book_id"])
    if not protagonist_id or not es["main_character_state"]:
        return 0
    existing = session.execute(
        text(
            "SELECT 1 FROM character_states WHERE character_id=:c AND chapter_id=:ch LIMIT 1"
        ),
        {"c": protagonist_id, "ch": es["chapter_id"]},
    ).fetchone()
    if existing:
        return 0
    state_text = (
        f"[Ch{es['chapter_number']} @ {es['physical_location'] or '未知'}] "
        f"{es['main_character_state']}"
    )
    session.execute(
        text(
            "INSERT INTO character_states (character_id, chapter_id, state_text, source) "
            "VALUES (:c,:ch,:t,:s)"
        ),
        {
            "c": protagonist_id,
            "ch": es["chapter_id"],
            "t": state_text,
            "s": f"chapter_exit_state:ch{es['chapter_number']}",
        },
    )
    return 1


def _upsert_world_settings(session: Session, *, es: dict[str, Any]) -> int:
    facts = _split_facts(es["new_facts"])
    if not facts:
        return 0
    prefix = f"[book{es['book_id']}]"
    existing_titles = {
        r[0]
        for r in session.execute(
            text("SELECT title FROM world_settings WHERE title LIKE :p"),
            {"p": f"{prefix}%"},
        )
    }
    written = 0
    for fact in facts:
        title = f"{prefix}[ch{es['chapter_number']}] {fact}"
        if title in existing_titles:
            continue
        existing_titles.add(title)
        session.execute(
            text("INSERT INTO world_settings (title, content) VALUES (:t,:c)"),
            {"t": title, "c": fact},
        )
        written += 1
    return written


def _upsert_foreshadow(session: Session, *, es: dict[str, Any]) -> int:
    if not es["plot_hook"] or len(es["plot_hook"].strip()) < 8:
        return 0
    setup = f"[ch{es['chapter_number']}] {es['plot_hook'].strip()}"
    existing = session.execute(
        text(
            "SELECT 1 FROM foreshadows WHERE book_id=:b AND setup_text=:s LIMIT 1"
        ),
        {"b": es["book_id"], "s": setup},
    ).fetchone()
    if existing:
        return 0
    session.execute(
        text(
            "INSERT INTO foreshadows (setup_text, payoff_text, status, book_id) "
            "VALUES (:s, NULL, 'pending', :b)"
        ),
        {"s": setup, "b": es["book_id"]},
    )
    return 1


def sync_long_term_memory_for_version(
    session: Session, *, chapter_version_id: int
) -> dict[str, int]:
    """由 approve_chapter 调用 · 失败不抛出 · 尽力增量写入。

    返回 {'states': n, 'facts': n, 'hooks': n, 'summaries': n} 便于日志。
    """
    result = {"states": 0, "facts": 0, "hooks": 0, "summaries": 0}
    try:
        es = _load_exit_state(session, chapter_version_id=chapter_version_id)
        if not es:
            # ★ 兜底:无 chapter_exit_states 时从 content 末段 200 字派生(避免 backfill 缺失)
            es = _derive_exit_state_from_content(session, chapter_version_id=chapter_version_id)
        if not es:
            return result
        result["states"] = _upsert_character_state(session, es=es)
        result["facts"] = _upsert_world_settings(session, es=es)
        result["hooks"] = _upsert_foreshadow(session, es=es)
        result["summaries"] = _upsert_layered_summary(session, es=es)  # ★ 借鉴优化 2:分层摘要
    except Exception:
        # 兜底 · 长期记忆同步失败不能阻塞 approve，也不能回滚调用者事务。
        return result
    return result


def _upsert_layered_summary(session: Session, *, es: dict[str, Any]) -> int:
    """★ 借鉴优化 2:分层摘要(业界 5/5 都做)。

    业界惯例:近期 1-2 章全文 + 中期 3-10 章 summary + 长期 11+ 章 hook
    我们实现:每章落 3 层摘要
      - L1 全文(>0 字符时):给 LLM 复述用
      - L2 主线摘要(state_text ~ 200 字):给 LLM 推 3-10 章用
      - L3 hook 关键词(plot_hook):给 LLM 推 11+ 章用
    """
    try:
        # 表不存在就建(轻量容错,生产环境应 alembic 迁移)
        session.execute(text(
            "CREATE TABLE IF NOT EXISTS chapter_layered_summaries ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  book_id INTEGER NOT NULL,"
            "  chapter_id INTEGER NOT NULL,"
            "  chapter_version_id INTEGER,"
            "  chapter_number INTEGER NOT NULL,"
            "  layer TEXT NOT NULL,"
            "  summary TEXT NOT NULL,"
            "  token_estimate INTEGER,"
            "  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,"
            "  UNIQUE(book_id, chapter_id, layer)"
            ")"
        ))
        session.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_cls_book_chapter ON chapter_layered_summaries(book_id, chapter_number)"
        ))
        session.flush()
    except Exception:
        return 0

    book_id = es.get("book_id")
    chapter_id = es.get("chapter_id")
    chapter_number = es.get("chapter_number")
    # 优先用 es 自带的 chapter_version_id(从 SQL JOIN 取的)。
    chapter_version_id = es.get("chapter_version_id")
    if not (book_id and chapter_id and chapter_number):
        return 0

    written = 0
    try:
        # L2 中线摘要:state_text + location + time_marker 组合
        l2_parts = []
        if es.get("state_text"):
            l2_parts.append(f"主角:{es['state_text']}")
        if es.get("physical_location"):
            l2_parts.append(f"位置:{es['physical_location']}")
        if es.get("time_marker"):
            l2_parts.append(f"时间:{es['time_marker']}")
        if es.get("plot_hook"):
            l2_parts.append(f"钩子:{es['plot_hook']}")
        l2_summary = " | ".join(l2_parts) if l2_parts else ""
        if l2_summary:
            session.execute(text(
                "INSERT OR REPLACE INTO chapter_layered_summaries"
                " (book_id, chapter_id, chapter_version_id, chapter_number, layer, summary, token_estimate)"
                " VALUES (:book_id, :chapter_id, :cv_id, :chapter_number, 'L2_mid', :summary, :tokens)"
            ), {
                "book_id": book_id, "chapter_id": chapter_id, "cv_id": chapter_version_id,
                "chapter_number": chapter_number, "summary": l2_summary[:600],
                "tokens": len(l2_summary) // 2,  # 中文 1 token ~ 2 字
            })
            written += 1

        # L3 hook 关键词:从 plot_hook / new_facts 提取
        l3_parts = []
        if es.get("plot_hook"):
            l3_parts.append(es["plot_hook"])
        if es.get("new_facts"):
            l3_parts.append(es["new_facts"])
        l3_summary = " | ".join(l3_parts)[:200] if l3_parts else ""
        if l3_summary:
            session.execute(text(
                "INSERT OR REPLACE INTO chapter_layered_summaries"
                " (book_id, chapter_id, chapter_version_id, chapter_number, layer, summary, token_estimate)"
                " VALUES (:book_id, :chapter_id, :cv_id, :chapter_number, 'L3_hook', :summary, :tokens)"
            ), {
                "book_id": book_id, "chapter_id": chapter_id, "cv_id": chapter_version_id,
                "chapter_number": chapter_number, "summary": l3_summary,
                "tokens": len(l3_summary) // 2,
            })
            written += 1

        session.flush()
    except Exception:
        return written
    return written
