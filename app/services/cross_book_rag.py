"""借鉴 4.0: 跨本知识库检索。

retrieve_book_knowledge 已有但只查单本。
本模块:跨 book_id 检索——同作者同风格/同题材的章节互为参考。

业界对齐:webnovel-writer 跨本知识库 · ainovel-cli 多作家共享向量。

【2026-08-07 v25.10 改造】
数据源迁移:
- 5 本沷街书全删,knowledge_embeddings 已清空
- web_corpus 14 本 80% 是西幻/同人,题材错位
- 新增 knowledge_anchors 表:6 本公认爆款 18 章 4 万字(题材对位)
- 数据源优先级:knowledge_anchors(外部爆款)> web_corpus(只取题材对位的)> knowledge_embeddings(已废)

【2026-08-07 v25.14 改造】题材优先级动态化:
- 之前:BOOK6_PRIORITY_THEMES 硬编码("都市高武系统","无限流","都市系统")
- 现在:按 current_book_id + book.genre 动态决定题材优先级
- 修真/修真仙侠/武侠修真 → book7《造化之地》(古风武侠+仙侠+网游)
- 都市高武系统/无限流/都市系统 → book6(网游武侠系统)
"""
from __future__ import annotations

import sqlite3
from typing import Any


# 2026-08-07 v25.14 改造:按 book_id 动态决定题材优先级
BOOK_PRIORITY_THEMES = {
    6: ("都市高武系统", "无限流", "都市系统"),  # book6 网游武侠系统
    7: ("修真", "修真仙侠", "武侠修真"),  # book7《造化之地》古风武侠+仙侠+网游
}


def _load_anchors(
    con: sqlite3.Connection,
    *,
    current_book_id: int,
    query: str,
    top_k: int = 5,
    exclude_book_names: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """从 knowledge_anchors 加载 6 本真爆款锚点.

    简化:不依赖 query 字面匹配,直接按题材相关性 + 章节多样性选 top_k.
    题材相关性:
    - book6(网游武侠系统) 优先匹配 都市高武系统 / 都市系统 / 无限流
    - book7《造化之地》(古风武侠+仙侠+网游) 优先匹配 修真 / 修真仙侠 / 武侠修真
    """
    cur = con.cursor()
    # 检查表是否存在
    try:
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='knowledge_anchors'")
        if not cur.fetchone():
            return []
    except Exception:
        return []

    cur.execute("SELECT COUNT(*) FROM knowledge_anchors")
    if cur.fetchone()[0] == 0:
        return []

    # 题材匹配:按 current_book_id 动态决定优先级
    BOOK_PRIORITY = BOOK_PRIORITY_THEMES.get(current_book_id, ("修真", "修真仙侠", "武侠修真"))

    # 先取题材对位的
    theme_hits: list[dict] = []
    other_hits: list[dict] = []
    for theme in BOOK_PRIORITY:
        cur.execute("""
            SELECT book_name, chapter_no, chapter_title, opening_text, theme_tag
            FROM knowledge_anchors
            WHERE theme_tag = ? AND book_name NOT IN ({})
            ORDER BY chapter_no
        """.format(",".join("?" * len(exclude_book_names)) if exclude_book_names else "''"),
        (theme, *exclude_book_names))
        for r in cur.fetchall():
            theme_hits.append({
                "book_name": r[0],
                "chapter_no": r[1],
                "chapter_title": r[2],
                "snippet": r[3][:200] if r[3] else "",
                "theme": r[4],
                "score": 0.9,  # 题材对位高分
            })

    # 兜底:其他题材
    cur.execute("""
        SELECT book_name, chapter_no, chapter_title, opening_text, theme_tag
        FROM knowledge_anchors
        WHERE theme_tag NOT IN ({})
          AND book_name NOT IN ({})
        ORDER BY chapter_no
    """.format(
        ",".join("?" * len(BOOK_PRIORITY)),
        ",".join("?" * len(exclude_book_names)) if exclude_book_names else "''"
    ), (*BOOK_PRIORITY, *exclude_book_names))
    for r in cur.fetchall():
        other_hits.append({
            "book_name": r[0],
            "chapter_no": r[1],
            "chapter_title": r[2],
            "snippet": r[3][:200] if r[3] else "",
            "theme": r[4],
            "score": 0.5,
        })

    # 合并:题材对位优先,再补其他;去重(同书多章保留)
    results = []
    seen_books = set()
    for item in theme_hits + other_hits:
        if item["book_name"] in seen_books:
            continue
        seen_books.add(item["book_name"])
        results.append(item)
        if len(results) >= top_k:
            break
    return results


def retrieve_cross_book(
    con: sqlite3.Connection,
    *,
    current_book_id: int,
    query: str,
    top_k: int = 5,
    exclude_book_ids: tuple[int, ...] = (),
) -> list[dict[str, Any]]:
    """跨本检索(旧 API 保留)。

    简化实现:用 LIKE 字面匹配。
    数据源:knowledge_embeddings 表(已废,空表)。
    """
    cur = con.cursor()
    try:
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='knowledge_embeddings'")
        if not cur.fetchone():
            return []
    except Exception:
        return []

    cur.execute("SELECT COUNT(*) FROM knowledge_embeddings")
    if cur.fetchone()[0] == 0:
        return []
    cur.execute(
        """
        SELECT book_id, source_label, substr(text, 1, 200) as snippet
        FROM knowledge_embeddings
        WHERE book_id != ?
          AND book_id NOT IN ({})
          AND (text LIKE ? OR source_label LIKE ?)
        ORDER BY id DESC
        LIMIT ?
        """.format(
            ",".join("?" * len(exclude_book_ids)) if exclude_book_ids else "0"
        ),
        (current_book_id, *exclude_book_ids, f"%{query[:10]}%", f"%{query[:10]}%", top_k),
    )
    results = []
    for r in cur.fetchall():
        results.append({
            "book_id": r[0],
            "source_label": r[1],
            "snippet": r[2],
            "score": 0.5,
        })
    return results


def build_cross_book_block(
    con: sqlite3.Connection,
    *,
    current_book_id: int,
    chapter_number: int,
    chapter_title: str,
    prefer_web: bool = True,  # 2026-08-07 v25.10:启用,走 knowledge_anchors
    top_k: int = 4,
) -> str:
    """构造跨本对照块,注入 brief。

    【2026-08-07 v25.10】数据源改造:
    1. knowledge_anchors(6 本真爆款 18 章 4 万字)—— 题材对位优先 ⭐⭐⭐⭐⭐
    2. web_corpus(14 本番茄 OCR ch1)—— 兜底,只取题材对位的(去除西幻/同人污染)
    3. knowledge_embeddings(已清空)—— 跳过

    排除:book6 自己的锚点(避免回授)。

    格式:
    [CROSS_BOOK_REF]
    跨书章节开头风格参考(从外部公认爆款 + 自有跨本):
    - 《书名》 ch1: 「XXX」开头...
    - 《书名》 ch30: 「XXX」开头...
    [CROSS_BOOK_REF_END]
    """
    refs: list[tuple[str, int, str, str]] = []  # (bname, ch_no, snippet, theme)

    # 1) knowledge_anchors 优先(6 本真爆款)
    if prefer_web:
        anchors = _load_anchors(
            con,
            current_book_id=current_book_id,
            query=chapter_title or "",
            top_k=top_k,
            exclude_book_names=(),  # book6 不在锚点表,无需排除
        )
        for item in anchors:
            refs.append((item["book_name"], item["chapter_no"], item["snippet"], item["theme"]))

    # 2) web_corpus 兜底(去西幻/同人污染,只留题材对位的)
    if len(refs) < top_k:
        try:
            cur = con.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='web_corpus'")
            if cur.fetchone():
                cur.execute("SELECT COUNT(*) FROM web_corpus WHERE LENGTH(chapter_content) > 100")
                if cur.fetchone()[0] > 0:
                    cur.execute("""
                        SELECT book_name, chapter_no, substr(chapter_content, 1, 200)
                        FROM web_corpus
                        WHERE LENGTH(chapter_content) > 100
                          AND chapter_content NOT LIKE '%魔法%'
                          AND chapter_content NOT LIKE '%精灵%'
                          AND chapter_content NOT LIKE '%龙珠%'
                          AND chapter_content NOT LIKE '%骸%'
                        ORDER BY crawl_time DESC LIMIT ?
                    """, (top_k - len(refs),))
                    for bname, ch_no, snippet in cur.fetchall():
                        refs.append((bname or "未知", ch_no or 1, snippet, "番茄OCR"))
        except Exception:
            pass

    # 3) knowledge_embeddings 已废,跳过

    if not refs:
        return ""
    block = "[CROSS_BOOK_REF]\n跨书章节开头风格参考(从外部公认爆款 + 番茄榜对位题材):\n"
    for bname, ch_no, snippet, theme in refs:
        block += f"- 《{bname}》 ch{ch_no} ({theme}): {snippet[:120]}\n"
    block += "[CROSS_BOOK_REF_END]"
    return block


if __name__ == "__main__":
    con = sqlite3.connect("data/novel.db")
    print("=== book6 ch1 锚点测试 ===")
    block = build_cross_book_block(con, current_book_id=6, chapter_number=1, chapter_title="千亿项目")
    print(block or "(无锚点)")
    print(f"\n字符: {len(block)}")
