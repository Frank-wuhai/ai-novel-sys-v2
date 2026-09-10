"""番茄阅读指标检测。

检测口径只管移动端可读形态，不要求生成端写成电报句：
1. 段落过长才 hard fail；短段不代表每句都要拆成 2-8 字孤句。
2. 每千字段数不足会提示段落过密。
3. 游戏词密度按调用方传入阈值检测；game_wuxia 生产链路通常放宽到 10/千字。
4. 最长段和单章字数作为阅读体验/结构风险提示。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# 游戏词黑名单（会打断武侠/仙侠沉浸感）
GAME_WORDS = (
    "系统", "NPC", "npc", "账号", "内测", "论坛", "服务器",
    "数据流", "版本更新", "删号", "重练", "任务栏", "任务栏",
    "血条", "buff", "debuff", "buff条", "cd时间", "冷却时间",
    "重生", "退游", "游戏公司", "客服", "举报",
    "刷副本", "副本", "打怪升级", "boss", "掉落", "爆装备",
    "属性面板", "面板", "点卡", "月卡", "礼包码",
    "客户端", "客户端", "补丁", "服务端", "gm", "GM",
    "封号", "封号", "外挂",
)


@dataclass(frozen=True)
class FanqieHardMetrics:
    para_avg: float
    para_max: int
    paras_per_1000: float
    game_word_density: float
    total_chars: int
    issues: list[str]  # hard-fail 级别
    warnings: list[str]  # 软警告

    def to_dict(self) -> dict[str, Any]:
        return {
            "para_avg_chars": round(self.para_avg, 1),
            "para_max_chars": self.para_max,
            "paragraphs_per_1000_chars": round(self.paras_per_1000, 1),
            "game_word_density_per_1000": round(self.game_word_density, 2),
            "total_chars": self.total_chars,
            "issues": self.issues,
            "warnings": self.warnings,
        }


def _split_paragraphs(text: str) -> list[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n|\r\n\s*\r\n", str(text or "")) if p.strip()]
    if len(paras) <= 2:
        paras = [p.strip() for p in str(text or "").split("\n") if p.strip()]
    # 兜底：如果 \n\n 分段和 \n 分段差异过大 (>1.4x)·说明多数段落只用了单换行·按 \n 切
    # 这样与 dispatch 入口 format_paragraphs 后的番茄真实呈现一致
    lines = [p.strip() for p in str(text or "").split("\n") if p.strip()]
    if len(lines) > len(paras) * 1.4:
        paras = lines
    return paras


def evaluate_fanqie_metrics(text: str, game_word_limit: float = 5.0) -> FanqieHardMetrics:
    paras = _split_paragraphs(text)
    total = len(text.replace(" ", "").replace("\n", ""))
    issues: list[str] = []
    warnings: list[str] = []
    if not paras or total == 0:
        return FanqieHardMetrics(0.0, 0, 0.0, 0.0, 0, ["empty_text"], [])

    para_lens = [len(p) for p in paras]
    para_avg = sum(para_lens) / len(paras)
    para_max = max(para_lens)
    paras_per_1000 = len(paras) / total * 1000

    # 游戏词密度
    lower = text.lower()
    game_hits = sum(text.count(w) if w.isalpha() and w.isascii() is False else lower.count(w.lower()) for w in GAME_WORDS)
    game_density = game_hits / total * 1000

    # hard-fail 判定 (2026-08-18: 番茄段长软门 — 45-70/段密度 18-24 视为可接受，
    # 只有段均 >90 或段密度 <14 才 hard fail，避免误杀中等长段)
    if para_avg > 90:
        issues.append(f"fanqie_para_avg_too_long: {para_avg:.1f} > 90")
    if paras_per_1000 < 14:
        issues.append(f"fanqie_para_density_too_low: {paras_per_1000:.1f} < 14")
    if game_density > game_word_limit:
        issues.append(f"fanqie_game_word_density_too_high: {game_density:.2f} > {game_word_limit:g}/千字")

    # 软警告 (2026-08-18: 45-70 段均 + 18-24 段密度 视为可接受，但仍记录边界)
    if 70 < para_avg <= 90:
        warnings.append(f"para_avg_in_soft_zone: {para_avg:.1f} in (70, 90]")
    if para_avg > 35 and para_avg <= 70:
        warnings.append(f"para_avg_above_ideal: {para_avg:.1f} > 35")
    if 14 <= paras_per_1000 < 18:
        warnings.append(f"para_density_in_soft_zone: {paras_per_1000:.1f} in [14, 18)")
    if paras_per_1000 < 30 and paras_per_1000 >= 18:
        warnings.append(f"para_density_below_ideal: {paras_per_1000:.1f} < 30")
    if game_density > 3:
        warnings.append(f"game_word_density_above_ideal: {game_density:.2f} > 3")
    if para_max > 150:
        warnings.append(f"longest_para_too_long: {para_max} > 150")

    return FanqieHardMetrics(
        para_avg=para_avg,
        para_max=para_max,
        paras_per_1000=paras_per_1000,
        game_word_density=game_density,
        total_chars=total,
        issues=issues,
        warnings=warnings,
    )
