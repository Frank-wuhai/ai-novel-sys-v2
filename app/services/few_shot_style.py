"""借鉴 2.0: Few-shot 风格注入(代替 LoRA 微调)。

为什么不用 LoRA:你机器无 GPU + 云 GPU 需 ¥10-30/次。
Few-shot 替代方案:从 5 本已发章节抽 10-20 段金句,作为示例注入 brief。
LLM 看到示例 → 模仿风格(句长/对话/抽象词/对话模式)。

业界对齐:ainovel-cli 用 few-shot style priming。
"""
from __future__ import annotations

import random
import re
import sqlite3
from typing import Any


# 金句判定:长度 8-30 字 + 含名词/动词/语气词,排除白描
GOLDEN_LINE_PATTERNS = [
    re.compile(r"[\u4e00-\u9fff]{4,30}[。!！?？]"),  # 短句末有标点
]


def _is_golden(line: str) -> bool:
    """判定一行是不是"金句"(短句、含具体名词、有态度)。"""
    line = line.strip()
    if not line or len(line) < 6 or len(line) > 30:
        return False
    # 必须含至少 1 个动词
    if not re.search(r"[看见了到了想要给走说问拿放]", line):
        return False
    # 排除白描(全是"的""了")
    if line.count("的") > 3 or line.count("了") > 3:
        return False
    return True


def _extract_golden_from_anchors(
    con: sqlite3.Connection,
    *,
    max_lines: int = 15,
) -> list[str]:
    """2026-08-07 v25.10:从 knowledge_anchors(6 本真爆款)抽金句.

    比 web_corpus 强:
    - 题材对位(都市高武/无限流/都市系统优先)
    - 文本干净(笔趣阁 UTF-8,无 OCR 错字)
    - 18 章 4 万字,够抽
    """
    cur = con.cursor()
    try:
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='knowledge_anchors'")
        if not cur.fetchone():
            return []
    except Exception:
        return []
    cur.execute("SELECT COUNT(*) FROM knowledge_anchors")
    if cur.fetchone()[0] == 0:
        return []
    cur.execute("""
        SELECT book_name, chapter_no, opening_text, theme_tag
        FROM knowledge_anchors
        WHERE LENGTH(opening_text) > 100
        ORDER BY
            CASE WHEN theme_tag IN ('都市高武系统', '无限流', '都市系统') THEN 0 ELSE 1 END,
            book_name, chapter_no
    """)
    all_lines: list[str] = []
    for bname, ch_no, content, theme in cur.fetchall():
        if not content:
            continue
        for p in content.split("\n"):
            p = p.strip()
            if _is_golden(p):
                all_lines.append(p)
    if not all_lines:
        return []
    return random.sample(all_lines, min(max_lines, len(all_lines)))


def extract_golden_lines(
    con: sqlite3.Connection,
    *,
    book_id: int | None = None,
    max_lines: int = 15,
    min_per_book: int = 3,
    prefer_web: bool = True,  # 2026-08-07 v25.10:开回,走 knowledge_anchors
) -> list[str]:
    """抽金句,优先 knowledge_anchors 6 本真爆款(2026-08-07 用户拍).

    来源:
    1. knowledge_anchors(6 本公认爆款 18 章 4 万字)—— 优先 ⭐⭐⭐⭐⭐
    2. web_corpus(14 本番茄 OCR ch1)—— 兜底(只取题材对位的)
    3. 5 本已发 approved(烂章节 fallback)—— 跳过(book2/3/4/5 已删)

    策略:
    - prefer_web=True 且 knowledge_anchors 有数据 → 全从 knowledge_anchors 抽
    - 否则 fallback web_corpus(题材过滤)
    """
    if prefer_web:
        # 1) 优先 knowledge_anchors
        try:
            lines = _extract_golden_from_anchors(con, max_lines=max_lines)
            if lines:
                return lines
        except Exception:
            pass
        # 2) 兜底 web_corpus(题材过滤)
        try:
            cur = con.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='web_corpus'")
            if cur.fetchone():
                cur.execute("SELECT COUNT(*) FROM web_corpus WHERE LENGTH(chapter_content) > 100")
                n_web = cur.fetchone()[0]
                if n_web > 0:
                    return _extract_golden_from_web(con, max_lines=max_lines)
        except Exception:
            pass
    return []


def _extract_golden_from_web(
    con: sqlite3.Connection,
    *,
    max_lines: int = 15,
    exclude_themes: tuple[str, ...] = ("西幻", "异世界", "魔法", "亡灵", "骷髅", "炼金", "鹰人", "巨龙", "魔王", "刺客", "佣兵", "矮人", "精灵"),
) -> list[str]:
    """从 web_corpus 抽金句(优先 ch1 钩子+冲突句).

    2026-08-07:加 exclude_themes 过滤—— book4 都市题材,排除西幻/异世界金句.
    """
    cur = con.cursor()
    cur.execute("""
        SELECT chapter_content, book_name, book_desc FROM web_corpus
        WHERE LENGTH(chapter_content) > 100
        ORDER BY crawl_time DESC LIMIT 100
    """)
    rows = cur.fetchall()
    all_lines: list[str] = []
    for content, bname, bdesc in rows:
        if not content:
            continue
        # 题材过滤:书名/描述含西幻词则跳过
        if bname and any(t in bname for t in exclude_themes):
            continue
        if bdesc and any(t in bdesc for t in exclude_themes):
            continue
        for p in content.split("\n"):
            p = p.strip()
            if _is_golden(p):
                all_lines.append(p)
    if not all_lines:
        return []
    return random.sample(all_lines, min(max_lines, len(all_lines)))


def _extract_golden_from_own_books(
    con: sqlite3.Connection,
    *,
    book_id: int | None = None,
    max_lines: int = 15,
    min_per_book: int = 3,
) -> list[str]:
    """Fallback: 抽 5 本已发 approved 章节金句."""
    cur = con.cursor()
    if book_id:
        book_ids = [book_id]
    else:
        book_ids = [2, 3, 4, 5, 6]

    all_lines: list[str] = []
    per_book_count: dict[int, int] = {}

    for bid in book_ids:
        cur.execute(
            """
            SELECT cv.content
            FROM chapter_versions cv
            JOIN chapters c ON c.id = cv.chapter_id
            WHERE c.book_id=? AND cv.status='approved' AND cv.content IS NOT NULL
              AND cv.version_number = (
                SELECT MAX(cv2.version_number) FROM chapter_versions cv2
                WHERE cv2.chapter_id = cv.chapter_id AND cv2.status='approved'
              )
            ORDER BY c.chapter_number
            """,
            (bid,),
        )
        contents = [r[0] for r in cur.fetchall()]
        book_lines = []
        for content in contents[:30]:
            if not content:
                continue
            paras = content.split("\n")
            for p in paras:
                p = p.strip()
                if _is_golden(p):
                    book_lines.append(p)
        sampled = random.sample(book_lines, min(min_per_book, len(book_lines))) if book_lines else []
        all_lines.extend(sampled)
        per_book_count[bid] = len(sampled)
    if len(all_lines) > max_lines:
        all_lines = random.sample(all_lines, max_lines)
    return all_lines


def build_few_shot_block(
    con: sqlite3.Connection,
    *,
    book_id: int | None = None,
    max_lines: int = 12,
) -> str:
    """构造 few-shot 风格块,注入 brief。

    格式:
    [FEW_SHOT_STYLE]
    作者风格示例(避免风格漂移):
    1. 他说"这碗面不收钱,下回再来。"
    2. 妈的,是真...
    ...
    [FEW_SHOT_STYLE_END]
    """
    lines = extract_golden_lines(con, book_id=book_id, max_lines=max_lines)
    if not lines:
        return ""
    block = "[FEW_SHOT_STYLE]\n作者风格示例(避免风格漂移):\n"
    for i, line in enumerate(lines, 1):
        block += f"{i}. {line}\n"
    block += "[FEW_SHOT_STYLE_END]"
    return block


if __name__ == "__main__":
    con = sqlite3.connect("data/novel.db")
    block = build_few_shot_block(con, book_id=4, max_lines=10)
    print(block)
    print("\n--- 跨本 ---")
    block2 = build_few_shot_block(con, max_lines=15)
    print(block2)
