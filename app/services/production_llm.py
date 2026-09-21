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


_UNIT_PLACEHOLDER_LINES = {
    "可直接追加的新正文",
    "追加的新正文",
    "新增正文",
    "新正文",
    "正文",
    "返修后可直接替换原单元的正文",
    "可直接替换原单元的正文",
}

_UNIT_META_LINE_PATTERNS = [
    re.compile(r"^以下是.*(?:正文|续写|返修|小单元).*$"),
    re.compile(r"^下面是.*(?:正文|续写|返修|小单元).*$"),
    re.compile(r"^（?注[:：].*）?$"),
]


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


def _is_valid_draft_output_text(text: str) -> bool:
    """判断 text 是否能通过 _extract_json 解析为带 title+content 的草稿对象。

    比单纯 .strip() 更准确:thinking 模型偶发返回几千字纯思维链(里面没 JSON),
    这种情况应视为"无效输出",触发兜底重发。
    """
    stripped = (text or "").strip()
    if not stripped:
        return False
    try:
        from app.llm.schemas import _extract_json  # 避免循环依赖
        obj = _extract_json(stripped)
    except Exception:
        return False
    if not isinstance(obj, dict):
        return False
    return bool("title" in obj and "content" in obj and obj.get("content"))


def _empty_text_fallback(
    provider,
    *,
    original_prompt: str,
    max_tokens: int,
    temperature: float | None,
    model: str | None,
    task_label: str,
) -> str | None:
    """空文本兜底:重发原 prompt 给非 thinking 模型,避免 thinking 偶发空返回导致章失败。

    若当前已是 thinking 版,自动切到对应非 thinking 兜底(deepseek-v4-pro-thinking → deepseek-v4-pro);
    若当前本来就是非 thinking 版且也空,直接重发一次(可能是瞬时网络问题)。
    成功返回有效文本,失败返回 None。
    """
    # 兜底模型解析优先级(2026-09-21 第4.5步验收腿修复):
    # 1) 显式配置 LLM_FALLBACK_MODEL —— 指向一个非 thinking 模型;
    # 2) 模型名带 -thinking 后缀 —— 去掉后缀换非 thinking 版;
    # 3) 其余情况(如 kimi-k3 无后缀可剥)—— 同模型重发但必须抬高预算,
    #    同预算重发对"推理烧光预算返回空"是注定的二次失败。
    fallback_model = settings.llm_fallback_model or None
    fallback_max_tokens = max_tokens
    if not fallback_model and model and model.endswith("-thinking"):
        fallback_model = model[: -len("-thinking")]
    if not fallback_model and not model and settings.model_name and settings.model_name.endswith("-thinking"):
        fallback_model = settings.model_name[: -len("-thinking")]
    if not fallback_model:
        fallback_model = model
        fallback_max_tokens = max(max_tokens * 2, 16000)
    try:
        resp = provider.generate(
            original_prompt,
            max_tokens=fallback_max_tokens,
            temperature=temperature,
            model=fallback_model,
            response_format={"type": "json_object"} if provider.name != "dry_run" else None,
        )
        text = (resp.text or "").strip()
        return text if text else None
    except Exception:
        return None


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
    # 提前拦截:response_text 为空/无法解析时(thinking 模型偶发空返回或思维链里没出
    # JSON),直接切到非 thinking 兜底模型重发整段 prompt,不走同模型 repair 重试
    # (thinking 模型重发仍会空/不出 JSON)。这是 ch5/ch6 失败的根因。
    if not _is_valid_draft_output_text(response_text):
        fallback = _empty_text_fallback(
            provider,
            original_prompt=original_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            model=model,
            task_label=task_label,
        )
        if fallback is not None and _is_valid_draft_output_text(fallback):
            try:
                return parse_draft_output(fallback)
            except StructuredOutputError as exc:
                raise StructuredOutputError(
                    f"{task_label} 兜底模型也未给出合法 JSON: {exc}"
                ) from exc
        # 兜底也没拿到内容,抛错让上层重试整章
        raise StructuredOutputError(
            f"{task_label} 输出非合法 JSON(thinking 偶发空/纯思维链),兜底重发仍未拿到合法内容"
        )
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
- 如果内容过长，优先保证 JSON 合法和章节完整；正文严格控制在 1800-2500 个中文字符（上限2800）。
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


def sanitize_content_unit_text(text: str) -> str:
    """Remove JSON-schema/example labels that models sometimes echo as prose."""
    value = str(text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json|text|markdown)?\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    lines: list[str] = []
    for raw_line in value.splitlines():
        line = raw_line.strip()
        compact = re.sub(r"\s+", "", line).strip("：:。；;，,、")
        if compact in _UNIT_PLACEHOLDER_LINES:
            continue
        if line.startswith(("content_unit", '"content_unit"', "unit_note", '"unit_note"')):
            continue
        if any(pattern.match(line) for pattern in _UNIT_META_LINE_PATTERNS):
            continue
        lines.append(raw_line.rstrip())
    cleaned = "\n".join(lines).strip()
    cleaned = re.sub(r"^(?:新增正文|追加正文|返修正文|正文)\s*[:：]\s*", "", cleaned).strip()
    return cleaned


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
- content_unit 里只能放小说正文，不能放“新增正文/可直接追加的新正文/下面是”等说明标签。
- 如果已经接近章末，本单元要把钩子推得更具体，但不要草草完结。

请严格输出 JSON：{{"content_unit":"<只放小说正文，不要写标签>","unit_note":"本单元完成的小变化","done":false}}

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
                expected_schema='{"content_unit":"<只放小说正文，不要写标签>","unit_note":"本单元完成的小变化","done":false}',
                max_tokens=min(max(max_tokens // 2, 2500), 5000),
                temperature=temperature,
                model=model,
                task_label=f"{task_label}扩写小单元",
            )
            unit_text = sanitize_content_unit_text(str(unit_data.get("content_unit") or ""))
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



def compress_overlong_draft_output(
    provider,
    *,
    draft: DraftOutput,
    original_prompt: str,
    min_chars: int,
    max_chars: int,
    max_tokens: int,
    temperature: float | None,
    model: str | None,
    task_label: str,
) -> tuple[DraftOutput, dict]:
    before_chars = chinese_chars(draft.content or "")
    if before_chars <= max_chars:
        return draft, {
            "attempted": False,
            "accepted": True,
            "max_chars": max_chars,
            "actual_chars": before_chars,
        }
    target_high = min(max_chars - 80, 2500)
    target_low = max(min_chars, min(1800, target_high - 300))
    if target_low >= target_high:
        target_low = max(1200, target_high - 350)
    repair_prompt = f"""
你正在给{task_label}做超长压缩返修。当前正文 {before_chars} 个中文字符，系统硬上限 {max_chars}，超过会无法保存。

请严格输出 JSON 对象，不要 Markdown，不要解释：
{{"title":"章节标题","content":"压缩后的完整章节正文","self_check":["压缩说明"],"used_brief_points":["保留要点"]}}

压缩要求：
- 正文必须控制在 {target_low}-{target_high} 个中文字符，绝不能超过 {max_chars}。
- 保留主角目标、阻碍、关键选择、代价、回报和章末钩子。
- 只删冗余铺陈、重复解释、过密环境描写和同义反复，不要删掉因果链。
- 不要写提纲，不要概述剧情，content 只能是可直接入库的小说正文。
- 标题尽量保持原题：{draft.title}

原始任务：
{original_prompt}

待压缩正文：
{draft.content}
""".strip()
    try:
        response = provider.generate(
            repair_prompt,
            max_tokens=min(max(max_tokens // 2, 2600), 4200),
            temperature=min(0.35, float(temperature or 0.55)),
            model=model,
            response_format={"type": "json_object"} if provider.name != "dry_run" else None,
        )
        repaired = parse_draft_output(response.text)
    except Exception as exc:
        return draft, {
            "attempted": True,
            "accepted": False,
            "max_chars": max_chars,
            "before_chars": before_chars,
            "error": str(exc),
        }
    repaired.title = draft.title
    after_chars = chinese_chars(repaired.content or "")
    accepted = target_low <= after_chars <= max_chars and after_chars >= int(min(before_chars, min_chars) * 0.85)
    if not accepted:
        return draft, {
            "attempted": True,
            "accepted": False,
            "max_chars": max_chars,
            "target_low": target_low,
            "target_high": target_high,
            "before_chars": before_chars,
            "after_chars": after_chars,
            "provider": response.provider,
            "model": response.model,
            **llm_usage_payload(response, prompt=repair_prompt),
        }
    repaired.self_check = [
        *repaired.self_check[:3],
        f"超长压缩返修：{before_chars}->{after_chars}，控制在硬上限{max_chars}内。",
    ]
    return repaired, {
        "attempted": True,
        "accepted": True,
        "max_chars": max_chars,
        "target_low": target_low,
        "target_high": target_high,
        "before_chars": before_chars,
        "after_chars": after_chars,
        "provider": response.provider,
        "model": response.model,
        **llm_usage_payload(response, prompt=repair_prompt),
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
    unit_results = local_repair.get("unit_results") if isinstance(local_repair.get("unit_results"), list) else []
    if local_repair.get("attempted") and any(isinstance(item, dict) and item.get("accepted") for item in unit_results):
        return draft, {
            "attempted": True,
            "accepted": False,
            "mode": "local_units",
            "threshold": threshold,
            "before": before.to_dict(),
            "local_repair": local_repair,
            "reason": "local unit repair was rejected; skipped expensive whole-chapter repair in this transaction",
        }
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
    rejection = _unit_flow_rejection_reason(
        before=before.to_dict(),
        after=after.to_dict(),
        before_chars=before_chars,
        after_chars=after_chars,
        min_chars=min_chars,
        threshold=threshold,
        content=repaired.content,
        before_content=draft.content,
    )
    if rejection:
        return draft, {
            "attempted": True,
            "accepted": False,
            "mode": "whole_chapter",
            "threshold": threshold,
            "before": before.to_dict(),
            "after": after.to_dict(),
            "local_repair": local_repair,
            "reason": rejection,
            "provider": response.provider,
            "model": response.model,
            **llm_usage_payload(response, prompt=repair_prompt),
        }
    repaired.self_check = [
        *repaired.self_check[:4],
        f"小单元返修：{before.score}->{after.score}，单元数 {before.unit_count}->{after.unit_count}。",
    ]
    # ★扩写返修只补正文长度,不动标题:强制保留原标题,防 LLM 自作主张重起(带"第N章"前缀等)
    repaired.title = draft.title
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


def repair_lineage_consistency(
    provider,
    *,
    draft: DraftOutput,
    min_chars: int,
    max_tokens: int,
    temperature: float | None,
    model: str | None,
    task_label: str,
    prior_gained: set | None = None,
    max_rounds: int = 2,
) -> tuple[DraftOutput, dict]:
    """传承一致性守卫返修(确定性检测 + 定向LLM补写)。

    检测"功法/信物凭空到手""对话回指凭空""功法名混用"三类逻辑硬伤。
    检测由 lineage_guard.check_chapter 确定性完成(零LLM);
    发现硬伤后定向调 LLM **补写缺失的传授/获得/行礼动作**(不重写全章),
    复检硬伤清零才接受。清不掉则保留原稿并标记 accepted=False(交由上层决定是否 promote)。
    """
    from app.services.lineage_guard import check_chapter, describe_issues

    prior_gained = prior_gained or set()
    issues, _ = check_chapter(draft.title, draft.content, prior_gained)
    if not issues:
        return draft, {"attempted": False, "accepted": True, "issues_before": 0}

    rounds = []
    cur_draft = draft
    for rd in range(max_rounds):
        issues, _ = check_chapter(cur_draft.title, cur_draft.content, prior_gained)
        if not issues:
            break
        repair_prompt = f"""
你正在给一章武侠网文修复"剧情推进逻辑硬伤"。下面列出确定性检测抓到的硬伤,每一条都是"后文用到了一个前文从未交代的东西"。

【必须修复的硬伤】
{describe_issues(issues)}

【修复规则——极重要】
1. 关键传承(师父/功法/秘籍/信物)必须"先有获得,后有使用"。如果后文主角在练"纯阳功"、揣着"木牌"、行了某个"礼",那么前文必须有一个明确的场景写清楚它**怎么来的**:
   - 功法:必须有老道/师父**明确传授并说出功法名字**的动作句(如"老道枯手按在他背心,一字一句传下《纯阳功》起手吐纳"),不能只写"走了一遍纯阳功起手式"就当传过了。
   - 信物:必须有"给/递/留下/发给"的动作,不能凭空揣着。
   - 对话回指(如老道问"你那个礼谁教的"):前文必须**先写出主角行礼的动作**,老道才能问。
2. canon 传承顺序固定:先习《纯阳功》残篇 → 再松风十三剑 → 之后才是绵掌/梯云纵。不能颠倒(不能绵掌先于纯阳功传授)。
3. 只做"补链"修改:在合适位置补写缺失的传授/获得/行礼场景,让因果闭合。尽量保留原有文笔和其他情节,不要整章重写。
4. 补写后功法名前后必须一致:主角练的到底叫什么,全章用同一个名字。
5. 若硬伤是"设备来历凭空"(游戏头盔/脑机设备):在主角第一次拿起/戴上设备的地方补一句来历——是这个近未来时代人人都有的平常消费电子、还是二手淘来/内测附赠/朋友旧物/攒钱买的,并顺带点破为何一戴就有真实触觉(脑机接口/神经直连是这时代的成熟技术)。一两句话即可,不要长篇设定。绝不能让头盔凭空出现在床头。
6. 若硬伤是"单机感"(游戏世界无其他玩家):这是网游文,游戏世界里除了主角和NPC,必须有别的活人玩家的存在痕迹。在游戏内场景补入一两处轻量的玩家纹理即可——远处几个玩家身影走过、山门外有人排队拜师、公屏/世界频道飘过一行字、旁边有玩家在打怪喊话、有萌新问路等。不要喧宾夺主,点到为止,让读者知道"这个世界不止我一个人在玩"。

请严格输出 JSON 对象,不要 Markdown/代码块/解释。字段:
- title: 字符串,章节标题(可不变)
- content: 字符串,补链修复后的完整正文
- self_check: 字符串数组,逐条说明每个硬伤如何被补链修复
- used_brief_points: 字符串数组

返修后正文不得低于 {min_chars} 个中文字符。

待修复正文:
{cur_draft.content}
""".strip()
        try:
            response = provider.generate(
                repair_prompt,
                max_tokens=max(max_tokens, 12000),
                temperature=temperature,
                model=model,
                response_format={"type": "json_object"} if provider.name != "dry_run" else None,
            )
            repaired = parse_draft_output(response.text)
            # ★补链只动正文,不动标题:LLM 返修时常自作主张重起标题(甚至带"第N章"前缀),
            #   这不是传承守卫的职责。强制保留进入本函数时的原标题,杜绝标题被返修污染。
            repaired.title = draft.title
        except Exception as exc:
            rounds.append({"round": rd + 1, "error": str(exc), "issues": len(issues)})
            break
        after_issues, _ = check_chapter(repaired.title, repaired.content, prior_gained)
        after_chars = chinese_chars(repaired.content)
        rounds.append({
            "round": rd + 1,
            "issues_before": len(issues),
            "issues_after": len(after_issues),
            "chars_after": after_chars,
        })
        # 接受条件:硬伤减少 且 字数不塌
        if after_chars >= min_chars * 0.9 and len(after_issues) < len(issues):
            cur_draft = repaired
            if not after_issues:
                break

    final_issues, _ = check_chapter(cur_draft.title, cur_draft.content, prior_gained)
    accepted = len(final_issues) == 0
    if accepted and cur_draft is not draft:
        cur_draft.self_check = [
            *cur_draft.self_check[:4],
            f"传承守卫返修:{len(issues)}处硬伤→0,补链修复完成。",
        ]
    return cur_draft, {
        "attempted": True,
        "accepted": accepted,
        "issues_before": len(issues),
        "issues_after": len(final_issues),
        "remaining": describe_issues(final_issues) if final_issues else "",
        "rounds": rounds,
    }


def repair_semantic_issues(
    provider,
    *,
    draft: DraftOutput,
    min_chars: int,
    max_tokens: int,
    temperature: float | None,
    model: str | None,
    task_label: str,
    max_rounds: int = 3,
) -> tuple[DraftOutput, dict]:
    """语义返修门(LLM 探针诊断 S1-S7 + 定向补写)。

    ★这是对"确定性关键词守卫无法判定语义充分性(来源/活人氛围/情绪落点)"这一架构
      边界的正解:用能判语义的 LLM 探针做门,命中 S1-S7 就把带锚点的缺陷喂回 LLM
      定向补写,循环到探针零命中或到达 max_rounds。
    ★只做缺陷诊断+定向修复,不打分、不做入库判定(遵守"不靠单次LLM评分入库"铁律)。
    ★探针用通用中文模型(deepseek-v4-pro),不是生成用的代码/thinking 模型。
    """
    from app.services.semantic_gate import (
        probe_semantic, hits_from_probe, build_repair_instruction,
    )

    probe = probe_semantic(draft.content, provider=provider)
    hits = hits_from_probe(probe)
    if probe.get("error"):
        # 探针本身失败(no_json 等):不阻断流程,标记未审,交上层
        return draft, {"attempted": False, "accepted": True, "probe_error": probe.get("error"), "hits_before": 0}
    if not hits:
        return draft, {"attempted": False, "accepted": True, "hits_before": 0}

    rounds = []
    cur_draft = draft
    hits_before = len(hits)
    for rd in range(max_rounds):
        probe = probe_semantic(cur_draft.content, provider=provider)
        hits = hits_from_probe(probe)
        if not hits:
            break
        repair_prompt = f"""
你正在给一章武侠网文(玄幻武侠+现实同步题材:主角白天现实送外卖照顾病父,晚上戴设备进《入梦》游戏,在清虚观拜师练功,游戏所得同步现实身体)做"语义质量返修"。下面是资深责编逐条指出的语义缺陷,每条都带原文锚点和修复要求。

【必须修复的语义缺陷】
{build_repair_instruction(hits)}

【修复规则——极重要】
1. 只做"定向补写/改写"对应缺陷处,尽量保留原有文笔、情节骨架和其他没问题的段落,不要整章重写、不要改变主线剧情走向。
2. 补写要自然融入上下文,像原作者一手写成,不能有补丁痕迹。
3. 世界观 canon 不可违背:清虚观、《纯阳功》残篇、松风十三剑、断剑承影;传承顺序 纯阳功→松风十三剑→绵掌/梯云纵;现实是2040年代脑机接口消费级普及的近未来;金手指=游戏所练睡眠中同步现实肉体。
4. 叙事哲学:读者要"爽"不是"惨",正向反馈详写、苦难略写,武侠世界的奇观/武学/变强要写透。
5. 标题不要改动。

请严格输出 JSON 对象,不要 Markdown/代码块/解释。字段:
- title: 字符串,章节标题(保持不变)
- content: 字符串,语义返修后的完整正文
- self_check: 字符串数组,逐条说明每个语义缺陷如何被修复
- used_brief_points: 字符串数组

返修后正文不得低于 {min_chars} 个中文字符。

待修复正文:
{cur_draft.content}
""".strip()
        try:
            response = provider.generate(
                repair_prompt,
                max_tokens=max(max_tokens, 12000),
                temperature=temperature,
                model=model,
                response_format={"type": "json_object"} if provider.name != "dry_run" else None,
            )
            repaired = parse_draft_output(response.text)
            # 语义返修只动正文,标题保持不变
            repaired.title = draft.title
        except Exception as exc:
            rounds.append({"round": rd + 1, "error": str(exc), "hits": len(hits)})
            break
        after_probe = probe_semantic(repaired.content, provider=provider)
        after_hits = hits_from_probe(after_probe)
        after_chars = chinese_chars(repaired.content)
        rounds.append({
            "round": rd + 1,
            "hits_before": len(hits),
            "hits_after": len(after_hits),
            "chars_after": after_chars,
            "codes_before": [h["code"] for h in hits],
            "codes_after": [h["code"] for h in after_hits],
        })
        # 接受条件(收紧):字数不塌 且 命中减少 且 不引入新缺陷类别。
        #   旧逻辑"总数减少即接受"会顾此失彼——修好 S2/S7 却把 S6 挤掉、或补写引入
        #   新的 S1。要求新命中集合 ⊆ 旧命中集合(codes),杜绝"修A带出B"。
        before_codes = {h["code"] for h in hits}
        after_codes = {h["code"] for h in after_hits}
        introduced_new = bool(after_codes - before_codes)
        if after_chars >= min_chars * 0.9 and len(after_hits) < len(hits) and not introduced_new:
            cur_draft = repaired
            if not after_hits:
                break
        elif after_chars >= min_chars * 0.9 and not introduced_new and after_codes < before_codes:
            # 命中数持平但严格清掉了某些项(集合真子集)也接受,继续下一轮攻剩余项
            cur_draft = repaired

    final_probe = probe_semantic(cur_draft.content, provider=provider)
    final_hits = hits_from_probe(final_probe)
    accepted = len(final_hits) == 0
    if accepted and cur_draft is not draft:
        cur_draft.self_check = [
            *cur_draft.self_check[:4],
            f"语义返修门:{hits_before}处语义缺陷→0,S1-S7全清。",
        ]
    return cur_draft, {
        "attempted": True,
        "accepted": accepted,
        "hits_before": hits_before,
        "hits_after": len(final_hits),
        "remaining": [h["code"] for h in final_hits],
        "rounds": rounds,
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

请严格输出 JSON：{{"content_unit":"<只放替换后的小说正文，不要写标签>","unit_note":"说明修复了什么"}}

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
- content_unit 里只能放小说正文，不能放“返修后可直接替换原单元的正文/下面是”等说明标签。

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
                expected_schema='{"content_unit":"<只放替换后的小说正文，不要写标签>","unit_note":"说明修复了什么"}',
                max_tokens=min(max(max_tokens // 3, 1800), 3500),
                temperature=temperature,
                model=model,
                task_label=f"{task_label}局部返修小单元",
            )
            unit_text = sanitize_content_unit_text(str(data.get("content_unit") or ""))
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
    rejection = _unit_flow_rejection_reason(
        before=before_report,
        after=after.to_dict(),
        before_chars=before_chars,
        after_chars=after_chars,
        min_chars=min_chars,
        threshold=threshold,
        content=candidate_content,
        before_content=draft.content,
    )
    if rejection:
        return draft, {
            "attempted": True,
            "accepted": False,
            "mode": "local_units",
            "threshold": threshold,
            "before": before_report,
            "after": after.to_dict(),
            "unit_results": unit_results,
            "usage": responses_usage,
            "reason": rejection,
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


def _unit_flow_rejection_reason(
    *,
    before: dict,
    after: dict,
    before_chars: int,
    after_chars: int,
    min_chars: int,
    threshold: int,
    content: str,
    before_content: str = "",
) -> str:
    before_score = int(before.get("score") or 0)
    after_score = int(after.get("score") or 0)
    before_units = int(before.get("unit_count") or 0)
    after_units = int(after.get("unit_count") or 0)
    if after_score < before_score:
        return f"unit flow regressed: {before_score}->{after_score}"
    if after_chars < min(min_chars, int(before_chars * 0.92)):
        return "repaired draft did not preserve required length"
    if before_units and after_units > max(before_units + 2, int(before_units * 1.25)):
        return f"unit count expanded suspiciously: {before_units}->{after_units}"
    if _repeated_long_segment_count(content) > _repeated_long_segment_count(before_content):
        return "repaired draft contains repeated long segments"
    if after_score < threshold:
        return f"unit flow remains below threshold: {after_score}<{threshold}"
    return ""


def _has_repeated_long_segments(content: str) -> bool:
    return _repeated_long_segment_count(content) > 0


def _repeated_long_segment_count(content: str) -> int:
    repeats = 0
    seen: set[str] = set()
    for paragraph in re.split(r"\n{2,}", content or ""):
        normalized = re.sub(r"\s+", "", paragraph)
        if len(normalized) < 60:
            continue
        key = normalized[:180]
        if key in seen:
            repeats += 1
            continue
        seen.add(key)
    seen.clear()
    for part in re.split(r"(?:\n{2,}|[。！？])", content or ""):
        normalized = re.sub(r"\s+", "", part)
        if len(normalized) < 60:
            continue
        key = normalized[:140]
        if key in seen:
            repeats += 1
            continue
        seen.add(key)
    return repeats


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
