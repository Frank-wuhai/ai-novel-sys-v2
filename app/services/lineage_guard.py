# -*- coding: utf-8 -*-
"""传承一致性守卫(确定性,零LLM) —— 抓"功法/信物凭空到手""对话回指凭空""功法名混用"。

被 app 生成管道(production_llm.repair_lineage_consistency)和
reference_corpus/lineage_consistency_check.py(离线批量核查)共用。

核心逻辑:
  1. canon 功法/信物:若正文出现"使用/持有"动作,则必须也出现"获得/传授"动作,否则=凭空。
  2. 功法名混用:主角走的是"家传吐纳法门",却又称在练"纯阳功"且无传授句。
  3. 对话回指凭空:一句对话引用了一个前文从未写过的动作(如"你那个礼谁教的"但前文没行礼)。
"""
from __future__ import annotations

import re

# canon 功法/信物清单(与 story_bibles / world_rules book6 对齐)
LINEAGE_ITEMS = ["纯阳功", "松风十三剑", "绵掌", "梯云纵", "承影"]
# 身份信物类(拿了就代表某身份,凭空=硬伤)
TOKEN_ITEMS = ["木牌", "令牌", "信物", "引荐信"]

# 对话回指凭空:一句对话引用了一个前文没发生的动作。(正则锚点, [前文应出现的关键词])
_DIALOG_CALLBACKS = [
    (r"那个礼[，,。？?]?谁教的|你那个礼|方才的礼", ["礼", "行礼", "作揖", "抱拳", "躬身", "拜", "拱"]),
    (r"方才的拳|刚才那招|那一掌|那一式", ["拳", "掌", "招", "式", "练", "比划"]),
    (r"你那一剑|那一剑何名|方才那一剑", ["剑", "刺", "挥", "劈"]),
]

# ★头盔/脑机设备来历(现实侧科幻底座·S7-b/c 确定性化):
#   正文一旦出现"戴头盔/脑机进游戏"的动作,全章必须有一处交代设备来历或沉浸原理,
#   否则=凭空出现在床头的降智硬伤。这是能确定性拦截的,不交给 prompt 祈祷。
_DEVICE_NAMES = ["头盔", "脑机", "游戏舱", "神经接入", "接入舱"]
# 进游戏/戴设备动作(触发词):出现即认定本章有"进游戏"场景,来历必须交代
_DEVICE_ENTER_VERBS = ["戴上", "扣上", "戴好", "一扣", "套上", "接入", "登入", "登录", "进入游戏", "进游戏", "躺进", "钻进"]
# 来历/原理交代词分两类:
#   ★来历词(设备"怎么来的")——必须出现在设备名邻近窗口内才算数。全章匹配会假阴性:
#     "内测奖金到账"里的"内测"离"游戏舱"很远,修饰的是游戏收入不是设备来历。
_DEVICE_ORIGIN_NEARBY = [
    "二手", "内测", "附赠", "标配", "淘来", "淘的", "捡来", "捡的", "攒钱", "分期", "省吃俭用",
    "人手一台", "人人都有", "普及", "廉价", "白菜价", "留下的", "送的", "借来", "买来", "买的",
]
#   ★原理/时代词(点破"为何一戴就真实"或"这是什么时代")——硬科幻专有词,全章任意位置都算,不会误伤。
_DEVICE_PRINCIPLE_ANY = [
    "脑机接口", "神经元", "意识接入", "全感官", "拟真", "近未来", "这年头",
    "现在家家", "这时代", "这个时代",
]
# 合并给回归/离线脚本引用(向后兼容)
_DEVICE_ORIGIN_KW = _DEVICE_ORIGIN_NEARBY + _DEVICE_PRINCIPLE_ANY


def _check_device_origin(content: str) -> list[dict]:
    """确定性头盔来历检测:出现'戴设备进游戏'动作但全章无来历/原理交代=硬伤。"""
    issues: list[dict] = []
    has_device = any(d in content for d in _DEVICE_NAMES)
    if not has_device:
        return []
    # 是否有"戴/进"动作(确认本章确实有进游戏场景,纯提及设备名不触发)
    has_enter = False
    for d in _DEVICE_NAMES:
        for m in re.finditer(re.escape(d), content):
            s = max(0, m.start() - 12)
            e = min(len(content), m.end() + 12)
            if any(v in content[s:e] for v in _DEVICE_ENTER_VERBS):
                has_enter = True
                break
        if has_enter:
            break
    if not has_enter:
        return []
    # 有进游戏动作:判定是否已交代来历或原理。
    #   原理词(脑机接口/神经元等硬科幻专有词)——全章任意位置出现即认定已点破沉浸原理。
    if any(kw in content for kw in _DEVICE_PRINCIPLE_ANY):
        return issues
    #   来历词——必须出现在某个设备名的邻近窗口(±60字)内才算数,
    #   避免"内测奖金"这类离设备很远、修饰别的对象的词造成假阴性。
    for d in _DEVICE_NAMES:
        for m in re.finditer(re.escape(d), content):
            s = max(0, m.start() - 60)
            e = min(len(content), m.end() + 60)
            if any(kw in content[s:e] for kw in _DEVICE_ORIGIN_NEARBY):
                return issues
    # 既无原理词、又无邻近来历词 → 设备来历凭空
    q = re.findall(r"[^。！？\n]{0,20}(?:头盔|脑机|游戏舱)[^。！？\n]{0,20}", content)
    return [{
        "type": "设备来历凭空",
        "name": "游戏头盔/脑机设备",
        "quote": (q[0].strip() if q else "头盔"),
        "note": "主角戴设备进游戏,但设备本身既无来历交代(二手/内测附赠/标配/攒钱买等,须紧贴设备本身,不能是'内测奖金'这类无关的钱)也无沉浸原理点破(脑机接口/神经元直连让人有真实触觉),读者会觉得凭空出现、就这么进去了,降智",
    }]


# ★S2 单机感(游戏世界"活人纹理"·确定性化):
#   本章有游戏内场景(进了《入梦》游戏世界)却完全没有其他玩家的存在痕迹,
#   读起来像单机 RPG 只有主角+NPC。爽文里游戏世界该有别的玩家路过/喊话/组队/公屏。
#   这是能确定性拦的:检测"进了游戏世界"信号 + 全章零"其他玩家纹理"→硬伤触发补写。
# 进入游戏世界的信号(确认本章确有游戏内场景,纯现实章不触发)
_GAME_SCENE_MARKERS = ["入梦", "清虚观", "游戏舱", "登入", "登录", "进入游戏", "进游戏",
                       "系统提示", "面板", "属性栏", "新手村", "复活", "退出登录", "下线"]
# 其他玩家的存在纹理(命中任一即认为游戏世界有活人,不算单机感)
_OTHER_PLAYER_MARKERS = [
    "玩家", "道友", "同服", "同区", "组队", "队友", "工会", "帮派", "公会",
    "公屏", "世界频道", "论坛", "攻略", "喊话", "私聊", "好友", "路过的人",
    "别的玩家", "其他玩家", "有人喊", "有人在", "人群", "人流", "熙熙攘攘",
    "叫卖", "摆摊", "排队", "围观", "ID", "等级榜", "排行榜", "野队", "新手", "萌新", "大佬",
]


def _check_single_player_feel(content: str) -> list[dict]:
    """确定性单机感检测:本章有游戏内场景但全章零其他玩家纹理=单机感硬伤。"""
    # 只对完整章节生效:短片段(测试反例/局部单元)不做整章级"单机感"判定,避免误伤。
    if len(content) < 800:
        return []
    # 是否进了游戏世界
    if not any(mk in content for mk in _GAME_SCENE_MARKERS):
        return []
    # 至少要有明确的游戏世界地标(清虚观/入梦/系统面板),避免只在现实里提一句"入梦"就触发
    strong = ["清虚观", "系统提示", "面板", "属性栏", "新手村", "复活", "退出登录", "下线"]
    if not any(mk in content for mk in strong) and content.count("入梦") < 1:
        return []
    # 全章是否有任何其他玩家纹理
    if any(mk in content for mk in _OTHER_PLAYER_MARKERS):
        return []
    return [{
        "type": "单机感",
        "name": "游戏世界无其他玩家",
        "quote": "(整章游戏场景仅主角与NPC)",
        "note": "本章有游戏内场景,但全章没有任何其他玩家的存在纹理(路过/喊话/组队/公屏/论坛/摆摊/围观等),读起来像单机RPG。这是网游文,游戏世界该有别的活人玩家的痕迹,哪怕远处几个身影、公屏一行字",
    }]

USE_VERBS = ["练", "使", "运", "运转", "施展", "催动", "使出", "打出", "运起", "修炼", "习练", "以"]
TOKEN_USE_VERBS = ["揣", "掏出", "递", "亮出", "出示", "拿出", "凭", "持", "挂着", "带着", "凭着", "拿着", "攥着"]
GAIN_VERBS = ["传", "授", "教", "给", "赐", "学会", "学了", "习得", "得了", "得到",
              "获得", "记住", "递给", "丢给", "扔给", "抛给", "塞给", "留下", "赠", "收下", "接过", "发给"]


def _near(content, name, verbs, window=6, exclude_question=False):
    """在 name 出现位置前后 window 字符内是否有 verbs 里的动词。
    exclude_question: 若该处 name 落在疑问句中,不计入(疑问≠事实)。"""
    for m in re.finditer(re.escape(name), content):
        s = max(0, m.start() - window)
        e = min(len(content), m.end() + window)
        ctx = content[s:e]
        if exclude_question:
            sent_s = max(0, m.start() - 30)
            sent_e = min(len(content), m.end() + 10)
            if "?" in content[sent_s:sent_e] or "？" in content[sent_s:sent_e]:
                continue
        for v in verbs:
            if v in ctx:
                return True
    return False


def _best_quote(content, name):
    """优先摘一句'陈述性使用'该 name 的句子(非疑问),作为硬伤锚点。"""
    candidates = re.findall(r"[^。！？\n]{0,25}" + re.escape(name) + r"[^。！？\n]{0,25}", content)
    for c in candidates:
        if "?" in c or "？" in c:
            continue
        if any(v in c for v in ["练", "使", "运", "施展", "以", "催动", "打出"]):
            return c.strip()
    for c in candidates:
        if "?" not in c and "？" not in c:
            return c.strip()
    return (candidates[0].strip() if candidates else name)


def check_chapter(title, content, prior_gained=None):
    """返回 (issues, gained_this)。
    prior_gained: 前文已获得的名(集合),本章不再要求交代来源。"""
    prior_gained = prior_gained or set()
    issues = []
    gained_this = set()

    for name in LINEAGE_ITEMS + TOKEN_ITEMS:
        if name not in content:
            continue
        is_token = name in TOKEN_ITEMS
        use_verbs = TOKEN_USE_VERBS if is_token else USE_VERBS
        used = _near(content, name, use_verbs, exclude_question=True)
        # 功法传授句往往较长("传你清虚观入门心法——《纯阳功》残篇"),用宽窗口(±24字);
        # 信物获得动作短("递给他一块木牌"),用窄窗口(±8字)避免扫到邻句无关动词。
        gain_window = 8 if is_token else 24
        gained = _near(content, name, GAIN_VERBS, window=gain_window)
        if gained:
            gained_this.add(name)
        if used and not gained and name not in prior_gained:
            issues.append({
                "type": "凭空到手",
                "name": name,
                "quote": _best_quote(content, name),
                "note": f"正文出现'{'持有' if is_token else '使用'}{name}'但全章无传授/获得动作,且前文未获得",
            })

    # 功法名混用
    tunafa = ("吐纳法门" in content) or ("吐纳" in content and "家传" in content)
    if tunafa and "纯阳功" in content:
        chunyang_gained = _near(content, "纯阳功", GAIN_VERBS, window=30)
        chunyang_used = _near(content, "纯阳功", ["练", "走", "运", "修", "以"], exclude_question=True)
        if chunyang_used and not chunyang_gained and "纯阳功" not in prior_gained:
            issues.append({
                "type": "功法名混用",
                "name": "纯阳功↔吐纳法门",
                "quote": _best_quote(content, "纯阳功"),
                "note": "主角前文走的是家传吐纳法门,后文却称在练纯阳功,且无老道传授纯阳功的动作",
            })

    # 对话回指凭空
    for anchor, action_kw in _DIALOG_CALLBACKS:
        m = re.search(anchor, content)
        if not m:
            continue
        before_text = content[:m.start()]
        if not any(kw in before_text for kw in action_kw):
            issues.append({
                "type": "对话回指凭空",
                "name": m.group().strip(),
                "quote": m.group().strip(),
                "note": f"对话引用了'{'/'.join(action_kw)}'这个动作,但引用点之前的正文从未描写主角做过它",
            })

    # ★头盔/脑机设备来历(现实侧科幻底座硬伤·确定性)
    issues.extend(_check_device_origin(content))

    # ★S2 单机感(游戏世界无其他玩家纹理·确定性)
    issues.extend(_check_single_player_feel(content))

    return issues, gained_this


def describe_issues(issues) -> str:
    """把 issues 列成给 LLM 返修用的中文指令清单。"""
    lines = []
    for it in issues:
        lines.append(f"- [{it['type']}] {it['name']}：{it['note']}。原文锚点：「{it['quote']}」")
    return "\n".join(lines)
