from __future__ import annotations

import json
from dataclasses import dataclass

from openai import OpenAI

from app.core.config import settings


@dataclass
class LLMResponse:
    text: str
    provider: str
    model: str


class BaseLLMProvider:
    name = "base"

    def generate(self, prompt: str, *, max_tokens: int = 2000) -> LLMResponse:
        raise NotImplementedError


class DryRunProvider(BaseLLMProvider):
    name = "dry_run"

    def generate(self, prompt: str, *, max_tokens: int = 2000) -> LLMResponse:
        unit = (
            "这是干运行流程生成的测试草稿，用来验证数据库、版本、审稿、批准和发布任务链路。"
            "它不代表正式小说正文，也不应提交到任何平台。"
            "文本保留章节结构、场景压力、选择代价和章末钩子的占位位置，方便质量门禁计算长度和基础格式。"
            "真正创作时，系统必须重新调用正式模型，并经过人工审批。"
        )
        content = "测试草稿\n\n" + "\n\n".join(unit for _ in range(18)) + "\n\n输入摘要：\n" + prompt[:500]
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
        return LLMResponse(text=text, provider=self.name, model="dry-run")


class ArkOpenAIProvider(BaseLLMProvider):
    name = "ark_openai_compatible"

    def __init__(self) -> None:
        if not settings.ark_api_key or not settings.ark_base_url:
            raise RuntimeError("ARK_API_KEY and ARK_BASE_URL are required for live LLM calls")
        self.client = OpenAI(api_key=settings.ark_api_key, base_url=settings.ark_base_url)

    def generate(self, prompt: str, *, max_tokens: int = 2000) -> LLMResponse:
        result = self.client.chat.completions.create(
            model=settings.model_name,
            messages=[
                {"role": "system", "content": "你是网文生产系统里的受控写作工位，只按结构化输入生成草稿。"},
                {"role": "user", "content": prompt},
            ],
            max_tokens=max_tokens,
        )
        text = result.choices[0].message.content or ""
        return LLMResponse(text=text, provider=self.name, model=settings.model_name)


def get_provider(dry_run: bool) -> BaseLLMProvider:
    if dry_run:
        return DryRunProvider()
    return ArkOpenAIProvider()
