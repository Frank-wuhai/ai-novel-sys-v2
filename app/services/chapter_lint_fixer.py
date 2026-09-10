# -*- coding: utf-8 -*-
"""chapter_lint_fixer.py — 章节确定性后处理器 (B-3/B-4)

核心哲学: 凡是代码能确定性拦截的,绝不交给 prompt 祈祷。
每个变换 idempotent(重复运行结果不变),改了永不反弹,形成只进不退的棘轮。

覆盖:
  C1 游戏黑话替换  —— NPC/PVP/BOSS/game 等武侠世界出戏词 → 古风词(白名单ID/ICU等保留)
  C2 不是X是Y 清除 —— 把"不是A，是B"改写为直接陈述B(去掉否定对比脚手架)
  C3 拐杖意象降频  —— 同一意象词在近距离(窗口内)重复出现时,第2+次替换为同义变体

不做打分、不做过门。只做文本变换 + 返回修复统计(供 dashboard 追踪)。
"""
from __future__ import annotations
import re

# ---------------- C1 游戏黑话替换 ----------------
# 白名单(保留,与 deterministic_lint.py 一致): ID/ICU/CT/IP/APP/bug
# 黑名单 → 古风替换词。就近语境替换,武侠世界不出戏。
_GAME_JARGON_MAP = {
    "NPC": "剧中人",
    "PVP": "生死斗",
    "PVE": "闯关",
    "BOSS": "魁首",
    "boss": "魁首",
    "buff": "加持",
    "debuff": "折损",
    "combo": "连招",
    "game舱": "游戏舱",
    "game": "游戏",
    "Game": "游戏",
    "GAME": "游戏",
    "UI": "操作界面",
    "ui": "操作界面",
}

# ---------------- C1b 生造英文科技品牌名 → 中文 ----------------
# LLM 写近未来脑机设定时爱自造英文品牌名(如 NeuroLink/NeruoLink/BrainLink),
# 还常拼错。中文网文里出现裸英文品牌=中英混杂扣分。用正则统一替换。
# 白名单短词(ID/APP/IP 等)不受影响,因为这里只匹配 CamelCase 或含 Link/Tech/Net 的合成词。
_TECH_BRAND_RE = re.compile(
    r"(?:Ner[uo]{2,3}Link|Neuro[A-Za-z]+|Brain[A-Za-z]*Link|[A-Z][a-z]+Link|"
    r"[A-Z][a-z]+Tech|[A-Z][a-z]+Net|[A-Z][a-z]+Gear|[A-Z][a-z]+Sync)"
    r"(?:[- ]?\d+)?"  # 可带型号后缀 -3 / 3
)
_TECH_BRAND_REPL = "脑机接口设备"

# ---------------- C2 "不是X，是Y" 清除 ----------------
# 匹配 不是<A>[，,。]（而）是<B>  A、B 为不含句末标点的短片段
# 改写策略: 直接保留 Y(去掉"不是X，(而)是"脚手架),把 Y 提为主句
_BUSHI_FULL_RE = re.compile(
    r"不是([^，。！？、\n]{1,18})[，,]\s*(?:而)?是([^，。！？、\n]{1,18})"
)

# ---------------- C5 比喻本体错配：锈蚀词误配有机体 ----------------
# 金属会锈，骨头/身子骨/血肉/筋不会。锈蚀词紧邻有机体名词=本体错配。
# 修法：把"(生)锈/锈透/锈蚀 + 有机体"改成语义正确的"僵/发僵"或直接删锈字。
# 只匹配"锈蚀词 <的/得> ... 有机体"或"有机体 ... 锈透"两种安全窄模式，避免误伤"门轴生锈"。
_ORGANIC = "骨头|身子骨|骨架|血肉|筋骨|筋|皮肉|嗓子|喉咙|脑子"
# 模式1: 像/跟/如 生锈的<有机体>  → 像发僵的<有机体>
_RUST_METAPHOR_RE = re.compile(rf"(生?锈)的({_ORGANIC})")
# 模式2: <有机体>...锈透/锈住（中间可有"还没/都/全"等副词）→ 用"发僵"
_RUST_ORGAN_RE = re.compile(rf"({_ORGANIC})(还|都|全|没|又)*(生|发)?锈(透|住)")

# ---------------- C4 翻译腔: 开头主语"他/我"+"感到/觉得/意识到" ----------------
# 业界 4 大翻译腔特征之一,中译英常这么写,中文用身体化细节更地道
# 例:"他感到后背发凉" → "后背蹿上来一阵凉意"
# 策略:删除 "感到/觉得/意识到" 主语后那一句的开头标签,让后文做主语
_C4_PERCEPTION_RE = re.compile(
    rf"^({_ORGANIC}|[他她我]|内心|心里|脑中|心里){{0,2}}(感到|觉得|意识到|察觉到|发觉|明白到)([^。\n]{{0,40}})[。\n]"
)

# ---------------- C6 抽象情绪标签 → 强制身体化 ----------------
# LLM 写情绪最爱用"很紧张/很震惊/很痛苦"——读者感受不到
# 修法:把"很<情绪词>"改为具体身体化表达(本节保守策略:只删"很",避免硬改语义偏差)
_C6_VERY_EMOTION_RE = re.compile(r"很(紧张|震惊|痛苦|害怕|恐惧|愤怒|激动|失望|沮丧|开心|高兴|兴奋|难受|委屈)")

# ---------------- C8 "不是X的。是Y的。" 三段式倒装 ----------------
# 这种"不是X的。是Y的。"节奏工具(机械 AI 味)禁用
# 修法:直接保留 Y 那段(去掉"不是X的。是"脚手架)
_C8_NEG_TRIPLET_RE = re.compile(
    r"不是([^，。！？\n]{1,12})的[。.][\s]*是([^，。！？\n]{1,18})的[。.][\s]*"
)

# ---------------- C7 重复短句连用 降频 ----------------
# LLM 写"一/二/三/四"句连用制造紧迫感,过 3 个短句(≤12字)连用就降频
# 策略:把第 3+ 个短句改为复合句(用"而"连接)

# ---------------- C3 拐杖意象降频 ----------------
# 高频拐杖意象 → 可替换的同义变体池。近距重复时轮换。
_CRUTCH_VARIANTS = {
    "指节发白": ["手背青筋绷起", "攥得骨头咯响", "五指掐进掌心"],
    "发白": ["泛青", "煞白", "褪了血色"],
    "冰": ["凉", "寒", "冷"],
    "棉花": ["软泥", "空处", "浮云"],
    "砂纸": ["粗石", "锉刀", "枯木"],
    "喉结": ["嗓子", "咽喉", "喉咙"],
    "针扎": ["刺痛", "麻痒", "钝疼"],
}
# C3 只对"近距离重复"生效(同一意象在 WINDOW 字符内出现第2次才替换)
_CRUTCH_WINDOW = 600


def _fix_c1_jargon(text: str) -> tuple[str, int]:
    n = 0
    for jargon, repl in _GAME_JARGON_MAP.items():
        cnt = text.count(jargon)
        if cnt:
            text = text.replace(jargon, repl)
            n += cnt
    # C1b: 生造英文科技品牌名(NeuroLink/XxxLink 等)→ 中文
    brand_hits = _TECH_BRAND_RE.findall(text)
    if brand_hits:
        text = _TECH_BRAND_RE.sub(_TECH_BRAND_REPL, text)
        n += len(brand_hits)
    return text, n


def _fix_c2_bushi(text: str) -> tuple[str, int]:
    """把'不是A，是B'改写为直接陈述B。保留B,丢弃否定对比脚手架。"""
    count = [0]

    def _repl(m):
        b = m.group(2).strip()
        count[0] += 1
        return b  # 直接用 B 替换整个"不是A，是B"结构

    new = _BUSHI_FULL_RE.sub(_repl, text)
    return new, count[0]


def _fix_c3_crutch(text: str) -> tuple[str, int]:
    """近距重复的拐杖意象降频:窗口内第2+次出现时轮换为同义变体。"""
    total = 0
    for word, variants in _CRUTCH_VARIANTS.items():
        if not variants:
            continue
        # 找到所有出现位置
        positions = [m.start() for m in re.finditer(re.escape(word), text)]
        if len(positions) < 2:
            continue
        # 从后往前替换(避免位移错乱),窗口内重复的第2+次才换
        last_kept = None
        to_replace = []  # (pos) 需要替换的位置
        for p in positions:
            if last_kept is None or (p - last_kept) > _CRUTCH_WINDOW:
                last_kept = p  # 这次保留原词,重置窗口
            else:
                to_replace.append(p)  # 窗口内重复,替换
        # 从后往前替换
        vi = 0
        for p in reversed(to_replace):
            variant = variants[vi % len(variants)]
            vi += 1
            text = text[:p] + variant + text[p + len(word):]
            total += 1
    return text, total


def _fix_c5_rust_metaphor(text: str) -> tuple[str, int]:
    """比喻本体错配：锈蚀词误配有机体（骨头/身子骨等）→ 改为语义正确的'僵'。"""
    n = [0]

    def _m1(m):
        n[0] += 1
        return "发僵的" + m.group(2)  # 生锈的骨头 → 发僵的骨头

    def _m2(m):
        n[0] += 1
        adv = m.group(2) or ""  # 保留"还没/都"等副词
        return m.group(1) + adv + "发僵"  # 身子骨还没锈透 → 身子骨还没发僵

    text = _RUST_METAPHOR_RE.sub(_m1, text)
    text = _RUST_ORGAN_RE.sub(_m2, text)
    return text, n[0]


def _fix_c4_perception(text: str) -> tuple[str, int]:
    """C4 翻译腔:'感到/觉得/意识到 + 抽象标签'开头 → 砍掉标签,让后文身体化细节当主语。

    例:'他感到后背发凉' → '后背发凉'
        '我意识到自己心跳在加快' → '心跳在加快'
    策略:匹配"主语(他/我/她/内心/心里/脑中[组合])+ 标签(感到/觉得/意识到)",删除主语+标签。
    """
    n = 0
    # 复合主语(他/她/我 + 可选 心里/内心/脑中)
    verbs = ("感到", "觉得", "意识到", "察觉到", "发觉", "明白到")
    for verb in verbs:
        # 严苛:只匹配"他/我/她/内心/心里/脑中 + 标签",中间无其他字
        # 关键:主语组合要严苛,避免误伤"他快速感到..."
        # 中间 group 只允许"心里/内心/脑中"等纯定位词,不允许"他/我/她"
        patterns = [
            rf"(^|[\s，。\n])(他|她|我|内心|心里|脑中)((内心|心里|脑中){{0,2}})({verb})([^。\n]{{0,40}})([。\n])",
        ]
        for pat_str in patterns:
            pat = re.compile(pat_str)
            matches = list(pat.finditer(text))
            for m in reversed(matches):  # 从后往前替换,避免位移错乱
                # 仅当中间没有动词/形容词时,才删
                middle = m.group(4) or ""
                if not re.search(r"[的了着过是]", middle):
                    # 保留主语(他/我/她/内心等)+ 可选中间修饰,只删翻译腔标签(感到/觉得等)
                    prefix = m.group(1) + m.group(2) + (m.group(3) or "")
                    # 后续可能以"他/我/她"开头(复合从句)— 删掉避免重复主语
                    tail = m.group(6)
                    if tail and tail[0] in "他她我":
                        tail = tail[1:]  # 去掉首字
                    text = text[:m.start()] + prefix + tail + m.group(7) + text[m.end():]
                    n += 1
    return text, n


def _fix_c6_very_emotion(text: str) -> tuple[str, int]:
    """C6 抽象情绪标签:'很<情绪词>' → 强制身体化。

    例:'他很紧张' → '他手心冒汗'(这里采用保守:只删"很",把强度交给上下文)
        '他心跳很快,很紧张' → '他心跳很快,紧张'
    策略:把"很"删掉,让情绪词单独出来,读者自己感知强度。
    """
    n = 0
    for m in _C6_VERY_EMOTION_RE.finditer(text):
        n += 1
    text = _C6_VERY_EMOTION_RE.sub(lambda m: m.group(1), text)
    return text, n


def _fix_c8_neg_triplet(text: str) -> tuple[str, int]:
    """C8 '不是X的。是Y的。' 三段式倒装 → 删脚手架保留 Y。

    例:'不是他错。是世界错。' → '世界错。'
        '不是躲得漂亮。是躲得勉强。' → '躲得勉强。'
    策略:这种 AI 味节奏工具,直接砍掉。
    """
    n = 0

    def _repl(m):
        nonlocal n
        n += 1
        return m.group(2) + "。"  # 保留 Y + 句号

    text = _C8_NEG_TRIPLET_RE.sub(_repl, text)
    return text, n


def fix_chapter_text(text: str) -> tuple[str, dict]:
    """对整章正文做确定性后处理。返回(修复后正文, 修复统计)。
    idempotent: 对已修复文本再跑一次,统计应接近0。"""
    stats = {}
    text, stats["c1_jargon"] = _fix_c1_jargon(text)
    text, stats["c2_bushi"] = _fix_c2_bushi(text)
    text, stats["c3_crutch"] = _fix_c3_crutch(text)
    text, stats["c4_perception"] = _fix_c4_perception(text)
    text, stats["c5_rust"] = _fix_c5_rust_metaphor(text)
    text, stats["c6_very_emotion"] = _fix_c6_very_emotion(text)
    text, stats["c8_neg_triplet"] = _fix_c8_neg_triplet(text)
    stats["total"] = sum(stats.values())
    return text, stats


if __name__ == "__main__":
    # 自测: 验证幂等性与效果
    sample = (
        "他戴上game舱,系统提示NPC靠近。不是有方向,是冷。"
        "他指节发白。又一次指节发白。胃里像塞了块冰,后背也像冰。"
        "不是躲得漂亮,是躲得勉强。BOSS出现了。"
        "门轴涩得跟生锈的骨头似的。他说身子骨还没锈透。"
    )
    fixed, s = fix_chapter_text(sample)
    print("原文:", sample)
    print("修复:", fixed)
    print("统计:", s)
    # 幂等性检查
    fixed2, s2 = fix_chapter_text(fixed)
    print("二次统计(应接近0):", s2)
    assert s2["c1_jargon"] == 0, "C1 应幂等"
    assert s2["c2_bushi"] == 0, "C2 应幂等"
    assert s2["c5_rust"] == 0, "C5 应幂等"
    # 验证正确用法不被误伤
    ok = "门轴涩得厉害,断剑锈迹斑斑,铁架也生锈了。"
    _, s3 = fix_chapter_text(ok)
    assert s3["c5_rust"] == 0, "C5 不得误伤金属正确锈用法"
    print("✅ 幂等性 + 无误伤 通过")
