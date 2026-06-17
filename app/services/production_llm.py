from __future__ import annotations

import json
import re

from sqlalchemy.orm import Session

from app.core.config import settings
from app.llm.schemas import DraftOutput, StructuredOutputError, parse_draft_output
from app.models.entities import GenerationTask, LLMRequestLog
from app.services.chapter_units import evaluate_chapter_units, split_chapter_units
from app.services.llm_audit import record_llm_request
from app.services.quality import chinese_chars


def llm_usage_payload(response, *, prompt: str) -> dict:
    actual_prompt, actual_response, actual_total = actual_usage_tokens(response.usage)
    return {
        "prompt_chars": len(prompt),
        "response_chars": len(response.text),
        "estimated_prompt_tokens": response.estimated_prompt_tokens,
        "estimated_response_tokens": response.estimated_response_tokens,
        "estimated_total_tokens": response.estimated_prompt_tokens + response.estimated_response_tokens,
        "actual_prompt_tokens": actual_prompt,
        "actual_response_tokens": actual_response,
        "actual_total_tokens": actual_total,
        "elapsed_ms": response.elapsed_ms,
        "usage": response.usage,
        "request_id": response.request_id,
    }


def llm_parameter_snapshot(*, dry_run: bool, max_tokens: int, temperature: float | None, model: str | None = None) -> dict:
    return {
        "provider_mode": "dry_run" if dry_run else "live",
        "requested_model": model or settings.model_name,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }


def record_generation_llm_log(
    session: Session,
    *,
    task: GenerationTask,
    response,
    prompt_template: str,
    prompt: str,
    status: str,
    error_category: str = "",
) -> LLMRequestLog:
    actual_prompt, actual_response, actual_total = actual_usage_tokens(response.usage)
    return record_llm_request(
        session,
        book_id=task.book_id,
        task_type=task.task_type,
        generation_task_id=task.id,
        provider=response.provider,
        model=response.model,
        request_id=response.request_id,
        prompt_template=prompt_template,
        prompt_chars=len(prompt),
        response_chars=len(response.text),
        estimated_prompt_tokens=response.estimated_prompt_tokens,
        estimated_response_tokens=response.estimated_response_tokens,
        actual_prompt_tokens=actual_prompt,
        actual_response_tokens=actual_response,
        actual_total_tokens=actual_total,
        elapsed_ms=response.elapsed_ms,
        status=status,
        error_category=error_category,
    )


def parse_or_repair_draft_output(
    provider,
    *,
    response_text: str,
    original_prompt: str,
    max_tokens: int,
    temperature: float | None,
    model: str | None,
    task_label: str,
) -> DraftOutput:
    try:
        return parse_draft_output(response_text)
    except StructuredOutputError as first_exc:
        repair_max_tokens = max(max_tokens + 1500, 4500 if "修订" in task_label else 4000)
        previous_output_excerpt = response_text[:4000]
        repair_prompt = f"""
你刚才的{task_label}输出不是合法 JSON，系统无法保存章节。

请基于下面的原始任务，重新输出一个完整、合法的 JSON 对象。不要解释，不要 Markdown。

JSON 格式必须严格为：
{{
  "title": "章节标题",
  "content": "完整章节正文",
  "self_check": ["自检点1", "自检点2"],
  "used_brief_points": ["使用到的写作说明要点"]
}}

要求：
- content 必须是完整字符串，不要截断。
- 字符串内部换行必须正确转义，保证最终是合法 JSON。
- 不要输出系统提示、模型信息、草稿标记或元叙事说明。
- 如果内容过长，优先保证 JSON 合法和章节完整；正文建议控制在 3000-4500 个中文字符以内。
- 不要追加解释，不要追加第二个 JSON，不要把正文放在 JSON 外面。

上一轮错误：
{first_exc}

上一轮原始输出前 4000 字：
{previous_output_excerpt}

原始任务：
{original_prompt}
""".strip()
        try:
            repaired = provider.generate(
                repair_prompt,
                max_tokens=repair_max_tokens,
                temperature=temperature,
                model=model,
                response_format={"type": "json_object"} if provider.name != "dry_run" else None,
            )
            return parse_draft_output(repaired.text)
        except Exception as repair_exc:
            raise StructuredOutputError(f"{first_exc}; repair attempt failed: {repair_exc}") from repair_exc


def parse_or_repair_json_object(
    provider,
    *,
    response_text: str,
    original_prompt: str,
    expected_schema: str,
    max_tokens: int,
    temperature: float | None,
    model: str | None,
    task_label: str,
) -> dict:
    try:
        return _parse_json_object(response_text)
    except Exception as first_exc:
        repair_prompt = f"""
你刚才的{task_label}输出不是合法 JSON，系统无法读取。

请基于原始任务重新输出一个完整、合法的 JSON 对象。不要解释，不要 Markdown。

JSON 格式必须严格为：
{expected_schema}

要求：
- 字符串内部换行和双引号必须正确转义。
- 不要在 JSON 外追加任何文字。
- 不要输出多个 JSON。

上一轮错误：
{first_exc}

上一轮原始输出前 2500 字：
{response_text[:2500]}

原始任务：
{original_prompt}
""".strip()
        repaired = provider.generate(
            repair_prompt,
            max_tokens=max(max_tokens, 1800),
            temperature=temperature,
            model=model,
            response_format={"type": "json_object"} if provider.name != "dry_run" else None,
        )
        return _parse_json_object(repaired.text)


def expand_short_draft_output(
    provider,
    *,
    draft: DraftOutput,
    original_prompt: str,
    min_chars: int,
    max_tokens: int,
    temperature: float | None,
    model: str | None,
    task_label: str,
) -> tuple[DraftOutput, dict]:
    current = draft
    current_chars = chinese_chars(current.content)
    if current_chars >= min_chars:
        return current, {"attempted": False, "required_chars": min_chars, "actual_chars": current_chars}
    unit_results: list[dict] = []
    max_units = 8
    for unit_index in range(1, max_units + 1):
        current_chars = chinese_chars(current.content)
        if current_chars >= min_chars:
            break
        unit_prompt = f"""
你正在像真人作者一样续写{task_label}，不是一次性补摘要。

当前正文只有 {current_chars} 个中文字符，硬性最低要求是 {min_chars}。请只续写下一个约500-700中文字符的小单元。

小单元要求：
- 承接当前正文最后一个动作或后果，不要跳时间，不要总结剧情。
- 本单元必须有小目标、阻碍、人物反应、信息增量和局面微变化。
- 用动作、对话、环境和感官推进，不要写提纲，不要解释你在续写。
- 不要重写已有正文；只输出可以直接追加到正文末尾的新内容。
- 如果已经接近章末，本单元要把钩子推得更具体，但不要草草完结。

请严格输出 JSON：{{"content_unit":"可直接追加的新正文","unit_note":"本单元完成的小变化","done":false}}

原始任务：
{original_prompt}

当前正文：
{current.content[-2500:]}
""".strip()
        try:
            unit_response = provider.generate(
                unit_prompt,
                max_tokens=min(max(max_tokens // 2, 2500), 5000),
                temperature=temperature,
                model=model,
                response_format={"type": "json_object"} if provider.name != "dry_run" else None,
            )
            unit_data = parse_or_repair_json_object(
                provider,
                response_text=unit_response.text,
                original_prompt=unit_prompt,
                expected_schema='{"content_unit":"可直接追加的新正文","unit_note":"本单元完成的小变化","done":false}',
                max_tokens=min(max(max_tokens // 2, 2500), 5000),
                temperature=temperature,
                model=model,
                task_label=f"{task_label}扩写小单元",
            )
            unit_text = str(unit_data.get("content_unit") or "").strip()
            if not unit_text:
                raise ValueError("content_unit is empty")
        except Exception as exc:
            unit_results.append({"unit": unit_index, "accepted": False, "error": str(exc)})
            break
        current = DraftOutput(
            title=current.title,
            content=current.content.rstrip() + "\n\n" + unit_text,
            self_check=current.self_check[:3],
            used_brief_points=current.used_brief_points[:8],
        )
        unit_results.append(
            {
                "unit": unit_index,
                "accepted": True,
                "unit_chars": chinese_chars(unit_text),
                "total_chars": chinese_chars(current.content),
                "provider": unit_response.provider,
                "model": unit_response.model,
                **llm_usage_payload(unit_response, prompt=unit_prompt),
            }
        )
    final_chars = chinese_chars(current.content)
    current.self_check = [
        *current.self_check[:3],
        f"已按约500字小单元续写至{final_chars}中文字符，最低要求{min_chars}。",
    ]
    return current, {
        "attempted": True,
        "accepted": final_chars >= min_chars,
        "required_chars": min_chars,
        "actual_chars": final_chars,
        "previous_chars": chinese_chars(draft.content),
        "unit_results": unit_results,
    }


def repair_humanized_unit_flow(
    provider,
    *,
    draft: DraftOutput,
    original_prompt: str,
    min_chars: int,
    max_tokens: int,
    temperature: float | None,
    model: str | None,
    task_label: str,
    threshold: int = 70,
) -> tuple[DraftOutput, dict]:
    before = evaluate_chapter_units(draft.content)
    if before.score >= threshold and not before.repair_contract:
        return draft, {
            "attempted": False,
            "accepted": True,
            "threshold": threshold,
            "before": before.to_dict(),
        }
    local_draft, local_repair = repair_failed_chapter_units(
        provider,
        draft=draft,
        original_prompt=original_prompt,
        min_chars=min_chars,
        max_tokens=max_tokens,
        temperature=temperature,
        model=model,
        task_label=task_label,
        before_report=before.to_dict(),
        threshold=threshold,
    )
    if local_repair.get("accepted"):
        return local_draft, local_repair
    repair_prompt = f"""
你正在进行{task_label}的拟人化小单元返修。目标不是润色几句话，而是把整章改成连续的 300-700 字小单元生产稿。

请严格输出 JSON 对象，不要 Markdown，不要代码块，不要额外解释。

JSON 字段：
- title: 字符串，章节标题
- content: 字符串，返修后的完整章节正文
- self_check: 字符串数组，逐条说明小单元如何连续推进、哪些单元问题被修复
- used_brief_points: 字符串数组，列出保留和落实的 brief / Canon / 质量要求

小单元验收报告：
{json.dumps(before.to_dict(), ensure_ascii=False, indent=2)}

返修要求：
- 保留原章节的有效故事事实、人物关系、设定边界和章末方向，但允许重排段落与重写场景推进。
- 按 300-700 中文字符的小单元组织正文；每个单元必须有小目标、阻碍、人物反应、信息增量和局面变化。
- 每个单元都要承接上一单元动作后果，不要跳成剧情梗概，不要只写设定说明。
- 优先修复 repair_contract 中列出的单元问题；如果某单元目标、阻碍、后果或承接缺失，必须补成可见动作和后果。
- 正文里不要标“单元一/单元二”，小单元只是内部生产节奏。
- 返修后正文不得低于 {min_chars} 个中文字符；不要用 self_check 凑字数。

原始任务：
{original_prompt}

待返修正文：
{draft.content}
""".strip()
    try:
        response = provider.generate(
            repair_prompt,
            max_tokens=max(max_tokens, 5000),
            temperature=temperature,
            model=model,
            response_format={"type": "json_object"} if provider.name != "dry_run" else None,
        )
        repaired = parse_draft_output(response.text)
    except Exception as exc:
        return draft, {
            "attempted": True,
            "accepted": False,
            "mode": "whole_chapter",
            "threshold": threshold,
            "before": before.to_dict(),
            "local_repair": local_repair,
            "error": str(exc),
        }
    after = evaluate_chapter_units(repaired.content)
    before_chars = chinese_chars(draft.content)
    after_chars = chinese_chars(repaired.content)
    accepted = after.score >= before.score and after_chars >= min(min_chars, before_chars)
    if not accepted:
        return draft, {
            "attempted": True,
            "accepted": False,
            "mode": "whole_chapter",
            "threshold": threshold,
            "before": before.to_dict(),
            "after": after.to_dict(),
            "local_repair": local_repair,
            "reason": "repaired draft did not improve unit flow or preserved length",
            "provider": response.provider,
            "model": response.model,
            **llm_usage_payload(response, prompt=repair_prompt),
        }
    repaired.self_check = [
        *repaired.self_check[:4],
        f"小单元返修：{before.score}->{after.score}，单元数 {before.unit_count}->{after.unit_count}。",
    ]
    return repaired, {
        "attempted": True,
        "accepted": True,
        "mode": "whole_chapter",
        "threshold": threshold,
        "before": before.to_dict(),
        "after": after.to_dict(),
        "local_repair": local_repair,
        "provider": response.provider,
        "model": response.model,
        **llm_usage_payload(response, prompt=repair_prompt),
    }


def repair_failed_chapter_units(
    provider,
    *,
    draft: DraftOutput,
    original_prompt: str,
    min_chars: int,
    max_tokens: int,
    temperature: float | None,
    model: str | None,
    task_label: str,
    before_report: dict,
    threshold: int = 70,
    max_units: int = 3,
) -> tuple[DraftOutput, dict]:
    source_units = split_chapter_units(draft.content)
    report_units = before_report.get("units") if isinstance(before_report.get("units"), list) else []
    failed_rows = [row for row in report_units if isinstance(row, dict) and int(row.get("score") or 0) < threshold]
    if not source_units or len(source_units) < 3 or not failed_rows:
        return draft, {
            "attempted": False,
            "accepted": False,
            "mode": "local_units",
            "reason": "local repair requires at least 3 units and explicit weak units",
            "before": before_report,
        }
    current_units = [unit.text for unit in source_units]
    unit_results: list[dict] = []
    responses_usage: list[dict] = []
    for row in failed_rows[:max_units]:
        index = int(row.get("index") or 0)
        if index < 1 or index > len(current_units):
            continue
        previous_context = current_units[index - 2][-800:] if index > 1 else ""
        next_context = current_units[index][:800] if index < len(current_units) else ""
        original_unit = current_units[index - 1]
        strategy = _unit_repair_strategy(row)
        unit_prompt = f"""
你正在局部返修{task_label}的第 {index} 个小单元。只重写这一小单元，不要重写整章。

请严格输出 JSON：{{"content_unit":"返修后可直接替换原单元的正文","unit_note":"说明修复了什么"}}

本单元问题：
{json.dumps(row, ensure_ascii=False, indent=2)}

本单元返修策略：
{strategy}

局部返修要求：
- 只输出这一单元的新正文，不能带“第{index}单元”等标签。
- 长度保持在 300-700 中文字符左右；如果原单元较短，也至少补成完整场景片段。
- 必须补清小目标、阻碍、可见动作、人物反应、信息增量和单元末后果。
- 必须承接上一单元，且给下一单元留下可接的动作后果。
- 保留本单元有效事实，不要新增会推翻原始任务、Canon 或章节方向的大设定。

上一单元末尾参考：
{previous_context}

原单元：
{original_unit}

下一单元开头参考：
{next_context}

原始任务：
{original_prompt[:5000]}
""".strip()
        try:
            response = provider.generate(
                unit_prompt,
                max_tokens=min(max(max_tokens // 3, 1800), 3500),
                temperature=temperature,
                model=model,
                response_format={"type": "json_object"} if provider.name != "dry_run" else None,
            )
            data = parse_or_repair_json_object(
                provider,
                response_text=response.text,
                original_prompt=unit_prompt,
                expected_schema='{"content_unit":"返修后可直接替换原单元的正文","unit_note":"说明修复了什么"}',
                max_tokens=min(max(max_tokens // 3, 1800), 3500),
                temperature=temperature,
                model=model,
                task_label=f"{task_label}局部返修小单元",
            )
            unit_text = str(data.get("content_unit") or "").strip()
            if not unit_text:
                raise ValueError("content_unit is empty")
        except Exception as exc:
            unit_results.append({"unit": index, "accepted": False, "error": str(exc)})
            break
        current_units[index - 1] = unit_text
        unit_results.append(
            {
                "unit": index,
                "accepted": True,
                "before_score": row.get("score"),
                "before_issues": row.get("issues", []),
                "strategy": strategy,
                "unit_chars": chinese_chars(unit_text),
                "unit_note": str(data.get("unit_note") or ""),
            }
        )
        responses_usage.append(
            {
                "unit": index,
                "provider": response.provider,
                "model": response.model,
                **llm_usage_payload(response, prompt=unit_prompt),
            }
        )
    if not any(item.get("accepted") for item in unit_results):
        return draft, {
            "attempted": True,
            "accepted": False,
            "mode": "local_units",
            "before": before_report,
            "unit_results": unit_results,
            "usage": responses_usage,
        }
    candidate_content = "\n\n".join(part.strip() for part in current_units if part.strip())
    after = evaluate_chapter_units(candidate_content)
    before_score = int(before_report.get("score") or 0)
    before_chars = chinese_chars(draft.content)
    after_chars = chinese_chars(candidate_content)
    accepted = after.score >= before_score and after_chars >= min(min_chars, int(before_chars * 0.92))
    if not accepted:
        return draft, {
            "attempted": True,
            "accepted": False,
            "mode": "local_units",
            "threshold": threshold,
            "before": before_report,
            "after": after.to_dict(),
            "unit_results": unit_results,
            "usage": responses_usage,
            "reason": "local unit repair did not improve unit flow or preserved length",
        }
    repaired = DraftOutput(
        title=draft.title,
        content=candidate_content,
        self_check=[
            *draft.self_check[:3],
            f"局部返修失败小单元：{before_score}->{after.score}，保留整章结构并替换 {sum(1 for item in unit_results if item.get('accepted'))} 个单元。",
        ],
        used_brief_points=draft.used_brief_points[:8],
    )
    return repaired, {
        "attempted": True,
        "accepted": True,
        "mode": "local_units",
        "threshold": threshold,
        "before": before_report,
        "after": after.to_dict(),
        "unit_results": unit_results,
        "usage": responses_usage,
    }


def actual_usage_tokens(usage: dict | None) -> tuple[int, int, int]:
    if not usage:
        return 0, 0, 0
    prompt = int(usage.get("prompt_tokens") or 0)
    response = int(usage.get("completion_tokens") or usage.get("response_tokens") or 0)
    total = int(usage.get("total_tokens") or prompt + response)
    return prompt, response, total


def _parse_json_object(value: str) -> dict:
    text = str(value or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("JSON output must be an object")
    return data


def _unit_repair_strategy(row: dict) -> str:
    issues = set(str(item) for item in (row.get("issues") or []))
    lines: list[str] = []
    if "handoff" in issues:
        lines.append("承接断裂：第一句必须接住上一单元最后动作/后果，最后一句必须把新后果递给下一单元。")
    if "reaction" in issues:
        lines.append("人物反应弱：补出迟疑、疼痛、沉默、怀疑、愤怒、嘴硬或临场找补，让人物像活人在现场。")
    if "action" in issues:
        lines.append("动作链弱：至少安排一个可见动作改变局面，例如逼近、退后、抓起、推开、遮挡、试探、交换。")
    if "obstacle" in issues:
        lines.append("阻碍不足：加入具体人物、环境、伤势、利益、规矩或误判形成的阻力。")
    if "consequence" in issues:
        lines.append("后果没落地：让主角动作立刻换来收益、损失、暴露、误会或更大麻烦。")
    if "info_gain" in issues:
        lines.append("信息增量弱：补一条读者能看见的新线索、规则、身份、代价或局面变化。")
    if "goal" in issues:
        lines.append("目标不清：开头两三句内写清主角此刻想解决的小问题。")
    if "length" in issues:
        lines.append("长度不稳：补成一个完整 300-700 中文字符场景片段，不写成梗概。")
    if "precision" in issues:
        lines.append("表达/观察逻辑风险：把判断改成可见证据、试探过程和有限推断。")
    return "\n".join(f"- {line}" for line in lines) or "- 通用局部返修：补清目标、阻碍、动作后果、人物反应和承接点。"
