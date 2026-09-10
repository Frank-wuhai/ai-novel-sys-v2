"""借鉴 3.0: 多次生成 ×N + 评分选最优。

每章生成 N 次 → 走完整 5 修链路 → 取 evaluate_chapter 分数最高者落库。
其余 N-1 存为 superseded(留作 human review 时切换)。

业界对齐:webnovel-writer 用 3 次重试 + 评分选优。
"""
from __future__ import annotations

import copy
from typing import Any
from sqlalchemy.orm import Session

from app.services.chapter_drafting import draft_chapter


def _safe_save_failed_attempt(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    content: str,
    score: int,
    attempt: int,
) -> int | None:
    """存 N 次失败/低分尝试为 superseded cv,留作人工 review 时切换。

    返回 cv_id(失败返 None)。
    """
    try:
        from app.models.entities import Chapter, ChapterVersion
        from datetime import datetime
        ch = session.query(Chapter).filter(
            Chapter.book_id == book_id,
            Chapter.chapter_number == chapter_number,
        ).first()
        if not ch:
            return None
        # 找最新 version_number
        latest = session.query(ChapterVersion).filter(
            ChapterVersion.chapter_id == ch.id
        ).order_by(ChapterVersion.version_number.desc()).first()
        next_v = (latest.version_number + 1) if latest else 1
        cv = ChapterVersion(
            chapter_id=ch.id,
            version_number=next_v,
            title=f"(多生成尝试{attempt} score={score})",
            content=content[:5000] if content else "",
            status="superseded",
            source=f"multi_gen_attempt_{attempt}",
            created_at=datetime.utcnow(),
        )
        session.add(cv)
        session.flush()
        return cv.id
    except Exception:
        session.rollback()
        return None


def _quick_score(content: str) -> int:
    """快速打分(不走 LLM evaluate_chapter),用于 multi_gen 内部 N 次挑选。

    评分维度:
    - 字数 1800-2600 满分 30
    - 碎段率 ≤15% 满分 20
    - 数字一致性(无 9500 万断裂)满分 25
    - 段落数 60-120 满分 25
    """
    if not content:
        return 0
    score = 0
    # 字数
    han = sum(1 for c in content if "\u4e00" <= c <= "\u9fff")
    if 1800 <= han <= 2600:
        score += 30
    elif 1500 <= han < 1800 or 2600 < han <= 3000:
        score += 20
    elif 1200 <= han < 1500:
        score += 10
    # 碎段率
    paras = content.split("\n")
    short = sum(1 for p in paras if 0 < len(p.strip()) < 10)
    total = sum(1 for p in paras if p.strip())
    if total > 0:
        short_pct = short / total * 100
        if short_pct <= 15:
            score += 20
        elif short_pct <= 25:
            score += 10
    # 9500 万断裂
    if "9500万" in content or "9500 万" in content or "9,500" in content or "九千五" in content:
        score = max(0, score - 25)
    # 段落数
    if 60 <= total <= 120:
        score += 25
    elif 40 <= total < 60 or 120 < total <= 150:
        score += 15
    return score


def draft_chapter_multi_gen(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    n: int = 5,
    keep_attempts: bool = True,
) -> dict[str, Any]:
    """借鉴 3.0: 每章生成 N 次,取评分最高者落库为 approved。

    流程:
    1. 跑 N 次 draft_chapter(dry_run=False)
    2. 每条 content 用 _quick_score 打分
    3. 取最高分那条 → 改 status='approved'
    4. 其他 N-1 条 → status='superseded' + 标 source='multi_gen_attempt_{i}'
    5. 返回 {'best_attempt': i, 'best_score': s, 'all_attempts': [...], 'best_cv_id': id}

    成本 = N × 5 修 = N × ~3 LLM call/chapter。
    """
    if n < 1:
        n = 1
    if n > 10:
        n = 10  # 硬上限,避免单章成本爆

    attempts: list[dict[str, Any]] = []
    best_idx = 0
    best_score = -1
    best_content = ""
    best_cv_id = None

    for i in range(1, n + 1):
        try:
            cv = draft_chapter(
                session, book_id=book_id, chapter_number=chapter_number, dry_run=False
            )
            content = cv.content or ""
            score = _quick_score(content)
            attempts.append({
                "attempt": i,
                "cv_id": cv.id,
                "version_number": cv.version_number,
                "status": cv.status,
                "score": score,
                "content_length": len(content),
                "han_count": sum(1 for c in content if "\u4e00" <= c <= "\u9fff"),
            })
            if score > best_score:
                best_score = score
                best_idx = i
                best_content = content
                best_cv_id = cv.id
            print(
                f"[multi-gen] ch{chapter_number} 尝试{i}/{n} score={score} cv_id={cv.id} "
                f"status={cv.status} 汉字数={attempts[-1]['han_count']}",
                flush=True,
            )
        except Exception as e:
            attempts.append({
                "attempt": i,
                "error": str(e)[:200],
                "score": 0,
            })
            print(f"[multi-gen] ch{chapter_number} 尝试{i}/{n} 失败: {e}", flush=True)

    # 把非最佳 N-1 条标 superseded
    if keep_attempts and best_cv_id is not None:
        for att in attempts:
            if "cv_id" not in att:
                continue
            if att["cv_id"] == best_cv_id:
                continue
            try:
                from app.models.entities import ChapterVersion
                cv = session.get(ChapterVersion, att["cv_id"])
                if cv and cv.status != "superseded":
                    cv.status = "superseded"
                    cv.source = f"multi_gen_attempt_{att['attempt']}_score{att['score']}"
            except Exception:
                session.rollback()

    return {
        "best_attempt": best_idx,
        "best_score": best_score,
        "best_cv_id": best_cv_id,
        "all_attempts": attempts,
        "total_attempts": len(attempts),
    }
