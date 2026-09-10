from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class WorldLogicReport:
    score: int
    checks: dict[str, int]
    issues: list[str]
    examples: list[str]
    recommendations: list[str]

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "checks": self.checks,
            "issues": self.issues,
            "examples": self.examples,
            "recommendations": self.recommendations,
        }


def evaluate_world_logic(text: str) -> WorldLogicReport:
    body = str(text or "")
    examples: list[str] = []
    checks = {
        "character_knowledge_boundary": _character_knowledge_boundary_score(body, examples),
        "quest_source_plausibility": _quest_source_plausibility_score(body, examples),
        "npc_agency": _npc_agency_score(body, examples),
        "player_layer_intrusion": _player_layer_intrusion_score(body, examples),
    }
    score = round(sum(checks.values()) / len(checks))
    if checks.get("character_knowledge_boundary", 100) < 60 and checks.get("quest_source_plausibility", 100) < 60:
        score = min(score, 55)
    if checks.get("player_layer_intrusion", 100) < 60:
        score = min(score, 55)
    if checks.get("character_knowledge_boundary", 100) < 60:
        score = min(score, 55)
    issues = [f"{name}={value}" for name, value in checks.items() if value < 60]
    return WorldLogicReport(
        score=max(0, min(100, score)),
        checks=checks,
        issues=issues,
        examples=examples[:8],
        recommendations=_recommendations(checks),
    )


def _character_knowledge_boundary_score(text: str, examples: list[str]) -> int:
    score = 88
    dialogue_lines = _dialogue_lines(text)
    for line in dialogue_lines:
        compact = re.sub(r"\s+", "", line)
        if "系统" in compact and not _line_marks_inner_monologue_or_panel(compact):
            score -= 35
            examples.append("对白中直接说出“系统”，但未建立 NPC 理解系统/玩家机制。")
            break
    if re.search(r'[“\"]?[^。！？!?]{0,18}系统[^。！？!?]{0,18}(?:给我|指的路|让我|发布|任务)[^。！？!?]{0,20}[”\"]?', text):
        score -= 30
        examples.append("主角把系统/任务机制当作可对世界内人物解释的事实，知识边界越界。")
    leak_reasons = game_world_meta_leak_reasons(text)
    if leak_reasons:
        score -= 35
        examples.extend(leak_reasons[:3])
    return max(0, min(100, score))


def _quest_source_plausibility_score(text: str, examples: list[str]) -> int:
    score = 88
    bad_patterns = (
        r"掌门[^。！？!?]{0,12}(?:亲自)?发布[^。！？!?]{0,12}任务",
        r"任务[^。！？!?]{0,12}(?:掌门|师父|道士)[^。！？!?]{0,12}(?:发布|给的|交代)",
        r"系统[^。！？!?]{0,10}(?:给我指的路|发布的任务|让我来)",
    )
    for pattern in bad_patterns:
        if re.search(pattern, text):
            score -= 35
            examples.append("任务来源被直接背书给掌门/系统，但正文没有先给出可信凭据。")
            break
    if "任务" in text and not any(anchor in text for anchor in ("告示", "木牌", "山门", "引路", "任务描述", "界面", "玉牌", "拜帖", "路标")):
        score -= 12
        examples.append("出现任务说法，但缺少告示、界面、信物、引路物等可见来源。")
    if re.search(r"(?:邀请函|论坛|新手村|玩家|排队)[^。！？!?]{0,30}(?:任务|引导任务)|(?:任务|引导任务)[^。！？!?]{0,30}(?:邀请函|论坛|新手村|玩家|排队)", text):
        score -= 22
        examples.append("任务来源落在论坛/邀请函/玩家排队等元游戏信息上，缺少世界内可见凭据。")
    return max(0, min(100, score))


def _npc_agency_score(text: str, examples: list[str]) -> int:
    score = 86
    if "NPC" in text:
        score -= 28
        examples.append("正文直接把世界内人物称为 NPC，削弱人物主体性。")
    if "机械 NPC" in text or "任务 NPC" in text:
        score -= 25
        examples.append("世界内人物被写成任务工具人。")
    return max(0, min(100, score))


def _player_layer_intrusion_score(text: str, examples: list[str]) -> int:
    score = 88
    leak_reasons = game_world_meta_leak_reasons(text)
    if leak_reasons:
        score -= 48
        examples.extend(leak_reasons[:3])
    return max(0, min(100, score))


def game_world_meta_leak_reasons(text: str) -> list[str]:
    reasons: list[str] = []
    for sentence in re.split(r"(?<=[。！？!?])", text or ""):
        compact = re.sub(r"\s+", "", sentence)
        if not compact:
            continue
        if "系统分配" in compact:
            reasons.append("游戏内现场出现“系统分配”类元规则解释，必须改成木牌/山门规矩/人物误判。")
        if "内测" in compact and any(marker in compact for marker in ("门派", "入门", "任务", "协议", "奖金", "清虚观", "拜师")):
            reasons.append("内测信息和门派入门/任务/协议绑定成说明段，侵入游戏内现场认知。")
        if "界面" in compact and any(marker in compact for marker in ("唯一能点开", "点开", "任务", "拜帖", "木牌", "清虚观")):
            reasons.append("用界面/可点击物解释世界内凭据，必须改成实体木牌、拜帖、告示或人物给出的物证。")
        if any(marker in compact for marker in ("任务栏", "任务面板")):
            reasons.append("任务栏/任务面板不能进入清虚观现场的任务来源或破局逻辑。")
        if "NPC" in compact and any(marker in compact for marker in ("不吃这套", "不能说", "跟", "对", "解释")):
            reasons.append("游戏内人物被主角按 NPC 接口理解，削弱人物主体性。")
        if "玩家" in compact and any(marker in compact for marker in ("门派", "清虚观", "山门", "拜师", "试炼")):
            reasons.append("玩家层概念侵入门派/山门/试炼现场。")
        if "论坛" in compact and any(marker in compact for marker in ("清虚观", "门派", "任务", "攻略", "试炼", "入门")):
            if not (compact.startswith(("进游戏前", "登入前", "登录前")) and not any(x in compact for x in ("内测", "任务", "协议", "界面", "唯一能点开"))):
                reasons.append("论坛攻略说明侵入游戏内现场，必须移到现实侧轻描或删除。")
    if _has_player_layer_intrusion(text):
        reasons.append("玩家层说明过载：游戏内正在受盘问/拜师/交涉时，不应成段解释论坛、内测、门派热度、玩家排队。")
    if _has_player_layer_leak(text):
        reasons.append("玩家层概念泄漏：游戏内现场不应直接写“我是来参加内测的”“NPC不吃这套”等解释。")
    return list(dict.fromkeys(reasons))


def _has_player_layer_intrusion(text: str) -> bool:
    for para in _paragraphs(text):
        compact = re.sub(r"\s+", "", para)
        if not any(marker in compact for marker in ("清虚观", "道士", "拂尘", "拜师", "师父", "山门", "门口", "后院")):
            continue
        meta_hits = _count_distinct_markers(
            compact,
            ("内测", "邀请函", "进游戏", "论坛", "新手村", "入门门派", "冷门", "任务抠门", "排队", "玩家"),
        )
        if meta_hits >= 4:
            return True
        if meta_hits >= 3 and any(trigger in compact for trigger in ("谁让你来的", "来做什么", "拜师", "盘问")):
            return True
    return False


def _has_player_layer_leak(text: str) -> bool:
    for sentence in re.split(r"(?<=[。！？!?])", text or ""):
        compact = re.sub(r"\s+", "", sentence)
        if not compact:
            continue
        if compact.startswith(("进游戏前", "登入前", "登录前")) and "NPC" not in compact and "内测" not in compact:
            continue
        meta_hits = _count_distinct_markers(compact, ("内测", "NPC", "玩家", "进游戏", "游戏里", "论坛", "新手村"))
        scene_hits = _count_distinct_markers(compact, ("道士", "清虚观", "拜师", "拂尘", "山门", "师父", "规矩", "咽回去", "不吃这套"))
        if meta_hits >= 2 and scene_hits >= 1:
            return True
        if "内测" in compact and any(marker in compact for marker in ("咽回去", "不能说", "不吃这套", "按规矩", "拜师", "道士", "清虚观")):
            return True
        if "NPC" in compact and any(marker in compact for marker in ("不吃这套", "不能说", "跟", "对", "解释")):
            return True
    return False

def _paragraphs(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"\n\s*\n+", text or "") if part.strip()]


def _count_distinct_markers(text: str, markers: tuple[str, ...]) -> int:
    return sum(1 for marker in markers if marker in text)


def _dialogue_lines(text: str) -> list[str]:
    return [item.strip() for item in re.findall(r"[“「『](.*?)[”」』]", text or "", flags=re.S) if item.strip()]


def _line_marks_inner_monologue_or_panel(line: str) -> bool:
    return any(marker in line for marker in ("检测到", "当前", "提示", "面板", "同步率", "精气值"))


def _recommendations(checks: dict[str, int]) -> list[str]:
    rows: list[str] = []
    if checks.get("character_knowledge_boundary", 100) < 60:
        rows.append("检查角色知识边界：主角知道的系统/玩家/任务机制，不能直接拿去对 NPC 解释，除非世界规则已建立。")
    if checks.get("quest_source_plausibility", 100) < 60:
        rows.append("任务来源必须落到可见凭据：告示、界面、信物、引路物、拜帖或山门规矩。")
    if checks.get("npc_agency", 100) < 60:
        rows.append("把 NPC 当作有身份、利益和怀疑的人写，不要让他们像任务接口。")
    if checks.get("player_layer_intrusion", 100) < 60:
        rows.append("玩家层信息只能轻量放在入场前或现实侧；进入清虚观盘问/拜师现场后，改用可见物证、规矩、误判和人物反应推进。")
    return rows
