"""成文判据判卷 (prose_judgement_v1 J1-J5) — 2026-09-10 第 3 步接线。

定位（对齐 prose_judgement_v1）：
- 成文判据不是门禁：不打分、不给 verdict、不自动 FAIL、不自动拦稿；
  产出缺口表 {criterion, anchor, explanation, fix_direction} 交人工裁决退修或放行。
- 判卷方式：LLM 判卷，温度 0（settings.prose_judge_temperature 默认 0.0）、
  固定判卷 prompt（prose_judgement@v1）、固定模型（settings.prose_judge_model）。
- 每条缺口必须落到原文锚点；判不出锚点的判定在解析层丢弃并计数 dropped_no_anchor。

与 _run_llm_chapter_review 的关系：同一 pattern（DB 提示词模板 + GenerationTask
审计 + record_generation_llm_log），但结果是报告 JSON 里的兄弟节 prose_judgement，
与 llm_review（主编审稿，接 editorial_gate）物理分开，互不影响 passed/score。
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.core.config import settings
from app.llm.providers import get_provider
from app.llm.schemas import parse_prose_judgement_output
from app.models.entities import Book, ChapterVersion, GenerationTask
from app.services.llm_errors import classify_exception
from app.services.production_llm import (
    llm_parameter_snapshot,
    llm_usage_payload,
    record_generation_llm_log,
)
from app.services.prompts import get_prompt_template, render_template, seed_prompt_templates


def run_prose_judgement(
    session: Session,
    *,
    book: Book | None,
    version: ChapterVersion,
    chapter_number: int,
    dry_run: bool,
) -> dict:
    """对章节正文跑 J1-J5 成文判卷，返回缺口表 dict（写入报告 prose_judgement 节）。

    失败时不抛异常：返回 {"status": "failed", ...}，保证判卷故障不阻塞质检主流程
    （与 _run_style_review 的失败不阻塞口径一致）。
    """
    if not book:
        return {"status": "failed", "error_category": "validation", "error": "book not found"}
    seed_prompt_templates(session)
    template = get_prompt_template(session, name="prose_judgement", version="v1")
    prompt = render_template(
        template,
        book_title=book.title,
        genre=book.genre,
        chapter_content=version.content,
    )
    provider = get_provider(dry_run)
    model = settings.prose_judge_model
    temperature = settings.prose_judge_temperature
    llm_parameters = llm_parameter_snapshot(
        dry_run=dry_run,
        max_tokens=settings.prose_judge_max_tokens,
        temperature=temperature,
        model=model,
    )
    input_json = {
        "chapter_number": chapter_number,
        "dry_run": dry_run,
        "prompt_template": f"{template.name}@{template.version}",
        "llm_parameters": llm_parameters,
        "version_id": version.id,
    }
    try:
        response = provider.generate(
            prompt,
            max_tokens=settings.prose_judge_max_tokens,
            temperature=temperature,
            model=model,
        )
        judgement = parse_prose_judgement_output(response.text)
    except Exception as exc:  # noqa: BLE001 — 判卷失败不阻塞质检主流程
        classification = classify_exception(exc)
        task = GenerationTask(
            book_id=book.id,
            task_type="prose_judgement",
            status="failed",
            input_json=json.dumps(input_json, ensure_ascii=False),
            output_json=json.dumps(
                {
                    "error_category": classification.category,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "llm_parameters": llm_parameters,
                },
                ensure_ascii=False,
            ),
        )
        session.add(task)
        session.flush()
        return {
            "status": "failed",
            "generation_task_id": task.id,
            "error_category": classification.category,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    task = GenerationTask(
        book_id=book.id,
        task_type="prose_judgement",
        status="completed",
        input_json=json.dumps(input_json, ensure_ascii=False),
        output_json=json.dumps(
            {
                "version_id": version.id,
                "provider": response.provider,
                "model": response.model,
                "llm_parameters": llm_parameters,
                **llm_usage_payload(response, prompt=prompt),
                "judgement": judgement.to_dict(),
            },
            ensure_ascii=False,
        ),
    )
    session.add(task)
    session.flush()
    record_generation_llm_log(
        session,
        task=task,
        response=response,
        prompt_template=f"{template.name}@{template.version}",
        prompt=prompt,
        status="completed",
    )
    return {
        "status": "completed",
        "generation_task_id": task.id,
        "provider": response.provider,
        "model": response.model,
        "request_id": response.request_id,
        **judgement.to_dict(),
    }
