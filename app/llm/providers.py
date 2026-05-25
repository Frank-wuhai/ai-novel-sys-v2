from __future__ import annotations

import json
import time
from dataclasses import dataclass

from openai import OpenAI

from app.core.config import settings


@dataclass
class LLMResponse:
    text: str
    provider: str
    model: str
    prompt_chars: int = 0
    response_chars: int = 0
    estimated_prompt_tokens: int = 0
    estimated_response_tokens: int = 0
    elapsed_ms: int = 0
    usage: dict | None = None
    request_id: str = ""


class BaseLLMProvider:
    name = "base"

    def generate(
        self,
        prompt: str,
        *,
        max_tokens: int = 2000,
        temperature: float | None = None,
        response_format: dict | None = None,
    ) -> LLMResponse:
        raise NotImplementedError


class DryRunProvider(BaseLLMProvider):
    name = "dry_run"

    def generate(
        self,
        prompt: str,
        *,
        max_tokens: int = 2000,
        temperature: float | None = None,
        response_format: dict | None = None,
    ) -> LLMResponse:
        started = time.perf_counter()
        if "reviewer_json_schema" in prompt:
            text = json.dumps(
                {
                    "verdict": "pass",
                    "score": 82,
                    "strengths": ["dry-run reviewer confirms visible pressure, choice, cost, and hook"],
                    "issues": [],
                    "revision_suggestions": ["正式使用时请启用 live reviewer 获取真实审稿意见"],
                    "risk_flags": [],
                },
                ensure_ascii=False,
            )
            return LLMResponse(
                text=text,
                provider=self.name,
                model="dry-run",
                prompt_chars=len(prompt),
                response_chars=len(text),
                estimated_prompt_tokens=estimate_tokens(prompt),
                estimated_response_tokens=estimate_tokens(text),
                elapsed_ms=_elapsed_ms(started),
                usage=None,
                request_id="dry-run",
            )
        paragraphs = [
            "林澈站在旧楼天台边缘时，异象已经逼近到第三次闪烁。远处的广告牌像被看不见的手拧弯，红光一层层压下来，所有声音都被挤成细线。他知道这一章不能只躲开危机，必须把压力推到选择面前。",
            "本章用于验证最小生产闭环，但正文仍按正式章节节奏推进：开场压力先落地，能力触发随后出现，代价落地必须清晰，章末钩子要把读者推向下一章。dry-run only 只作为审计标记存在，不改变故事内的选择和后果。",
            "他闭上眼，短暂推演三条结果。第一条路会让楼下的人群安全撤离，却会暴露他的能力；第二条路能保住秘密，但那名被困的孩子会被坠落的水箱砸中；第三条路最安静，也最残忍，需要他付出一段关于母亲声音的记忆。",
            "代价不是抽象的。林澈听见脑海里有东西被擦掉，像旧磁带忽然空白。他伸手抓住栏杆，还是选择了第三条路，因为这能同时换来救人和继续追查源头的机会。收益和损耗在同一秒落地，他没有无损解决危机。",
            "水箱偏离原本轨迹，砸碎了天台角落的玻璃棚。孩子获救，人群只看见一道模糊倒影。林澈却发现倒影比现实慢了半拍，镜面里还有另一个自己抬头，看向城市中心那片没有星光的黑雾。",
            "这一发现把剧情段目标往前推了一步：异象并非偶发事件，能力也不是单纯的幸运。林澈把破碎玻璃收进掌心，疼痛提醒他保持在已知规则边界内，不能把推演当成万能答案。",
            "回到楼梯间时，他遇见熟人，对方叫出了他的名字，他却想不起那人的姓氏。选择的后果终于具象化，代价钉进日常。林澈没有解释，只问对方最近有没有见过同样不同步的倒影。",
            "对方脸色变了，说昨晚城市中心的地铁站也出现过镜面延迟。这个信息增量像一枚钩子，把林澈从单点危机拉进更大的秘密。他意识到自己追查的不是一场事故，而是一条正在扩散的剧情线。",
            "林澈沿着楼梯往下走，每一级台阶都像在提醒他刚才的交换。他不敢立刻再次使用能力，因为 Story Bible 里最重要的禁区已经写得很清楚：不得无代价解决危机，也不得推翻已登记能力限制。剧情段边界同样压在他心里，这一段必须围绕第一次代价推演继续推进。",
            "他把手机录音打开，让自己复述刚得到的线索：城市中心、地铁站、镜面延迟、黑雾源头。复述不是解释，而是防止记忆继续剥落。选择已经做出，后果也已经开始扩散，他必须在下一次危机逼近前确认能力还能承受多少损耗。",
            "楼下警笛声越来越近，新的阻碍随之出现。有人看见了天台上的倒影，有人正在寻找那个救下孩子的人。林澈如果暴露，就会失去追查秘密的主动权；如果沉默，更多人可能在下一场异象里受伤。压力没有解除，只是换成了更难的选择。",
            "他最终把碎玻璃藏进衣袋，决定先去地铁站确认源头。这个决定没有让他轻松，反而让下一章的钩子更清晰：能力还能再用几次，记忆会先失去谁，黑雾为什么知道母亲的声音。每一个问题都把读者期待推向后续。",
            "章末，黑雾深处传来一声很轻的玻璃碎裂声。林澈手机屏幕亮起，陌生号码只发来一句话：如果你还记得她的声音，就别再推演下一次。秘密、代价和新的危机同时压来，他只能继续选择。",
        ]
        content = "测试草稿\n\n" + "\n\n".join(paragraphs) + "\n\n输入摘要：\n" + prompt[:500]
        text = json.dumps(
            {
                "title": "测试草稿",
                "content": content,
                "self_check": [
                    "dry-run output only",
                    "not publishable prose",
                    "structure fields present",
                ],
                "used_brief_points": [
                    "goal",
                    "required_beats",
                    "constraints",
                ],
            },
            ensure_ascii=False,
        )
        return LLMResponse(
            text=text,
            provider=self.name,
            model="dry-run",
            prompt_chars=len(prompt),
            response_chars=len(text),
            estimated_prompt_tokens=estimate_tokens(prompt),
            estimated_response_tokens=estimate_tokens(text),
            elapsed_ms=_elapsed_ms(started),
            usage=None,
            request_id="dry-run",
        )


class ArkOpenAIProvider(BaseLLMProvider):
    name = "ark_openai_compatible"

    def __init__(self) -> None:
        if not settings.ark_api_key or not settings.ark_base_url:
            raise RuntimeError("ARK_API_KEY and ARK_BASE_URL are required for live LLM calls")
        self.client = OpenAI(api_key=settings.ark_api_key, base_url=settings.ark_base_url)

    def generate(
        self,
        prompt: str,
        *,
        max_tokens: int = 2000,
        temperature: float | None = None,
        response_format: dict | None = None,
    ) -> LLMResponse:
        started = time.perf_counter()
        kwargs = {
            "model": settings.model_name,
            "messages": [
                {"role": "system", "content": "你是网文生产系统里的受控写作工位，只按结构化输入生成草稿。"},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_tokens,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if response_format is not None:
            kwargs["response_format"] = response_format
        result = self.client.chat.completions.create(
            **kwargs,
        )
        text = result.choices[0].message.content or ""
        usage = _usage_dict(getattr(result, "usage", None))
        request_id = getattr(result, "id", "") or ""
        return LLMResponse(
            text=text,
            provider=self.name,
            model=settings.model_name,
            prompt_chars=len(prompt),
            response_chars=len(text),
            estimated_prompt_tokens=estimate_tokens(prompt),
            estimated_response_tokens=estimate_tokens(text),
            elapsed_ms=_elapsed_ms(started),
            usage=usage,
            request_id=request_id,
        )


def get_provider(dry_run: bool) -> BaseLLMProvider:
    if dry_run:
        return DryRunProvider()
    return ArkOpenAIProvider()


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    non_ascii_chars = len(text) - ascii_chars
    return max(1, round(ascii_chars / 4 + non_ascii_chars / 1.6))


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))


def _usage_dict(usage: object) -> dict | None:
    if usage is None:
        return None
    data = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = getattr(usage, key, None)
        if value is not None:
            data[key] = value
    return data or None
