from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.production_llm import parse_or_repair_json_object


@dataclass
class FakeResponse:
    text: str
    provider: str = "fake"
    model: str = "fake-json-repair"
    usage: dict | None = None
    estimated_prompt_tokens: int = 0
    estimated_response_tokens: int = 0
    elapsed_ms: int = 0
    request_id: str = "fake"


class FakeProvider:
    name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, *args, **kwargs) -> FakeResponse:
        self.calls += 1
        return FakeResponse('{"content_unit":"修复后的正文单元。","unit_note":"已修复"}')


def main() -> int:
    provider = FakeProvider()
    data = parse_or_repair_json_object(
        provider,
        response_text='{"content_unit":"坏 JSON"\n "unit_note":"少逗号"}',
        original_prompt="请输出小单元 JSON",
        expected_schema='{"content_unit":"正文","unit_note":"说明"}',
        max_tokens=1200,
        temperature=0.3,
        model="fake",
        task_label="章节生成局部返修小单元",
    )
    if data.get("content_unit") != "修复后的正文单元。" or provider.calls != 1:
        print("json repair helper did not recover malformed object")
        print(data)
        print(f"calls={provider.calls}")
        return 1
    direct = parse_or_repair_json_object(
        provider,
        response_text='```json\n{"content_unit":"直接正文","unit_note":"可读"}\n```',
        original_prompt="请输出小单元 JSON",
        expected_schema='{"content_unit":"正文","unit_note":"说明"}',
        max_tokens=1200,
        temperature=0.3,
        model="fake",
        task_label="章节生成局部返修小单元",
    )
    if direct.get("content_unit") != "直接正文" or provider.calls != 1:
        print("json repair helper should parse fenced JSON without repair")
        print(direct)
        print(f"calls={provider.calls}")
        return 1
    print("production-llm-json-repair-regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
