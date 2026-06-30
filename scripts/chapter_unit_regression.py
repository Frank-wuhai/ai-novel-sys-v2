from __future__ import annotations

import json

from app.llm.providers import LLMResponse, estimate_tokens
from app.llm.schemas import DraftOutput
from app.services.chapter_units import evaluate_chapter_units
from app.services.production_llm import repair_failed_chapter_units, repair_humanized_unit_flow


GOOD_TEXT = """
林照先想活下去，只能把破伞握得更紧。他听见巷口脚步逼近，先退到水缸旁，假装去看缸底裂纹。追来的差役伸手要抓他，他却把伞骨一推，伞尖挑开腰牌，发现背面刻着一个陌生暗记。差役皱眉停住，这让林照明白，对方不是来抓逃犯，而是来试探那枚腰牌的来历。于是他没有逃，反而接过腰牌，低声问：“谁让你来的？”

刚才那句话还没落地，巷外又传来马蹄声。林照决定先藏起腰牌，接着把伞递给卖馄饨的老汉，逼自己装作只是过路人。老汉沉默片刻，反而把半碗热汤推到他手边，汤底露出一截红线。林照看见红线连着桌脚，才知道这摊位也是局中一环。马蹄逼近，老汉却笑着说规矩不能破，这让林照必须选择：留下问清秘密，还是趁乱退走。

于是林照按住发疼的手腕，没有马上走。他先把红线接到伞柄，再假装喝汤，等追兵靠近时猛地抬伞。红线带翻木桌，热汤泼向马前，追兵只得勒马。可老汉因此暴露身份，下一刻被人喊出旧名。林照终于明白这条街藏着他父亲的旧部，却也知道自己欠下代价。没等追兵回神，他抓起老汉留下的纸条，转身冲进雨里。
""".strip()


BAD_TEXT = """
林照经历了一番危险，局势变得复杂。众人大概都很紧张，随后发生了很多事情，主角也得到了线索。总之这一章表现了他的成长，也说明世界里有一些秘密。后来他离开现场，事情继续发展。
""".strip()


def main() -> int:
    good = evaluate_chapter_units(GOOD_TEXT, target_min=80, target_max=220).to_dict()
    single_newline_text = GOOD_TEXT.replace("。", "。\n").replace("\n\n", "\n")
    single_newline_good = evaluate_chapter_units(single_newline_text, target_min=80, target_max=220).to_dict()
    bad = evaluate_chapter_units(BAD_TEXT, target_min=80, target_max=220).to_dict()
    repaired, repair_meta = repair_humanized_unit_flow(
        _FakeRepairProvider(),
        draft=DraftOutput(title="坏稿", content="\n\n".join([BAD_TEXT] * 12), self_check=[], used_brief_points=[]),
        original_prompt="测试提示",
        min_chars=900,
        max_tokens=4000,
        temperature=0.4,
        model="fake",
        task_label="章节生成",
    )
    local_source = "\n\n".join([GOOD_TEXT, "\n".join([BAD_TEXT] * 4), GOOD_TEXT])
    local_before = evaluate_chapter_units(local_source).to_dict()
    local_repaired, local_meta = repair_failed_chapter_units(
        _FakeLocalRepairProvider(),
        draft=DraftOutput(title="局部坏稿", content=local_source, self_check=[], used_brief_points=[]),
        original_prompt="测试提示",
        min_chars=900,
        max_tokens=4000,
        temperature=0.4,
        model="fake",
        task_label="章节生成",
        before_report=local_before,
    )
    failures: list[str] = []
    if int(good.get("unit_count") or 0) < 3:
        failures.append("good_unit_count_low")
    if int(good.get("score") or 0) < 70:
        failures.append(f"good_score_low:{good.get('score')}")
    if int(single_newline_good.get("unit_count") or 0) < 3:
        failures.append(f"single_newline_unit_count_low:{single_newline_good.get('unit_count')}")
    if int(single_newline_good.get("score") or 0) < 70:
        failures.append(f"single_newline_score_low:{single_newline_good.get('score')}")
    if not bad.get("repair_contract"):
        failures.append("bad_missing_repair_contract")
    if int(bad.get("score") or 0) >= 70:
        failures.append(f"bad_score_too_high:{bad.get('score')}")
    if not repair_meta.get("attempted") or not repair_meta.get("accepted"):
        failures.append("unit_repair_not_accepted")
    if evaluate_chapter_units(repaired.content).score < 70:
        failures.append("unit_repair_score_low")
    if not local_meta.get("accepted") or local_meta.get("mode") != "local_units":
        failures.append("local_unit_repair_not_accepted")
    if "经历了一番危险" in local_repaired.content:
        failures.append("local_unit_repair_did_not_replace_bad_unit")
    payload = {
        "status": "fail" if failures else "pass",
        "failures": failures,
        "good": {"score": good.get("score"), "unit_count": good.get("unit_count")},
        "single_newline_good": {
            "score": single_newline_good.get("score"),
            "unit_count": single_newline_good.get("unit_count"),
        },
        "bad": {
            "score": bad.get("score"),
            "unit_count": bad.get("unit_count"),
            "repair_contract": bad.get("repair_contract"),
        },
        "repair": {
            "attempted": repair_meta.get("attempted"),
            "accepted": repair_meta.get("accepted"),
            "mode": repair_meta.get("mode"),
            "before_score": (repair_meta.get("before") or {}).get("score"),
            "after_score": (repair_meta.get("after") or {}).get("score"),
        },
        "local_repair": {
            "attempted": local_meta.get("attempted"),
            "accepted": local_meta.get("accepted"),
            "mode": local_meta.get("mode"),
            "before_score": (local_meta.get("before") or {}).get("score"),
            "after_score": (local_meta.get("after") or {}).get("score"),
        },
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if failures else 0


class _FakeRepairProvider:
    name = "fake"

    def generate(
        self,
        prompt: str,
        *,
        max_tokens: int = 2000,
        temperature: float | None = None,
        response_format: dict | None = None,
        model: str | None = None,
    ) -> LLMResponse:
        content = "\n\n".join([GOOD_TEXT] * 3)
        text = json.dumps(
            {
                "title": "返修稿",
                "content": content,
                "self_check": ["按小单元连续推进", "修复目标、阻碍、后果和承接"],
                "used_brief_points": ["小单元返修合同"],
            },
            ensure_ascii=False,
        )
        return LLMResponse(
            text=text,
            provider=self.name,
            model=model or "fake",
            prompt_chars=len(prompt),
            response_chars=len(text),
            estimated_prompt_tokens=estimate_tokens(prompt),
            estimated_response_tokens=estimate_tokens(text),
            elapsed_ms=1,
            usage=None,
            request_id="fake-repair",
        )


class _FakeLocalRepairProvider:
    name = "fake"

    def generate(
        self,
        prompt: str,
        *,
        max_tokens: int = 2000,
        temperature: float | None = None,
        response_format: dict | None = None,
        model: str | None = None,
    ) -> LLMResponse:
        repaired_unit = GOOD_TEXT.replace("林照", "林照这次").replace("破伞", "油纸伞").replace("腰牌", "木牌")
        text = json.dumps(
            {
                "content_unit": repaired_unit,
                "unit_note": "补清目标、阻碍、动作后果和承接",
            },
            ensure_ascii=False,
        )
        return LLMResponse(
            text=text,
            provider=self.name,
            model=model or "fake",
            prompt_chars=len(prompt),
            response_chars=len(text),
            estimated_prompt_tokens=estimate_tokens(prompt),
            estimated_response_tokens=estimate_tokens(text),
            elapsed_ms=1,
            usage=None,
            request_id="fake-local-repair",
        )


if __name__ == "__main__":
    raise SystemExit(main())
