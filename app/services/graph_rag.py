"""借鉴 2.0: GraphRAG 建库。

设计:
- graph_nodes: 节点(人物/事件/地点/物品/章节)
- graph_edges: 关系(出现在/导致/前往/持有/相邻)
- graph_node_chunks: 节点-章节关联

来源:从已发章节抽关系(简化版,纯规则提取,不用 LLM)。
后续可加 LLM 抽关系提升质量。
"""
from __future__ import annotations

import sqlite3
import re
from typing import Any

# 人物/地点/物品/事件 节点类型
NODE_TYPES = ("character", "location", "item", "event")


def ensure_graph_tables(con: sqlite3.Connection) -> None:
    """建 3 张 GraphRAG 表(idempotent)。"""
    cur = con.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS graph_nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id INTEGER NOT NULL,
            node_type TEXT NOT NULL,
            name TEXT NOT NULL,
            aliases TEXT,
            attributes TEXT,
            importance REAL DEFAULT 1.0,
            first_chapter INTEGER,
            last_chapter INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(book_id, node_type, name)
        );
        CREATE INDEX IF NOT EXISTS idx_gn_book_type
            ON graph_nodes(book_id, node_type);
        CREATE INDEX IF NOT EXISTS idx_gn_name
            ON graph_nodes(book_id, name);

        CREATE TABLE IF NOT EXISTS graph_edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id INTEGER NOT NULL,
            source_node_id INTEGER NOT NULL,
            target_node_id INTEGER NOT NULL,
            relation TEXT NOT NULL,
            weight REAL DEFAULT 1.0,
            first_chapter INTEGER,
            last_chapter INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_ge_source
            ON graph_edges(source_node_id);
        CREATE INDEX IF NOT EXISTS idx_ge_target
            ON graph_edges(target_node_id);
        CREATE INDEX IF NOT EXISTS idx_ge_book
            ON graph_edges(book_id, relation);

        CREATE TABLE IF NOT EXISTS graph_node_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id INTEGER NOT NULL,
            chapter_id INTEGER NOT NULL,
            chapter_number INTEGER NOT NULL,
            mention_count INTEGER DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_gnc_node
            ON graph_node_chunks(node_id);
        CREATE INDEX IF NOT EXISTS idx_gnc_chapter
            ON graph_node_chunks(chapter_id);
        """
    )
    con.commit()


# 简化人物名(从已发章节人名抽,book4 是 苏晨,book5 是 林渊,book6 是 顾晚)
# 注:实际需要从 characters 表读,这里硬编码常用名
COMMON_PROTAGONISTS = {
    "book4": ["苏晨", "周磊", "陆芷萱", "林清欢", "陈教授", "王胖子", "老张"],
    "book5": ["林渊", "陈海", "孙瑶", "张昊", "老鬼"],
    "book6": ["顾晚", "赵青", "李寻", "慕容复", "天山童姥"],
}

COMMON_LOCATIONS = {
    "book4": ["江城", "江城大学", "ATM机", "校门口", "西郊3号仓库", "工厂", "教室", "宿舍", "咖啡厅"],
    "book5": ["京城", "京城大学", "墓园", "古宅", "地下室", "会议室"],
    "book6": ["长安城", "华山派", "丐帮", "少林寺", "山庄", "客栈"],
}


def _upsert_node(
    con: sqlite3.Connection,
    *,
    book_id: int,
    node_type: str,
    name: str,
    chapter_number: int,
    attributes: str = "",
) -> int | None:
    """插入/更新节点,返回 node_id。"""
    if not name or len(name) > 50:
        return None
    cur = con.cursor()
    # 检查 existing
    r = cur.execute(
        "SELECT id, first_chapter, last_chapter, importance FROM graph_nodes "
        "WHERE book_id=? AND node_type=? AND name=?",
        (book_id, node_type, name),
    ).fetchone()
    if r:
        node_id, fc, lc, imp = r
        # 更新 last_chapter + importance++
        new_fc = min(fc, chapter_number) if fc else chapter_number
        new_lc = max(lc, chapter_number) if lc else chapter_number
        cur.execute(
            "UPDATE graph_nodes SET last_chapter=?, importance=? WHERE id=?",
            (new_lc, (imp or 1.0) + 0.1, node_id),
        )
        return node_id
    # 新建
    cur.execute(
        "INSERT INTO graph_nodes (book_id, node_type, name, first_chapter, last_chapter, importance, attributes) "
        "VALUES (?, ?, ?, ?, ?, 1.0, ?)",
        (book_id, node_type, name, chapter_number, chapter_number, attributes),
    )
    return cur.lastrowid


def _upsert_edge(
    con: sqlite3.Connection,
    *,
    book_id: int,
    source_id: int,
    target_id: int,
    relation: str,
    chapter_number: int,
) -> None:
    """插入/更新边(幂等)。"""
    if not source_id or not target_id or source_id == target_id:
        return
    cur = con.cursor()
    r = cur.execute(
        "SELECT id, weight, first_chapter, last_chapter FROM graph_edges "
        "WHERE book_id=? AND source_node_id=? AND target_node_id=? AND relation=?",
        (book_id, source_id, target_id, relation),
    ).fetchone()
    if r:
        eid, w, fc, lc = r
        new_fc = min(fc, chapter_number) if fc else chapter_number
        new_lc = max(lc, chapter_number) if lc else chapter_number
        cur.execute(
            "UPDATE graph_edges SET weight=?, last_chapter=? WHERE id=?",
            ((w or 1.0) + 0.1, new_lc, eid),
        )
    else:
        cur.execute(
            "INSERT INTO graph_edges (book_id, source_node_id, target_node_id, relation, weight, first_chapter, last_chapter) "
            "VALUES (?, ?, ?, ?, 1.0, ?, ?)",
            (book_id, source_id, target_id, relation, chapter_number, chapter_number),
        )


def _record_chunk(
    con: sqlite3.Connection,
    *,
    node_id: int,
    chapter_id: int,
    chapter_number: int,
) -> None:
    """节点-章节关联(幂等)。"""
    if not node_id:
        return
    cur = con.cursor()
    r = cur.execute(
        "SELECT id, mention_count FROM graph_node_chunks WHERE node_id=? AND chapter_id=?",
        (node_id, chapter_id),
    ).fetchone()
    if r:
        cur.execute(
            "UPDATE graph_node_chunks SET mention_count=mention_count+1 WHERE id=?",
            (r[0],),
        )
    else:
        cur.execute(
            "INSERT INTO graph_node_chunks (node_id, chapter_id, chapter_number, mention_count) "
            "VALUES (?, ?, ?, 1)",
            (node_id, chapter_id, chapter_number),
        )


def extract_graph_for_book(
    con: sqlite3.Connection,
    *,
    book_id: int,
    book_label: str,
) -> dict[str, int]:
    """从 book 全部已发章节抽 GraphRAG 节点/边。

    简化策略:用 COMMON_PROTAGONISTS / COMMON_LOCATIONS 词表扫章节内容。
    真 GraphRAG 需 LLM 抽,这里用规则先做基线。

    数据源:chapter_versions(每章 latest approved cv)——chapters.content 为空。
    """
    cur = con.cursor()
    # 找每章 latest approved cv(每个 chapter_id 取 version_number 最大的 approved)
    cur.execute(
        """
        SELECT cv.chapter_id, c.chapter_number, cv.content
        FROM chapter_versions cv
        JOIN chapters c ON c.id = cv.chapter_id
        WHERE c.book_id=? AND cv.status='approved' AND cv.content IS NOT NULL
          AND cv.version_number = (
            SELECT MAX(cv2.version_number) FROM chapter_versions cv2
            WHERE cv2.chapter_id = cv.chapter_id AND cv2.status='approved'
          )
        ORDER BY c.chapter_number
        """,
        (book_id,),
    )
    chapters = cur.fetchall()

    protag = COMMON_PROTAGONISTS.get(book_label, [])
    locs = COMMON_LOCATIONS.get(book_label, [])

    stats = {"nodes": 0, "edges": 0, "chunks": 0}

    for chapter_id, ch_num, content in chapters:
        if not content:
            continue
        # 抽人物节点
        chapter_char_ids: dict[str, int] = {}
        for name in protag:
            if name in content:
                nid = _upsert_node(
                    con,
                    book_id=book_id,
                    node_type="character",
                    name=name,
                    chapter_number=ch_num,
                    attributes="protagonist" if name == protag[0] else "supporting",
                )
                if nid:
                    chapter_char_ids[name] = nid
                    _record_chunk(con, node_id=nid, chapter_id=chapter_id, chapter_number=ch_num)
                    stats["nodes"] += 1
                    stats["chunks"] += 1

        # 抽地点节点
        chapter_loc_ids: dict[str, int] = {}
        for loc in locs:
            if loc in content:
                nid = _upsert_node(
                    con,
                    book_id=book_id,
                    node_type="location",
                    name=loc,
                    chapter_number=ch_num,
                )
                if nid:
                    chapter_loc_ids[loc] = nid
                    _record_chunk(con, node_id=nid, chapter_id=chapter_id, chapter_number=ch_num)
                    stats["nodes"] += 1
                    stats["chunks"] += 1

        # 抽边:人物-地点 (在/出现在)
        for cname, cid in chapter_char_ids.items():
            for lname, lid in chapter_loc_ids.items():
                _upsert_edge(
                    con,
                    book_id=book_id,
                    source_id=cid,
                    target_id=lid,
                    relation="appears_in",
                    chapter_number=ch_num,
                )
                stats["edges"] += 1

        # 抽边:人物-人物 (相邻,出现在同一章)
        char_list = list(chapter_char_ids.items())
        for i, (n1, id1) in enumerate(char_list):
            for n2, id2 in char_list[i + 1:]:
                _upsert_edge(
                    con,
                    book_id=book_id,
                    source_id=id1,
                    target_id=id2,
                    relation="co_appears",
                    chapter_number=ch_num,
                )
                stats["edges"] += 1

    con.commit()
    return stats


def query_graph_context(
    con: sqlite3.Connection,
    *,
    book_id: int,
    chapter_number: int,
    top_k: int = 5,
) -> str:
    """借鉴 2.0 GraphRAG 检索:返回前 N 章高频人物/地点/关系,注入 brief。

    格式:
    [GRAPH_CONTEXT]
    人物: 苏晨(ch1-3 出现 5 次), 周磊(ch1-2 出现 2 次)
    地点: 江城大学(ch1-3 出现 3 次)
    关系: 苏晨 -[appears_in]-> 江城大学, 苏晨 -[co_appears]-> 周磊
    """
    cur = con.cursor()
    # 找前 N 章高频人物(importance 降序)
    cur.execute(
        "SELECT name, node_type, importance, first_chapter, last_chapter "
        "FROM graph_nodes WHERE book_id=? AND importance > 1.0 "
        "ORDER BY importance DESC LIMIT ?",
        (book_id, top_k * 2),
    )
    nodes = cur.fetchall()
    if not nodes:
        return ""

    chars = [n for n in nodes if n[1] == "character"]
    locs = [n for n in nodes if n[1] == "location"]
    items = [n for n in nodes if n[1] == "item"]

    # 边:只查人物-地点、人物-人物
    char_ids = [n[0] for n in chars]  # 用名字反查 id
    cur.execute(
        "SELECT id FROM graph_nodes WHERE book_id=? AND name IN ({})".format(
            ",".join("?" * len(chars))
        ) if chars else "SELECT id FROM graph_nodes WHERE 1=0",
        (book_id,) + tuple(n[0] for n in chars) if chars else (book_id,),
    )
    char_id_set = {r[0] for r in cur.fetchall()}

    edges_text = []
    if char_id_set:
        cur.execute(
            "SELECT sn.name, tn.name, e.relation "
            "FROM graph_edges e "
            "JOIN graph_nodes sn ON sn.id = e.source_node_id "
            "JOIN graph_nodes tn ON tn.id = e.target_node_id "
            "WHERE e.book_id=? AND (sn.id IN ({0}) OR tn.id IN ({0})) "
            "ORDER BY e.weight DESC LIMIT 10".format(
                ",".join("?" * len(char_id_set))
            ),
            (book_id,) + tuple(char_id_set) + tuple(char_id_set),
        )
        for sn, tn, rel in cur.fetchall():
            edges_text.append(f"{sn} -[{rel}]-> {tn}")

    parts = ["[GRAPH_CONTEXT]"]
    if chars:
        parts.append("人物: " + ", ".join(
            f"{n[0]}(出现章节 {n[3]}-{n[4]})" for n in chars[:top_k]
        ))
    if locs:
        parts.append("地点: " + ", ".join(
            f"{n[0]}(出现章节 {n[3]}-{n[4]})" for n in locs[:3]
        ))
    if items:
        parts.append("物品: " + ", ".join(
            f"{n[0]}(出现章节 {n[3]}-{n[4]})" for n in items[:3]
        ))
    if edges_text:
        parts.append("关系: " + "; ".join(edges_text[:8]))

    return "\n".join(parts) + "\n[GRAPH_CONTEXT_END]"


if __name__ == "__main__":
    # 一次性建库 + 跑全 5 本
    con = sqlite3.connect("data/novel.db")
    ensure_graph_tables(con)
    book_map = {2: "book2", 3: "book3", 4: "book4", 5: "book5", 6: "book6"}
    total = {"nodes": 0, "edges": 0, "chunks": 0}
    for bid, blabel in book_map.items():
        s = extract_graph_for_book(con, book_id=bid, book_label=blabel)
        print(f"  {blabel} (book_id={bid}): {s}")
        for k in total:
            total[k] += s[k]
    print(f"\n总计: {total}")
    # 试查
    print("\n--- book4 ch1 graph context ---")
    print(query_graph_context(con, book_id=4, chapter_number=1))
