"""一致性校验门（2026-07-28 建立）

根治 book2 推荐失败诊断中暴露的"分单元生成→拼接无全局一致性校验层"架构缺口。
三类断层检测，全部产出可作为 hard_issue 的字符串：

1. name_drift（跨章名字漂移）——最致命
   canon 主角名（authority_terms.protagonists）已确定时，若正文出现"竞争性别名"
   （与 canon 名共享姓氏但名不同，如 canon=林北 而正文频繁出现 林默），判漂移。
   book2 ch1 v188 正是此症：林默23次 / 林北5次，canon=林北。

2. sentence_repeat（章内整句重复）
   同章内，长度 ≥ MIN_REPEAT_LEN 的句子（按中文标点断句）完全重复出现 ≥2 次。
   book2 ch1 原症：整句"余光扫过山门墙根——一把竹扫帚斜靠在青砖上，帚尖沾着碎叶和泥。"重复。

3. state_contradiction（相邻语句状态矛盾）
   紧邻的"说话/发声"与"沉默/不答"矛盾对。
   book2 ch1 原症："挤出声音来：'……你怎么知道。'"下一句"门卫没答话，转身往里走"。

设计原则：
- 纯规则、零 LLM、确定性（对齐用户"消除随机性"偏好）。
- 只挑高置信度硬断层，宁可漏报不可误报（误伤已发布干净章会引发不必要重写）。
- 可独立 CLI 跑（全量扫描），也可被 quality.evaluate_chapter 调用（生成回路 hard_issue）。
"""
from __future__ import annotations

import re

# 中文断句标点
_SENT_SPLIT = re.compile(r"[。！？!?…]+|\n+")
# 整句重复的最小长度（短句如"他愣了一下"天然会重复，不算断层）
MIN_REPEAT_LEN = 12
# 名字漂移：竞争别名出现次数达到此比例才判定（防止一次笔误误伤）
DRIFT_MIN_HITS = 3


def _chinese_sentences(text: str) -> list[str]:
    """按中文标点断句，返回去空白后的非空句子。"""
    out = []
    for seg in _SENT_SPLIT.split(text or ""):
        seg = seg.strip()
        if seg:
            out.append(seg)
    return out


def _strip_dialogue(text: str) -> str:
    """遮蔽引号内对话，只保留旁白供重复检测。

    对话台词的合理复现（排练→实战、回忆→现实、口号呼应）是正当文学手法，
    不应判重复。旁白（叙述/动作）的逐字重复才是拼接 bug。
    遮蔽中文双引号 “…” 与直角引号 「…」『…』内的内容。
    """
    text = re.sub(r"\u201c[^\u201d]*\u201d", "", text)  # “ … ”
    text = re.sub(r"\u300c[^\u300d]*\u300d", "", text)  # 「 … 」
    text = re.sub(r"\u300e[^\u300f]*\u300f", "", text)  # 『 … 』
    return text


def check_sentence_repeat(text: str) -> list[str]:
    """章内整句重复检测（仅旁白）。返回 issue 字符串列表。

    先遮蔽引号内对话，避免"排练→实战"等对话呼应误报（book4 ch11 即此类）。
    """
    issues: list[str] = []
    seen: dict[str, int] = {}
    for sent in _chinese_sentences(_strip_dialogue(text)):
        # 去掉句内空白再比对（防排版差异漏检）
        key = re.sub(r"\s+", "", sent)
        if len(key) < MIN_REPEAT_LEN:
            continue
        seen[key] = seen.get(key, 0) + 1
    for key, cnt in seen.items():
        if cnt >= 2:
            snippet = key[:20] + ("…" if len(key) > 20 else "")
            issues.append(f"sentence_repeat: '{snippet}' x{cnt}")
    return issues


def check_name_drift(text: str, protagonists: list[str] | None) -> list[str]:
    """跨章名字漂移检测。

    canon 主角名已确定时，扫描正文中与 canon 名共享姓氏但名不同的"竞争别名"。
    典型：canon=林北，正文出现 林X（X≠北）≥DRIFT_MIN_HITS 次 → 漂移。
    """
    issues: list[str] = []
    prot = [p for p in (protagonists or []) if p and isinstance(p, str)]
    if not prot:
        return issues
    for canon in prot:
        if len(canon) < 2:
            continue
        surname = canon[0]
        # 找所有"同姓双字名"：姓 + 单字（覆盖绝大多数中文名）
        # 注意：canon 可能是三字名，这里只对二字名做严格漂移检测，降低误伤
        if len(canon) != 2:
            continue
        canon_hits = len(re.findall(re.escape(canon), text))
        # 竞争别名：同姓 + 非 canon 尾字
        rivals: dict[str, int] = {}
        for m in re.finditer(re.escape(surname) + r"([\u4e00-\u9fff])", text):
            full = surname + m.group(1)
            if full == canon:
                continue
            rivals[full] = rivals.get(full, 0) + 1
        for rival, cnt in rivals.items():
            # 竞争别名出现次数达标，且不明显少于 canon（排除偶发同姓配角）
            if cnt >= DRIFT_MIN_HITS and cnt >= canon_hits * 0.3:
                issues.append(
                    f"name_drift: canon='{canon}'({canon_hits}) vs rival='{rival}'({cnt})"
                )
    return issues


# 状态矛盾对：(说话/发声动作正则, 紧随的沉默/不答正则)
_SPEAK_PAT = re.compile(
    r"(挤出声音|开口|说道|回答|答道|应道|吐出|说出|喊|问道|反问|低声道|沉声道)"
)
_SILENT_PAT = re.compile(r"(没答话|没有答话|没吭声|没出声|没说话|沉默不语|不发一言|默不作声)")


def check_state_contradiction(text: str) -> list[str]:
    """相邻语句状态矛盾检测：'说话'紧邻'不答'。

    只检测紧邻（同一句或相邻句）的说话→沉默矛盾，高置信度。
    """
    issues: list[str] = []
    sents = _chinese_sentences(text)
    for i in range(len(sents) - 1):
        a, b = sents[i], sents[i + 1]
        # A 句有说话动作，B 句紧接说"没答话/没出声"——矛盾
        if _SPEAK_PAT.search(a) and _SILENT_PAT.search(b):
            issues.append(
                f"state_contradiction: 说话'{a[:14]}…' 紧邻 沉默'{b[:14]}…'"
            )
    return issues


def evaluate_consistency(
    text: str,
    *,
    protagonists: list[str] | None = None,
) -> list[str]:
    """一致性校验门总入口。返回所有断层 issue（空列表=通过）。

    protagonists: canon 主角名列表（来自 authority_terms['protagonists']）。
    """
    issues: list[str] = []
    issues.extend(check_name_drift(text, protagonists))
    issues.extend(check_sentence_repeat(text))
    issues.extend(check_state_contradiction(text))
    return issues
