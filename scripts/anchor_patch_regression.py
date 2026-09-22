"""Regression: 锚点式窄补丁（2026-09-22 治本重构，预登记 anchor_patch_preregistered.md）。

验收标准 1-3 的离线纯函数覆盖（标准 4 由全量回归批覆盖，标准 5 为实战验收）：

1. _apply_anchor_edits 贴回正确性：合法 edits 非锚点区间逐字节一致；
   锚点缺失/重复/重叠/过短/非列表/超上限 → AnchorPatchError。
2. _extract_adjudicated_protected_strings：只提取【用户裁决】块内的「…」引用串，
   块外反面示例（禁止项）不混入；非裁决简报返回空。
3. _missing_protected_strings / _adjudicated_fallback_ban_error：裁决保护串缺失
   可被检出；裁决简报+非 rewrite_mode 必出兜底禁令，rewrite_mode/非裁决放行。
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.chapter_revision import (
    MAX_ANCHOR_EDITS,
    AnchorPatchError,
    _adjudicated_fallback_ban_error,
    _apply_anchor_edits,
    _extract_adjudicated_protected_strings,
    _missing_protected_strings,
    _parse_anchor_response,
)

SOURCE = (
    "开头第一段，保持原样。\n\n"
    "中间第二段需要修改，原句很糟。\n\n"
    "第三段也不动。\n\n"
    "结尾段同样保持原样。"
)

ADJUDICATED_CONSTRAINTS = """revision_mode:targeted

【用户裁决·2026-09-22·最高优先级】以下为用户批准的既定事实与文风，修订不得回改：
- 李家线全部成立（老李亡、大柱失魂归、「山里有口锅」呓语、「欠了这趟山」）；
- 老道半句评语（「就是轻」「压不住这地方的土」）与物证推理（「叫送回来的」）保留原样；
- 「魂叫山风刮散了」等民间话语保留；
- 批准短句原样保留——「汤还温着。」「噤声！」「山里有口锅。」
  不得注水拉长、不得补解释。
本轮只做场景可画面化，三类动作之外一概不碰：
② 悬念靠在场人身体反应抬，旁白不用「诡异/恐怖」级判断词；
禁止：改动任何情节事实。
"""


def _brief(constraints: str = "", goal: str = "测试") -> SimpleNamespace:
    return SimpleNamespace(id=1, goal=goal, required_beats="", constraints=constraints)


def main() -> int:
    failures: list[str] = []

    # ---- 1. 贴回正确性
    patched = _apply_anchor_edits(SOURCE, [
        {"anchor": "中间第二段需要修改，原句很糟。", "replacement": "中间第二段已经改好。"},
        {"anchor": "开头第一段，保持原样", "replacement": "开头第一段，保持原样（微调）"},
    ])
    expected = SOURCE.replace("中间第二段需要修改，原句很糟。", "中间第二段已经改好。").replace("开头第一段，保持原样", "开头第一段，保持原样（微调）", 1)
    if patched != expected:
        failures.append("case1: 合法 edits 贴回结果与期望不符")
    # 非锚点区间逐字节一致（第三段、结尾段、换行结构原样）
    for untouched in ("第三段也不动。", "结尾段同样保持原样。", "\n\n"):
        if untouched not in patched:
            failures.append(f"case1: 未触碰文本被破坏：{untouched!r}")
    # 删除（空 replacement）
    deleted = _apply_anchor_edits(SOURCE, [{"anchor": "中间第二段需要修改，原句很糟。", "replacement": ""}])
    if "中间第二段" in deleted or "第三段也不动。" not in deleted:
        failures.append("case1: 空 replacement（删除锚点）行为不正确")

    def _expect_error(edits, tag):
        try:
            _apply_anchor_edits(SOURCE, edits)
        except AnchorPatchError:
            return
        failures.append(f"{tag}: 应抛 AnchorPatchError 但未抛")

    _expect_error([{"anchor": "原文里根本不存在的句子。", "replacement": "x"}], "case1-missing")
    dup_source = "重复句甲乙丙丁。中段隔开。重复句甲乙丙丁。"
    try:
        _apply_anchor_edits(dup_source, [{"anchor": "重复句甲乙丙丁。", "replacement": "x"}])
        failures.append("case1-dup: 重复锚点应抛错")
    except AnchorPatchError:
        pass
    _expect_error([
        {"anchor": "开头第一段，保持原样。\n\n中间第二段需要修改，原句很糟。", "replacement": "a"},
        {"anchor": "中间第二段需要修改，原句很糟。", "replacement": "b"},
    ], "case1-overlap")
    _expect_error([{"anchor": "短", "replacement": "x"}], "case1-short")
    _expect_error("not-a-list", "case1-notlist")
    _expect_error([{"anchor": f"锚点{i}号位占位字符", "replacement": "x"} for i in range(MAX_ANCHOR_EDITS + 1)], "case1-toomany")

    # ---- 2. 保护串提取
    protected = _extract_adjudicated_protected_strings(_brief(ADJUDICATED_CONSTRAINTS))
    want = {"山里有口锅", "欠了这趟山", "就是轻", "压不住这地方的土", "叫送回来的",
            "魂叫山风刮散了", "汤还温着。", "噤声！"}
    missing_want = want - set(protected)
    if missing_want:
        failures.append(f"case2: 保护串提取缺失 {sorted(missing_want)}（实得 {len(protected)} 条）")
    if "诡异/恐怖" in protected:
        failures.append("case2: 裁决块外的反面示例「诡异/恐怖」被误提取")
    if _extract_adjudicated_protected_strings(_brief("普通简报，无裁决标记。「某句」")) != []:
        failures.append("case2: 非裁决简报应提取为空")

    # ---- 3. 保护校验 + 兜底禁令
    source_full = (
        "保留了 就是轻 、 压不住这地方的土 、 叫送回来的 、 魂叫山风刮散了 、"
        " 汤还温着。 、 噤声！ 、 山里有口锅。 、 欠了这趟山 等裁决内容的源稿。"
    )
    gone = _missing_protected_strings(_brief(ADJUDICATED_CONSTRAINTS), "全新的重写内容，啥也不剩。", source_content=source_full)
    if not gone:
        failures.append("case3: 保护串全部缺失时 _missing_protected_strings 应非空")
    kept = _missing_protected_strings(_brief(ADJUDICATED_CONSTRAINTS), source_full, source_content=source_full)
    if kept:
        failures.append(f"case3: 全部在位时不应有缺失，实得 {kept}")
    if _missing_protected_strings(_brief("无标记简报"), "任何内容", source_content=source_full) != []:
        failures.append("case3: 非裁决简报不做保护校验")
    # 概念名（源文中不逐字存在）不得参与校验（brief 18 实测教训）
    meta_brief = _brief("【用户裁决·测试】\n- 开场「引用回声式承接」保留原样；\n- 「就是轻」保留。")
    meta_missing = _missing_protected_strings(meta_brief, "不含评语的新稿。", source_content="只有普通正文，没有评语。")
    if meta_missing:
        failures.append(f"case3: 源文中不存在的概念名不应判缺失，实得 {meta_missing}")

    err = _adjudicated_fallback_ban_error(_brief(ADJUDICATED_CONSTRAINTS), rewrite_mode=False)
    if not err or "拒绝整章兜底" not in err:
        failures.append(f"case3: 裁决简报+非 rewrite 应出兜底禁令，实得 {err!r}")
    if _adjudicated_fallback_ban_error(_brief(ADJUDICATED_CONSTRAINTS), rewrite_mode=True) is not None:
        failures.append("case3: rewrite_mode（简报自主要求重写）不应被禁令拦截")
    if _adjudicated_fallback_ban_error(_brief("无标记简报"), rewrite_mode=False) is not None:
        failures.append("case3: 非裁决简报不应被禁令拦截")

    # ---- 4. 响应解析：推理前缀 + schema 回声 + 真实 JSON（kimi-k3 实测形态）
    messy = (
        'Let me analyze. The schema is '
        '{"patch_note":"一句话说明","edits":[{"anchor":"原文逐字片段","replacement":"x","reason":"y"}]}. '
        'Checking each paragraph... 结论如下：\n'
        '{"patch_note":"真改","edits":[{"anchor":"中间第二段需要修改，原句很糟。","replacement":"中间第二段已经改好。","reason":"r"}]}'
    )
    parsed = _parse_anchor_response(messy)
    if parsed.get("patch_note") != "真改":
        failures.append(f"case4: 应取推理之后的真实 JSON，实得 patch_note={parsed.get('patch_note')!r}")
    else:
        repatched = _apply_anchor_edits(SOURCE, parsed["edits"])
        if "中间第二段已经改好。" not in repatched:
            failures.append("case4: 解析出的 edits 贴回后未生效")
    # 干净 JSON 直接解析
    clean = _parse_anchor_response('{"patch_note":"ok","edits":[]}')
    if clean.get("edits") != []:
        failures.append("case4: 干净 JSON 解析异常")
    # 只有 schema 回声（JSON 被截断）：解析收到回声，但占位锚点过不了逐字校验——失败保持诚实
    echo_only = 'Thinking... schema: {"patch_note":"一句话说明","edits":[{"anchor":"原文逐字片段","replacement":"x","reason":"y"}]} then truncated {"patch_note"'
    try:
        echo_data = _parse_anchor_response(echo_only)
        try:
            _apply_anchor_edits(SOURCE, echo_data["edits"])
            failures.append("case4: schema 回声的占位锚点竟通过了逐字校验")
        except AnchorPatchError:
            pass
    except ValueError:
        pass  # 直接拒绝也合法

    if failures:
        print("anchor_patch_regression=FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("anchor_patch_regression=PASS")
    print("cases_evaluated=15")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
