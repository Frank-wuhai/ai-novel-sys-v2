"""Regression: CONFLICT / CHOICE-COST marker vocabulary must cover
action-driven conflict/cost expression, not just abstract words.

Bug (Book2 Ch4 death loop, Phase E.3): the well-written Ch4 v31 (4183
chars, coherent turning-point scene) scored conflict_pressure=50 and
choice_and_cost=50 because the text expressed conflict through actions
("盯着、盘问、破绽、硬撑") without using the 8 abstract words in
CONFLICT_MARKERS or CHOICE_COST_MARKERS. Both `chapter_type_gate`
required_dimensions were 68; the chapter looped 31 revisions and hit
the ceiling never crossing 74.

Root cause: `_marker_score(text, markers)` = 50 + hits*8 keyword-count
scoring is too narrow. Expand vocabulary to cover action-driven
expression while keeping the same 50+hits*8 formula.

This test locks in the expanded vocabulary and verifies the failing
Ch4 v31 text now scores >= 68 on both dimensions.
"""

from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.quality import CONFLICT_MARKERS, CHOICE_COST_MARKERS, _marker_score


CH4_V31_EXCERPT = """
林北心里一紧。昨晚走镖的时候，他为了练那套"趟泥步"，特意在每个路段都反复走了好几遍。
赵铁山蹲下去直接捏住他左脚靴子底，翻过来看了看。他的手指粗得像萝卜，指甲缝里嵌着洗不掉的炭灰，摸在鞋底磨损处的时候，力道精准得像在摸脉。
"你鞋底前掌磨损均匀，后跟几乎没磨。"赵铁山抬头盯着他，眼睛眯起来。
林北下意识咬牙，硬撑着说自己就是跟着队伍走。赵铁山起疑，戒备地转身，把炭盆里那根炭条又捡起来在粗布上勾了一道破绽。
张老哥说那趟镖走半路脚筋走坏了——现实里也进医院。林北认了这活，还是接下来。
"""


def test_ch4_v31_conflict_pressure_passes_gate():
    """After vocabulary expansion, Ch4-style action-driven text scores >= 68."""
    score = _marker_score(CH4_V31_EXCERPT, CONFLICT_MARKERS)
    assert score >= 68, f"conflict_pressure={score} < 68; hits={[m for m in CONFLICT_MARKERS if m in CH4_V31_EXCERPT]}"


def test_ch4_v31_choice_and_cost_passes_gate():
    score = _marker_score(CH4_V31_EXCERPT, CHOICE_COST_MARKERS)
    assert score >= 68, f"choice_and_cost={score} < 68; hits={[m for m in CHOICE_COST_MARKERS if m in CH4_V31_EXCERPT]}"


def test_action_verbs_in_conflict_vocab():
    """Action-driven conflict verbs must be part of CONFLICT_MARKERS."""
    required = {"盯", "盘问", "揭穿", "戳穿", "试探", "堵", "对峙", "破绽", "露馅", "起疑", "戒备", "怀疑"}
    missing = required - set(CONFLICT_MARKERS)
    assert not missing, f"missing action-verb conflict markers: {missing}"


def test_action_verbs_in_choice_vocab():
    """Action-driven choice/cost verbs must be part of CHOICE_COST_MARKERS."""
    required = {"咬牙", "硬撑", "扛", "顶", "认", "决定", "拼", "赌", "麻烦", "反噬"}
    missing = required - set(CHOICE_COST_MARKERS)
    assert not missing, f"missing action-verb choice markers: {missing}"


def test_neutral_text_still_scores_low():
    """Non-conflict text (pure description) must not accidentally score high."""
    neutral = "窗外阳光明媚，风轻云淡。小明坐在书桌前翻书页，翻得很慢。"
    c = _marker_score(neutral, CONFLICT_MARKERS)
    cc = _marker_score(neutral, CHOICE_COST_MARKERS)
    assert c < 65, f"neutral text got conflict={c}, should be < 65"
    assert cc < 65, f"neutral text got choice={cc}, should be < 65"


def test_marker_score_formula_unchanged():
    """Ensure _marker_score formula still is 50 + hits*8, clamped [45, 100]."""
    text = "".join(CONFLICT_MARKERS[:3])  # 3 markers → 50+24=74
    assert _marker_score(text, CONFLICT_MARKERS) == 74


if __name__ == "__main__":
    tests = [
        test_ch4_v31_conflict_pressure_passes_gate,
        test_ch4_v31_choice_and_cost_passes_gate,
        test_action_verbs_in_conflict_vocab,
        test_action_verbs_in_choice_vocab,
        test_neutral_text_still_scores_low,
        test_marker_score_formula_unchanged,
    ]
    fail = 0
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
        except AssertionError as e:
            print(f"FAIL: {t.__name__}: {e}")
            fail += 1
        except Exception as e:
            print(f"ERROR: {t.__name__}: {e}")
            fail += 1
    print(f"=== {len(tests)-fail}/{len(tests)} PASS ===")
    raise SystemExit(fail)
