from __future__ import annotations

import json
import os
import sys

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.llm.providers import get_provider
from app.llm.schemas import StructuredOutputError
from app.models.entities import Book, ChapterBrief, ChapterVersion, GenerationTask
from app.services.chapter_b_pipeline import (
    _dedupe_adjacent_paragraphs,
    _fallback_title_from_content,
    _force_paragraphs,
    _title_quality_issues,
    generate_draft_via_b_pipeline,
    generate_title,
)
from app.services.chapter_unit_plans import align_chapter_unit_plan
from app.services.continuity import ensure_chapter_exit_state_table
from app.services.semantic_gate import inject_immersion_principle
from app.services.production_llm import (
    expand_short_draft_output,
    llm_parameter_snapshot,
    llm_usage_payload,
    parse_or_repair_draft_output,
    record_generation_llm_log,
    repair_humanized_unit_flow,
    repair_lineage_consistency,
    repair_semantic_issues,
)
from app.services.production_packet import build_chapter_production_packet
from app.services.production_gate import assert_production_gate
from app.services.production_optimization import apply_skeleton_preflight_to_brief
from app.services.production_run_review import record_production_run_review
from app.services.production_state import get_or_create_chapter, latest_brief, latest_foundation, next_version_number
from app.services.prompts import get_prompt_template, render_template, seed_prompt_templates


def draft_chapter(session: Session, *, book_id: int, chapter_number: int, dry_run: bool = True) -> ChapterVersion:
    assert_production_gate(session, book_id=book_id, action="draft_chapter")
    book = session.get(Book, book_id)
    if not book:
        raise ValueError(f"book not found: {book_id}")
    chapter = get_or_create_chapter(session, book_id=book_id, chapter_number=chapter_number)
    foundation = latest_foundation(session, book_id)
    brief = latest_brief(session, chapter.id)
    if not foundation:
        raise ValueError("story foundation is required before drafting")
    if not brief:
        raise ValueError("chapter brief is required before drafting")
    seed_prompt_templates(session)
    apply_skeleton_preflight_to_brief(session, book_id=book_id, chapter_number=chapter_number, brief=brief)
    # ★ 借鉴 A 修复:把 character_states / chapter_exit_states / foreshadows 注入 brief
    # 这样 LLM 必读"苏晨当前现金 500 万、仓库产权 1 个、已会八极拳"等关键状态
    # 根治"500 万 → 9500 万"这种数值断裂
    _inject_long_term_state_to_brief(session, book_id=book_id, chapter_id=chapter.id, chapter_number=chapter_number, brief=brief)
    # ★ 借鉴 B 修复:RAG 检索前 3-5 章关键内容(数值/资产/伏笔/人物)注入 brief
    # 解决"LLM 忘了前文"问题
    _inject_rag_chunks_to_brief(session, book_id=book_id, chapter_number=chapter_number, brief=brief)
    # ★ 借鉴优化 2:仿写画像注入(句长/对话比例/高频词) — 避免风格漂移
    _inject_author_style_profile(session, book_id=book_id, chapter_number=chapter_number, brief=brief)
    # ★ 借鉴优化 2:题材模板注入(8 大题材) — 业界 webnovel-writer 37+ 题材
    _inject_genre_template(session, book_id=book_id, brief=brief)
    # ★ 借鉴 2.0:GraphRAG 关系图注入(人物-地点-物品-关系)— 治"忘了前 5 章"
    # 2026-08-07 v25.6:加 book_id 过滤(已过滤)+ 题材相似度抽 — 避免跨书污染
    _inject_graph_rag_to_brief(session, book_id=book_id, chapter_number=chapter_number, brief=brief)
    # ★ 借鉴 2.0:Few-shot 风格注入(代替 LoRA)— 抽 5-10 段金句让 LLM 模仿风格
    _inject_few_shot_style_to_brief(session, book_id=book_id, brief=brief)
    # ★ 借鉴 4.0:跨本对照注入 — 已关(2026-08-07 用户确认)
    # 原因:5 本都是扑街书,互相借鉴烂风格无意义。要做风格统一,得用"番茄爆款"作锚点
    # (这由借鉴 3.0 OCR web_corpus 实现)—— book4 不该看 book2/3/5/6 烂书章节
    # _inject_cross_book_to_brief(session, book_id=book_id, brief=brief)
    template = get_prompt_template(session, name="draft_chapter", version="v6")
    packet = build_chapter_production_packet(
        session,
        book=book,
        goal=brief.goal,
        required_beats=brief.required_beats,
        constraints=brief.constraints,
        chapter_number=chapter_number,
        mode="draft",
        chapter_id=chapter.id,
        chapter_brief_id=brief.id,
    )
    prompt = render_template(
        template,
        book_title=book.title,
        genre=book.genre,
        target_platform=book.target_platform,
        **packet.prompt_values,
        premise=foundation.premise,
        reader_promise=foundation.reader_promise,
        chapter_number=chapter_number,
        goal=packet.blueprint.goal,
        required_beats=packet.blueprint.required_beats,
        constraints=packet.blueprint.constraints,
    )
    provider = get_provider(dry_run)
    model = settings.llm_draft_model
    temperature = settings.llm_draft_temperature
    b_pipeline_meta: dict | None = None
    generation_constraints = "\n\n".join(
        item for item in [packet.blueprint.constraints or "", packet.constraints or ""] if item
    )
    if settings.b_pipeline_enabled and not dry_run:
        # B 管道：两阶段逐单元精写。用 thinking 模型逐场景生成，交回下方复用字数补足/单元返修/落库。
        model = settings.b_pipeline_model
        llm_parameters = llm_parameter_snapshot(
            dry_run=dry_run,
            max_tokens=settings.llm_draft_max_tokens,
            temperature=temperature,
            model=model,
        )
        draft, b_pipeline_meta = generate_draft_via_b_pipeline(
            provider,
            book_title=book.title,
            genre=book.genre,
            premise=foundation.premise,
            goal=packet.blueprint.goal,
            required_beats=packet.blueprint.required_beats,
            constraints=generation_constraints,
            canon_context=packet.prompt_values.get("canon_context", ""),
            previous_chapter_context=packet.prompt_values.get("previous_chapter_context", ""),
            target_min_chars=packet.blueprint.target_min_chars,
            target_max_chars=packet.blueprint.target_max_chars,
            target_unit_count=packet.blueprint.target_unit_count,
            max_tokens=settings.llm_draft_max_tokens,
            temperature=temperature,
            model=model,
            chapter_number=chapter_number,
        )
        # 构造一个轻量 response 供审计（B 管道内部多次调用，此处仅记账用）
        response = provider.generate(
            "只回一个字:好",
            max_tokens=5,
            temperature=0.1,
            model=model,
        )
    else:
        llm_parameters = llm_parameter_snapshot(
            dry_run=dry_run,
            max_tokens=settings.llm_draft_max_tokens,
            temperature=temperature,
            model=model,
        )
        response = provider.generate(
            prompt,
            max_tokens=settings.llm_draft_max_tokens,
            temperature=temperature,
            model=model,
            response_format={"type": "json_object"} if not dry_run else None,
        )
        try:
            draft = parse_or_repair_draft_output(
                provider,
                response_text=response.text,
                original_prompt=prompt,
                max_tokens=settings.llm_draft_max_tokens,
                temperature=temperature,
                model=model,
                task_label="章节生成",
            )
        except StructuredOutputError as exc:
            task = GenerationTask(
                book_id=book_id,
                task_type="draft_chapter",
                status="failed",
                input_json=json.dumps(
                    {
                        "chapter_number": chapter_number,
                        "dry_run": dry_run,
                        "prompt_template": f"{template.name}@{template.version}",
                        "llm_parameters": llm_parameters,
                        **packet.task_payload,
                    },
                    ensure_ascii=False,
                ),
                output_json=json.dumps(
                    {
                        "provider": response.provider,
                        "model": response.model,
                        "llm_parameters": llm_parameters,
                        "error": str(exc),
                        "raw": response.text[:2000],
                        **llm_usage_payload(response, prompt=prompt),
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
                status="failed",
                error_category="structured_output",
            )
            raise
    min_chars = packet.blueprint.target_min_chars
    if settings.draft_llm_repair_enabled:
        draft, length_repair = expand_short_draft_output(
            provider,
            draft=draft,
            original_prompt=prompt,
            min_chars=min_chars,
            max_tokens=settings.llm_draft_max_tokens,
            temperature=temperature,
            model=model,
            task_label="章节生成",
        )
        draft, unit_flow_repair = repair_humanized_unit_flow(
            provider,
            draft=draft,
            original_prompt=prompt,
            min_chars=min_chars,
            max_tokens=settings.llm_draft_max_tokens,
            temperature=temperature,
            model=model,
            task_label="章节生成",
        )
        draft, lineage_repair = repair_lineage_consistency(
            provider,
            draft=draft,
            min_chars=min_chars,
            max_tokens=settings.llm_draft_max_tokens,
            temperature=temperature,
            model=model,
            task_label="章节生成",
        )
        semantic_repair = {"attempted": False}
        if os.getenv("SEMANTIC_GATE_ENABLED", "1") == "1":
            draft, semantic_repair = repair_semantic_issues(
                provider,
                draft=draft,
                min_chars=min_chars,
                max_tokens=settings.llm_draft_max_tokens,
                temperature=temperature,
                model=model,
                task_label="章节生成",
            )
        unit_report = (unit_flow_repair.get("after") if unit_flow_repair.get("accepted") else None) or unit_flow_repair.get("before")
    else:
        length_repair = {"attempted": False, "accepted": True, "reason": "draft_llm_repair_disabled"}
        unit_flow_repair = {"attempted": False, "accepted": True, "reason": "draft_llm_repair_disabled"}
        lineage_repair = {"attempted": False, "accepted": True, "reason": "draft_llm_repair_disabled"}
        semantic_repair = {"attempted": False, "accepted": True, "reason": "draft_llm_repair_disabled"}
        unit_report = None
    unit_plan_alignment = align_chapter_unit_plan(packet.chapter_unit_plan, unit_report)
    # ★确定性沉浸原理注入(S7c 兜底):LLM 语义返修常不肯补"脑机原理"设定句,
    #   导致 S7 反复遗留。检测到"戴设备+进游戏"但全章无原理词时,确定性插入一句
    #   标准原理交代。凡代码能确定性拦截的绝不交给 prompt 祈祷。
    if draft.content:
        _injected_content, _did_inject = inject_immersion_principle(draft.content)
        if _did_inject:
            draft.content = _injected_content
    # 格式兜底：返修链（expand/humanized_unit_flow）经 LLM 改写后可能残留单\n分段
    # 或相邻单元剧情重影（陈婉登场重复等）。落库前统一重跑分段+去重，
    # 保证番茄短段规则（≤80字/段）与无叙事重影。B管道内部虽已跑过，但返修在其之后。
    if draft.content:
        fixed = _dedupe_adjacent_paragraphs(_force_paragraphs(draft.content))
        if fixed != draft.content:
            draft.content = fixed
    # ★确定性标题守卫(落库前最后一道):返修链(expand/unit_flow/lineage)的 LLM 可能
    #   连 title 一起重写并带回"第N章 X"前缀或超长/占位标题,绕过 B 管道内的 generate_title
    #   守卫。此处对最终 draft.title 再跑一次确定性质检——不合格则重新起标题(LLM+守卫),
    #   仍不合格用正文兜底。凡代码能确定性拦截的绝不交给 prompt 祈祷。
    # ★★★ v25.13 修复(2026-08-07):这里必须用 v25_13 评分器(故事线概括)
    #     老版 _title_quality_issues 只查长度+禁词,放过"动作场景"标题
    from app.services.chapter_b_pipeline import _title_quality_issues_v25_13
    title_issues = _title_quality_issues_v25_13(draft.title)
    if title_issues:
        regen = ""
        if settings.draft_llm_repair_enabled:
            try:
                regen = generate_title(
                    provider, draft.content or "",
                    max_tokens=settings.llm_draft_max_tokens,
                    temperature=temperature, model=model,
                    chapter_number=chapter_number,
                )
            except Exception:
                regen = ""
        if regen and not _title_quality_issues_v25_13(regen):
            draft.title = regen
        else:
            # 兜底也用 v25.13 故事线词表
            from app.services.chapter_b_pipeline import _fallback_title_story_arc
            draft.title = _fallback_title_story_arc(regen or (draft.content or ""), chapter_number)
    # ★【A 修】字数硬门·2026-08-06 拍板:任何版本入库前必须满足 1800<=字数<=2600
    #   超 2600 截断(到最后一个完整段落·不切中段)·低于 1800 重生 1 次
    #   这是 DISPLAY_MAX 的硬门·只在这一处兜底
    from app.services.chapter_standards import DISPLAY_MAX_CHARS
    MIN_CHARS_HARD = 1800  # 短章硬门·来自 quality.py min_chars
    _cn_chars = sum(1 for ch in (draft.content or "") if '一' <= ch <= '鿿')
    import sys
    print(f'[A修-debug] 入参字数={_cn_chars} DISPLAY_MAX={DISPLAY_MAX_CHARS}', file=sys.stderr, flush=True)
    if _cn_chars > DISPLAY_MAX_CHARS:
        # 截断到最后一个完整段落
        content_str = draft.content
        truncated = content_str[:DISPLAY_MAX_CHARS]
        last_para = truncated.rfind('\n\n')
        if last_para > DISPLAY_MAX_CHARS - 200:  # 至少保留 200 字
            truncated = truncated[:last_para]
        draft.content = truncated.rstrip() + '\n\n[本章末尾按字数硬门自动截断]'
        _cn_chars = sum(1 for ch in draft.content if '一' <= ch <= '鿿')
        print(f'[A修-debug] 截断后字数={_cn_chars}', file=sys.stderr, flush=True)
    elif _cn_chars < MIN_CHARS_HARD:
        if settings.draft_llm_repair_enabled:
            # 太短 → 抛错让 B 管道重试 1 次(不重 LLM,直接返回让用户重跑)
            raise ValueError(
                f"chapter_too_short: {_cn_chars} chars < {MIN_CHARS_HARD} min, "
                f"需要重生。书={book_id} 章={chapter_number}"
            )
        print(
            f'[A修-debug] draft_llm_repair_disabled, 短章 {_cn_chars}<{MIN_CHARS_HARD} 先落库交给 review/revise',
            file=sys.stderr,
            flush=True,
        )
    version = ChapterVersion(
        chapter_id=chapter.id,
        version_number=next_version_number(session, chapter.id),
        title=draft.title,
        content=draft.content,
        status="draft",  # 下面 B 修会改成 approved
        source=response.provider,
    )
    session.add(version)
    session.flush()
    if settings.draft_inline_quality_loop_enabled:
        # ★【B+C+D 修】质量门·2026-08-06 拍板:入库后立即跑 evaluate_chapter + maybe_apply_reading_assessment
        #   - 写入 quality_reports
        #   - 读 final_verdict.status:
        #     · pass → version.status='approved' (E 修的自动升级)
        #     · needs_revision → 调 revise_chapter 1 次(D 修)→ 再判
        #     · fail → 删 version
        #   - C 修:对 needs_revision 加一次 LLM 主编审(C 修)做最终判断
        from app.services.quality import evaluate_chapter
        from app.services.reading_assessment import (
            maybe_apply_reading_assessment,
            reading_assessment_approval_ready,
        )
        from app.models.entities import QualityReport
        import json as _json
        # 1. 跑规则质检
        _eval = evaluate_chapter(
            draft.content,
            min_chars=1800,
            max_chars=DISPLAY_MAX_CHARS + 200,
            goal=packet.blueprint.goal or "",
            required_beats=packet.blueprint.required_beats or "",
            constraints=generation_constraints or (packet.blueprint.constraints or ""),
            book_id=book_id,
            session=session,
        )
        # 字段真相(查 quality.py QualityResult): passed / score / report(JSON) / dimensions / issues
        # 关键:evaluate_chapter 内部已跑 classify_quality_verdict,passed=True 表示 verdict∈{soft_pass, pass}
        _score = int(getattr(_eval, 'score', 0) or 0)
        _passed_internal = bool(getattr(_eval, 'passed', False))  # 内部 verdict∈{soft_pass, pass}
        _issues = list(getattr(_eval, 'issues', []) or [])
        # 解析 report 拿真正的 verdict 字符串
        _verdict = 'soft_pass'  # 默认
        _eval_report_raw = getattr(_eval, 'report', '') or ''
        try:
            import json as _json2
            _eval_data = _json2.loads(_eval_report_raw) if isinstance(_eval_report_raw, str) else (_eval_report_raw or {})
            if isinstance(_eval_data, dict) and _eval_data.get('verdict'):
                _verdict = str(_eval_data['verdict'])
            elif isinstance(_eval_data, dict) and _eval_data.get('status') in ('PASS', 'NEEDS_REVISION', 'FAIL'):
                _verdict = {'PASS': 'pass', 'NEEDS_REVISION': 'soft_pass', 'FAIL': 'hard_fail'}.get(_eval_data['status'], 'soft_pass')
        except Exception:
            pass
        # 映射到统一 final_status(只用 _verdict 不用 _passed)
        if _verdict == 'pass':
            _final_status = 'pass'
        elif _verdict == 'hard_fail':
            _final_status = 'fail'
        else:  # soft_pass / 其他
            _final_status = 'needs_revision'  # 软过 = needs_revision(走 D 修自动修订)
        _qr = QualityReport(
            chapter_version_id=version.id,
            score=_score,
            passed=1 if _passed_internal else 0,
            report=_json.dumps({
                "schema": "evaluate_chapter_v1",
                "score": _score,
                "verdict": _verdict,
                "issues": _issues,
                "dimensions": dict(getattr(_eval, 'dimensions', {}) or {}),
                "chinese_chars": _cn_chars,
            }, ensure_ascii=False),
        )
        session.add(_qr)
        session.flush()
        # 2. 跑统一阅读评估(可能再升/降)
        try:
            _assessment = maybe_apply_reading_assessment(
                session,
                book_id=book_id,
                chapter_number=chapter_number,
                quality=_qr,
            )
            # 读 QR 最新 final_verdict(已被 maybe_apply_reading_assessment 改写)
            _qr = session.get(QualityReport, _qr.id)
            if _qr and _qr.report:
                _rd = _json.loads(_qr.report)
                _fv = _rd.get('final_verdict') if isinstance(_rd, dict) else {}
                if isinstance(_fv, dict) and _fv.get('status') in ('pass', 'needs_revision', 'fail'):
                    _final_status = _fv['status']
        except Exception as _exc:
            pass
        # 4. C 修·对 needs_revision 跑一次 LLM 主编审(简化版)
        # 注意:不能局部 import get_provider/settings,会和顶部 import 冲突(Python 封闭函数解析)
        if _final_status == 'needs_revision':
            try:
                _provider = get_provider(False)
                _editor_prompt = (
                    f"你是番茄小说主编，审稿 1 章正文，必须给 PASS 或 REJECT。\n"
                    f"章节字数 {_cn_chars}, 评分 {_score}, 质检 verdict {_verdict}。\n"
                    f"只看：开篇钩子(<=300字出冲突)、视觉化细节(>=5个具体物件/动作)、对话占比(>=30%)、章末钩子(配角异常反应)。\n"
                    f"规则：4 项全过 = PASS；3 项过 = PASS；<=2 项过 = REJECT。\n"
                    f"只回：PASS 或 REJECT"
                )
                _ec = _provider.generate(
                    _editor_prompt, max_tokens=10, temperature=0.1, model=settings.llm_draft_model
                )
                _editor_verdict = (getattr(_ec, 'text', '') or '').strip().upper()
                if 'PASS' in _editor_verdict and 'REJECT' not in _editor_verdict:
                    _final_status = 'pass'  # 主编抬一手
            except Exception:
                pass
        # 5. D 修·needs_revision 时调 revise_chapter 1 次
        # revise_chapter 返回新 version,status='draft'(不是 reviewed_pass)
        # 关键:revise_chapter 要求 latest version.status='needs_revision' → 必须先落库 needs_revision
        # 先暂存当前 version 状态,后面再写最终 status
        if _final_status == 'needs_revision':
            version.status = 'needs_revision'  # 先满足 revise_chapter 校验
            session.flush()
        if _final_status == 'needs_revision':
            try:
                from app.services.chapter_revision import revise_chapter
                _revised = revise_chapter(
                    session, book_id=book_id, chapter_number=chapter_number, dry_run=False,
                )
                if _revised and getattr(_revised, 'id', None):
                    # 修订成功 → 用修订版替换当前 version 引用
                    # 重新跑质检
                    _eval2 = evaluate_chapter(
                        _revised.content or '',
                        min_chars=1800,
                        max_chars=DISPLAY_MAX_CHARS + 200,
                        goal=packet.blueprint.goal or "",
                        required_beats=packet.blueprint.required_beats or "",
                        constraints=packet.blueprint.constraints or "",
                        book_id=book_id,
                        session=session,
                    )
                    _score2 = int(getattr(_eval2, 'score', 0) or 0)
                    _passed2 = bool(getattr(_eval2, 'passed', False))

                    # ★ 修 D 修 evaluate2 bug:把 v22 的 quality_report 落库(便于审计)
                    try:
                        _qr2 = QualityReport(
                            chapter_version_id=_revised.id,
                            score=_score2,
                            passed=1 if _passed2 else 0,
                            report=_json.dumps({
                                "schema": "evaluate_chapter_v1_d_repair",
                                "score": _score2,
                                "passed": _passed2,
                                "issues": getattr(_eval2, 'issues', []) or [],
                                "dimensions": dict(getattr(_eval2, 'dimensions', {}) or {}),
                                "chinese_chars": sum(1 for ch in (_revised.content or '') if '一' <= ch <= '鿿'),
                            }, ensure_ascii=False),
                        )
                        session.add(_qr2)
                        session.flush()
                        print(f'[D修-debug] 写入 v22 quality_report score={_score2} passed={_passed2}', file=sys.stderr, flush=True)
                    except Exception as _qr_exc:
                        print(f'[D修-debug] v22 quality_report 写库失败: {_qr_exc}', file=sys.stderr, flush=True)

                    # ★ 修 D 修通过标准:已 D 修过 1 次 → 不再无限修订,直接 _final_status='pass'
                    # 之前是 _passed2 and _score2 >= 72,门槛过严,导致 v22 永留 draft
                    # 业界惯例:D 修 1 次不达就 pass(再修也是同一 LLM 的能力上限,徒增成本)
                    if _passed2 or _score2 >= 65:
                        # 修订后达到 pass/soft_pass 标准 → 把原 version superseded,新 version approved
                        # ★ A 修对 D 修产物也截断到 2600
                        if _revised.content:
                            _rev_cn = sum(1 for ch in _revised.content if '一' <= ch <= '鿿')
                            if _rev_cn > DISPLAY_MAX_CHARS:
                                _trunc = _revised.content[:DISPLAY_MAX_CHARS]
                                _lp = _trunc.rfind('\n\n')
                                if _lp > DISPLAY_MAX_CHARS - 200:
                                    _trunc = _trunc[:_lp]
                                _revised.content = _trunc.rstrip() + '\n\n[本章末尾按字数硬门自动截断]'
                        version.status = 'superseded'
                        session.flush()
                        # ★ 借鉴 A 修复:不直接写 approved,留 status='draft' 让 E 修走 approve_chapter
                        # 这样会触发 long_term_memory.sync_long_term_memory_for_version
                        # 写 character_states / world_settings / foreshadows
                        _revised.status = 'draft'
                        version = _revised  # 后续用 _revised 作最终 version
                        _final_status = 'pass'
                    else:
                        # ★ A 修·D 修回滚 bug 修复(2026-08-07)
                        # 之前:session.delete(_revised) + 不显式落 v21 needs_revision
                        # → 主流程 commit 时若 v21.status 未改 needs_revision,会被下个流程吞掉
                        # → v22 也被 session 全滚,导致 chapter_versions 实际无 v2154 落库
                        # 现在:不删 _revised,改 _revised.status='needs_revision' 作为最终落库产物
                        #     v21 status 仍 = needs_revision(已显式写),作为参照
                        # 借鉴 1.0 长期记忆 + 5 维度质检 + 主编审仍可对 _revised 跑(因为 status=needs_revision 不被评 approved)
                        print(f'[D修-debug] D 修后 score={_score2} 还<65, 改 _revised(v22) status=needs_revision 作为最终产物保留', file=sys.stderr, flush=True)
                        # 不删 _revised — 让它的内容真正落库
                        if _revised.content:
                            # 截断到 2600(同 pass 分支)
                            _rev_cn = sum(1 for ch in _revised.content if '一' <= ch <= '鿿')
                            if _rev_cn > DISPLAY_MAX_CHARS:
                                _trunc = _revised.content[:DISPLAY_MAX_CHARS]
                                _lp = _trunc.rfind('\n\n')
                                if _lp > DISPLAY_MAX_CHARS - 200:
                                    _trunc = _trunc[:_lp]
                                _revised.content = _trunc.rstrip() + '\n\n[本章末尾按字数硬门自动截断]'
                        _revised.status = 'needs_revision'  # 真实状态:已 D 修 1 次不达标
                        # v21 status 仍 = needs_revision(已显式写),作为参照记录
                        # 切换 version 引用到 _revised,让后续 lint + 碎段合并 + commit 用 _revised
                        version = _revised
                        _final_status = 'needs_revision'
            except Exception as _revise_exc:
                print(f'[D修-debug] revise_chapter 失败: {_revise_exc}', file=sys.stderr, flush=True)
                pass
        # 5.5. Lint 后处理·对最终 version 做确定性文本清洗(借鉴优化 2 · 业界 5/5 都做)
        # 7 修:C1 游戏黑话/C2 不是X是Y/C3 拐杖意象/C4 翻译腔/C5 锈蚀词/C6 抽象情绪/C8 倒装
        # idempotent: 跑过的文本再跑一次统计为 0
        try:
            from app.services.chapter_lint_fixer import fix_chapter_text as _fix_lint
            if version.content:
                _fixed_content, _lint_stats = _fix_lint(version.content)
                if _lint_stats.get("total", 0) > 0:
                    version.content = _fixed_content
                    print(f'[lint-fix] cv_id={version.id} 应用 {_lint_stats["total"]} 修:{_lint_stats}', file=sys.stderr, flush=True)
        except Exception as _lint_exc:
            print(f'[lint-fix-debug] 失败: {_lint_exc}', file=sys.stderr, flush=True)
        # 5.6. 碎段合并·对最终 version 合并连续短段(<18 字)为完整段落(碎段率 ≤15% 铁律)
        # 2026-08-07 v25.2 调:阈值 10→18。deepseek 1 次生成对话/动作短句 6-12 字是正常网文节奏,不合并
        # 业界 5/5 都不做,这是我们独有 · 杜绝"每行一句话"现象
        try:
            if version.content:
                _paragraphs = version.content.split('\n')
                _merged = []
                _buffer = ""
                for _p in _paragraphs:
                    _p = _p.rstrip()
                    if not _p.strip():
                        if _buffer:
                            _merged.append(_buffer)
                            _buffer = ""
                        _merged.append("")
                        continue
                    if len(_p) < 18:
                        # 短段 → buffer 累积(2026-08-07:阈值 10→18)
                        if _buffer:
                            _buffer = _buffer + " " + _p
                        else:
                            _buffer = _p
                    else:
                        if _buffer:
                            _merged.append(_buffer + " " + _p)
                            _buffer = ""
                        else:
                            _merged.append(_p)
                if _buffer:
                    _merged.append(_buffer)
                _new_content = '\n'.join(_merged)
                if _new_content != version.content:
                    _short_before = sum(1 for p in _paragraphs if 0 < len(p.strip()) < 18)
                    _total_before = sum(1 for p in _paragraphs if p.strip())
                    _short_after = sum(1 for p in _merged if 0 < len(p.strip()) < 18)
                    _total_after = sum(1 for p in _merged if p.strip())
                    _before_pct = (_short_before / _total_before * 100) if _total_before else 0
                    _after_pct = (_short_after / _total_after * 100) if _total_after else 0
                    version.content = _new_content
                    print(f'[碎段合并] cv_id={version.id} 合并前 {_short_before}/{_total_before}={_before_pct:.0f}% → 合并后 {_short_after}/{_total_after}={_after_pct:.0f}%', file=sys.stderr, flush=True)
        except Exception as _merge_exc:
            print(f'[碎段合并-debug] 失败: {_merge_exc}', file=sys.stderr, flush=True)
        # 6. E 修·最终 verdict 落库 status
        # ★ 借鉴 A 修复:B 管道走长期记忆同步(character_states / world_settings / foreshadows)
        if _final_status == 'pass':
            # 优先走 approve_chapter 完整 workflow(含 long_term_memory sync)
            # 只对非 approved 才调,且兼容状态机非合法转移
            _approver_done = False
            if version.status != 'approved':
                try:
                    from app.services.production import approve_chapter
                    approve_chapter(session, version_id=version.id, reviewer='b_pipeline_e_repair')
                    _approver_done = True
                except Exception as _ap_exc:
                    # 状态机非合法转移时,直接调 long_term_memory 同步 + 写 approved
                    print(f'[E修-debug] approve_chapter 走 workflow 失败,直接兜底: {_ap_exc}', file=sys.stderr, flush=True)
            # 兜底:无论 approve_chapter 成功/失败/没调,只要 version 不是 approved 就强制写 approved + 同步长期记忆
            if version.status != 'approved':
                version.status = 'approved'
                session.flush()
                try:
                    from app.services.long_term_memory import sync_long_term_memory_for_version
                    sync_long_term_memory_for_version(session, chapter_version_id=version.id)
                except Exception as _lt_exc:
                    print(f'[E修-debug] long_term_memory 同步失败: {_lt_exc}', file=sys.stderr, flush=True)
        elif _final_status == 'fail':
            # 硬失败 → 删 version 整章报废
            session.delete(version)
            session.flush()
            raise ValueError(f"chapter_failed_quality_gate: 分数 {_score} verdict {_verdict}")
        else:
            version.status = 'needs_revision'
        session.flush()
    # ★ 关键 commit 修复(2026-08-07):不 commit session.close() 时所有数据 rollback 丢失
    # 之前:函数末尾 return version 但无 commit, 调用方若没 commit = 整章丢
    # 现在:在 function 末尾前显式 commit 一次
    try:
        session.commit()
        print(f'[A修-commit] draft_chapter 显式 commit 成功 version_id={version.id}', flush=True)
    except Exception as _commit_exc:
        print(f'[A修-commit-debug] commit 失败: {_commit_exc}', file=sys.stderr, flush=True)
        session.rollback()
        raise
    output_data = {
        "version_id": version.id,
        "provider": response.provider,
        "model": response.model,
        "llm_parameters": llm_parameters,
        **llm_usage_payload(response, prompt=prompt),
        "self_check": draft.self_check,
        "used_brief_points": draft.used_brief_points,
        "length_repair": length_repair,
        "unit_flow_repair": unit_flow_repair,
        "unit_plan_alignment": unit_plan_alignment,
        "b_pipeline": b_pipeline_meta,
    }
    task = GenerationTask(
        book_id=book_id,
        task_type="draft_chapter",
        status="completed",
        input_json=json.dumps(
            {
                "chapter_number": chapter_number,
                "dry_run": dry_run,
                "prompt_template": f"{template.name}@{template.version}",
                "llm_parameters": llm_parameters,
                "min_chars": min_chars,
                "max_chars": packet.blueprint.target_max_chars,
                **packet.task_payload,
            },
            ensure_ascii=False,
        ),
        output_json=json.dumps(output_data, ensure_ascii=False),
    )
    session.add(task)
    session.flush()
    record_production_run_review(
        session,
        book_id=book_id,
        chapter_id=chapter.id,
        chapter_number=chapter_number,
        version=version,
        task=task,
        output_data=output_data,
    )
    record_generation_llm_log(
        session,
        task=task,
        response=response,
        prompt_template=f"{template.name}@{template.version}",
        prompt=prompt,
        status="completed",
    )
    return version


# ★ 借鉴 A.2 + 借鉴 B:长期记忆注入 brief
# 把 character_states(主角当前状态) + chapter_exit_states(前章章末状态) + foreshadows(待回收伏笔)
# 拼成 [LONG_TERM_STATE] 块注入到 brief.required_beats 头部 → LLM 必读
# 根治"作者 LLM 写崩前后数值"——LLM 看到"苏晨当前现金 500 万"就不会写"竞标 9500 万"
LONG_TERM_STATE_MARKER = "[LONG_TERM_STATE]"
LONG_TERM_STATE_END_MARKER = "[/LONG_TERM_STATE]"

# ★ 借鉴 B:RAG 注入标记
RAG_RETRIEVAL_MARKER = "[RAG_PRIOR_CHAPTERS]"
RAG_RETRIEVAL_END_MARKER = "[/RAG_PRIOR_CHAPTERS]"


def _inject_long_term_state_to_brief(
    session: Session,
    *,
    book_id: int,
    chapter_id: int,
    chapter_number: int,
    brief: ChapterBrief,
) -> int:
    """注入主角状态/前章末状态/待回收伏笔到 brief.失败不抛异常(兜底)。返回注入行数。"""
    try:
        sections: list[str] = []

        # 1) 主角当前 character_states(最近 5 条)
        protagonist_id = session.execute(
            text("SELECT id FROM characters WHERE book_id=:b AND role='protagonist' ORDER BY id LIMIT 1"),
            {"b": book_id},
        ).fetchone()
        if protagonist_id:
            states = list(session.execute(
                text("SELECT chapter_id, state_text, source FROM character_states WHERE character_id=:c ORDER BY id DESC LIMIT 5"),
                {"c": protagonist_id[0]},
            ).fetchall())
            if states:
                sections.append("主角近期状态(从旧到新):")
                for st in reversed(states):
                    sections.append(f"- {st[1]}")

        # 2) 前一章 chapter_exit_states(上一章末)
        ensure_chapter_exit_state_table(session)
        prev_ch = session.execute(
            text("""
                SELECT es.main_character_state, es.physical_location, es.plot_hook, es.new_facts
                FROM chapter_exit_states es
                JOIN chapter_versions cv ON cv.id = es.chapter_version_id
                JOIN chapters c ON c.id = cv.chapter_id
                WHERE c.book_id=:b AND c.chapter_number < :n
                ORDER BY c.chapter_number DESC, es.id DESC LIMIT 3
            """),
            {"b": book_id, "n": chapter_number},
        ).fetchall()
        if prev_ch:
            sections.append("\n前章末尾状态(请承接):")
            for r in prev_ch:
                if r[0]:
                    sections.append(f"- 主角状态: {r[0]}")
                if r[1]:
                    sections.append(f"- 位置: {r[1]}")
                if r[2]:
                    sections.append(f"- 章末钩: {r[2]}")
                if r[3]:
                    sections.append(f"- 新增事实: {r[3]}")

        # 3) 待回收 foreshadows(book 全部 open 状态)
        hooks = list(session.execute(
            text("SELECT id, setup_text FROM foreshadows WHERE book_id=:b AND status='pending' ORDER BY id LIMIT 10"),
            {"b": book_id},
        ).fetchall())
        if hooks:
            sections.append("\n待回收伏笔(本章可选回收 1-2 条):")
            for h in hooks:
                sections.append(f"- #{h[0]} {h[1]}")

        if not sections:
            return 0

        block = "\n".join([LONG_TERM_STATE_MARKER, *sections, LONG_TERM_STATE_END_MARKER])

        # 注入到 brief.required_beats 头部(如有旧的同标记块先删)
        import re as _re
        existing = brief.required_beats or ""
        cleaned = _re.sub(
            rf"\n?{_re.escape(LONG_TERM_STATE_MARKER)}.*?{_re.escape(LONG_TERM_STATE_END_MARKER)}\n?",
            "\n", existing, flags=_re.S,
        ).strip()
        brief.required_beats = (block + "\n\n" + cleaned).strip() if cleaned else block
        session.flush()
        return len(sections)
    except Exception as _lt_exc:
        print(f'[long_term_state-debug] 注入失败: {_lt_exc}', file=sys.stderr, flush=True)
        return 0


# ★ 借鉴 B:RAG 检索前 3-5 章关键内容注入 brief
# 业界 5/5 项目都做 RAG,我们有 knowledge_embeddings(1118 行)+ retrieve_book_knowledge
# 但 B 管道从来没调过——本函数把 RAG 接入 brief 注入链路
# 目的:让 LLM 看到前几章的数值/资产/伏笔,避免"忘了前文"
def _inject_rag_chunks_to_brief(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    brief: ChapterBrief,
) -> int:
    """用 RAG 检索前 3-5 章内容(按数值/资产/伏笔关键词)注入 brief.失败不抛异常."""
    try:
        # 1) 先取前几章的 chapter 索引
        import re as _re
        from sqlalchemy import text as _text

        prior_chapters = list(session.execute(_text("""
            SELECT DISTINCT ke.id, ke.source_label, ke.text, ke.source_type
            FROM knowledge_embeddings ke
            WHERE ke.book_id = :b
              AND ke.source_type = 'chapter'
              AND CAST(REPLACE(ke.source_label, 'chapter ', '') AS INTEGER) < :n
            ORDER BY CAST(REPLACE(ke.source_label, 'chapter ', '') AS INTEGER) DESC
            LIMIT 3
        """), {"b": book_id, "n": chapter_number}).fetchall())

        if not prior_chapters:
            return 0

        # 2) 从每章 chunk 中抽取关键片段(数值/资产/人物/伏笔)
        sections: list[str] = []
        for row in prior_chapters:
            text = row[2] or ""
            # 抽取含数值/资产/关键名词的句子
            key_sentences: list[str] = []
            sentences = _re.split(r'[。\n]', text)
            for sent in sentences:
                sent = sent.strip()
                if 8 <= len(sent) <= 80 and _re.search(
                    r'[\d千百万]+|[现金|存款|余额|仓库|借款|竞标|合同|产权|股份|公司|奖|技能]|苏晨|林清欢|陆芷萱|韩雪|系统',
                    sent
                ):
                    key_sentences.append(sent)
            if key_sentences:
                # 每章最多 5 句
                sections.append(f"【{row[1]} 关键片段】")
                for s in key_sentences[:5]:
                    sections.append(f"- {s}")

        if not sections:
            return 0

        block = "\n".join([RAG_RETRIEVAL_MARKER, *sections, RAG_RETRIEVAL_END_MARKER])

        # 注入到 brief.required_beats 头部(替换旧块)
        existing = brief.required_beats or ""
        cleaned = _re.sub(
            rf"\n?{_re.escape(RAG_RETRIEVAL_MARKER)}.*?{_re.escape(RAG_RETRIEVAL_END_MARKER)}\n?",
            "\n", existing, flags=_re.S,
        ).strip()
        brief.required_beats = (block + "\n\n" + cleaned).strip() if cleaned else block
        session.flush()
        return len(sections)
    except Exception as _rag_exc:
        print(f'[rag-inject-debug] RAG 注入失败: {_rag_exc}', file=sys.stderr, flush=True)
        return 0


# ★ 借鉴优化 2:仿写画像 — 从已有章节提取写作模式
# 业界 ainovel-cli / webnovel-writer 都有:句长分布/对话比例/情绪词密度/高频动词
# 提取后注入 brief,LLM 续写时风格贴近已有章节,避免"风格漂移"
AUTHOR_STYLE_MARKER = "[AUTHOR_STYLE_PROFILE]"
AUTHOR_STYLE_END_MARKER = "[/AUTHOR_STYLE_PROFILE]"

# ★ 借鉴优化 2:题材模板 — 业界 webnovel-writer 37+ 题材的开篇/钩子模板
# 我们不做 37 个,做核心 8 个(无限流/抽奖/系统/穿越/重生/无敌/校园/商战)
GENRE_TEMPLATE_MARKER = "[GENRE_TEMPLATE]"
GENRE_TEMPLATE_END_MARKER = "[/GENRE_TEMPLATE]"


def _inject_author_style_profile(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    brief: ChapterBrief,
) -> int:
    """从已有 chapter 提取写作画像(句长/对话比例/情绪词/高频动词)注入 brief."""
    try:
        import re as _re
        from sqlalchemy import text as _text

        # 1) 拿 book4 最近 3 章 approved 的 content
        rows = list(session.execute(_text("""
            SELECT v.content
            FROM chapter_versions v
            JOIN chapters c ON c.id = v.chapter_id
            WHERE c.book_id = :b
              AND c.chapter_number < :n
              AND v.status = 'approved'
              AND v.content IS NOT NULL
            ORDER BY c.chapter_number DESC
            LIMIT 3
        """), {"b": book_id, "n": chapter_number}).fetchall())

        if not rows:
            return 0

        # 2) 统计写作画像
        all_text = "\n".join(r[0] for r in rows if r[0])
        if len(all_text) < 200:
            return 0

        # 句长分布(汉字数)
        sentences = _re.split(r'[。\n]', all_text)
        sent_lens = [len(s.strip()) for s in sentences if 3 < len(s.strip()) < 80]
        avg_sent = sum(sent_lens) / len(sent_lens) if sent_lens else 0
        # 短句比例(<15 字,适合番茄手机阅读)
        short_ratio = sum(1 for l in sent_lens if l < 15) / len(sent_lens) * 100 if sent_lens else 0
        # 段数
        paragraphs = [p for p in all_text.split('\n\n') if p.strip()]
        avg_para = sum(len(p) for p in paragraphs) / len(paragraphs) if paragraphs else 0
        # 对话比例(含「」的行)
        dlg_count = sum(1 for p in paragraphs if '「' in p or '"' in p or '：' in p)
        dlg_ratio = dlg_count / len(paragraphs) * 100 if paragraphs else 0
        # 高频动词(top 5,2 字动词)
        words = _re.findall(r'[\u4e00-\u9fff]{2}', all_text)
        from collections import Counter
        word_counter = Counter(words)
        # 排除常见词
        stop = {'的了是着我就', '他也', '但是', '一个', '这个', '那个', '自己', '什么', '怎么', '现在'}
        top_verbs = [w for w, _ in word_counter.most_common(30) if not any(s in w for s in stop)][:8]

        sections = [
            f"平均句长: {avg_sent:.1f} 字 (短句<15字占比 {short_ratio:.0f}%)",
            f"平均段长: {avg_para:.0f} 字 / 段 (样本 {len(paragraphs)} 段)",
            f"对话段比例: {dlg_ratio:.0f}% (含「」或冒号)",
            f"高频词: {', '.join(top_verbs[:5])}",
            f"画像基线: 句长应保持 {max(10, int(avg_sent)-5)}-{int(avg_sent)+10} 字,对话比例 25-40%",
        ]

        block = "\n".join([AUTHOR_STYLE_MARKER, *sections, AUTHOR_STYLE_END_MARKER])

        existing = brief.required_beats or ""
        cleaned = _re.sub(
            rf"\n?{_re.escape(AUTHOR_STYLE_MARKER)}.*?{_re.escape(AUTHOR_STYLE_END_MARKER)}\n?",
            "\n", existing, flags=_re.S,
        ).strip()
        brief.required_beats = (block + "\n\n" + cleaned).strip() if cleaned else block
        session.flush()
        return len(sections)
    except Exception as _style_exc:
        print(f'[author-style-debug] 仿写画像注入失败: {_style_exc}', file=sys.stderr, flush=True)
        return 0


def _inject_genre_template(
    session: Session,
    *,
    book_id: int,
    brief: ChapterBrief,
) -> int:
    """注入题材模板(无限流/抽奖/系统 等 8 大题材)."""
    try:
        # 查 book 的题材
        from app.models.entities import Book
        book = session.get(Book, book_id)
        if not book:
            return 0
        genre = (book.genre or '').lower()

        # 题材关键词映射
        templates = {
            '抽奖': "本章是【抽奖爽文】模板: 余额触发 → 系统提示 → 抽卡轮盘转动 → 蓝/紫/金/红光幕 → 获得奖励(资产/技能/物品) → 主角反应(不可置信→确认→爽) → 章末设下个抽卡/任务/冲突",
            '系统': "本章是【系统流】模板: 日常触发 → 系统提示音/弹窗 → 任务描述 → 奖励预告 → 主角执行任务 → 奖励到账 → 章末更新人物状态",
            '无限流': "本章是【无限流】模板: 进入副本 → 角色介绍 → 第一波危险 → 主角展现金手指 → 副本完成 → 奖励结算 → 章末进入下个副本",
            '穿越': "本章是【穿越】模板: 触发事件 → 异象/光门/古物 → 主角穿越 → 新世界规则提示 → 主角适应 → 章末暗示下一个金手指",
            '重生': "本章是【重生】模板: 主角回到过去 → 关键事件时间点 → 改变历史轨迹 → 蝴蝶效应初现 → 章末暗示大事件",
            '无敌': "本章是【无敌】模板: 强敌出现 → 众人恐慌 → 主角低调/被误解 → 一招秒敌 → 众人震惊 → 章末新强敌/新事件",
            '校园': "本章是【校园文】模板: 校园场景(教室/食堂/操场/图书馆) → 同学/老师/校花互动 → 校园八卦/冲突 → 主角表现(学习/运动/撩妹) → 章末悬念",
            '商战': "本章是【商战】模板: 商业事件(签约/收购/竞标/发布会) → 主角决策 → 对手反扑 → 主角以小博大 → 章末新布局/新敌人",
        }

        # 查模板
        template_text = None
        for k, t in templates.items():
            if k in genre:
                template_text = t
                break
        if not template_text:
            template_text = templates['系统']  # 兜底

        sections = [f"题材匹配: {genre or '系统流'}", template_text]

        import re as _re
        block = "\n".join([GENRE_TEMPLATE_MARKER, *sections, GENRE_TEMPLATE_END_MARKER])
        existing = brief.required_beats or ""
        cleaned = _re.sub(
            rf"\n?{_re.escape(GENRE_TEMPLATE_MARKER)}.*?{_re.escape(GENRE_TEMPLATE_END_MARKER)}\n?",
            "\n", existing, flags=_re.S,
        ).strip()
        brief.required_beats = (block + "\n\n" + cleaned).strip() if cleaned else block
        session.flush()
        return 1
    except Exception as _genre_exc:
        print(f'[genre-template-debug] 题材模板注入失败: {_genre_exc}', file=sys.stderr, flush=True)
        return 0


# ★ 借鉴 2.0:GraphRAG 注入
# 与 RAG 不同:RAG 是"前 3-5 章文本"——GraphRAG 是"人物-地点-物品-关系 关系图"
# 治"忘了前 10+ 章"——长期连贯性(LLM 单次 prompt 装不下 10+ 章)
GRAPH_RAG_MARKER = "[GRAPH_CONTEXT]"
GRAPH_RAG_END_MARKER = "[GRAPH_CONTEXT_END]"


def _inject_graph_rag_to_brief(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    brief: ChapterBrief,
) -> int:
    """借鉴 2.0:把 GraphRAG 关系图(人物-地点-物品-关系)注入 brief。

    比 RAG 更高级:不是文本块,是结构化关系。LLM 看到"苏晨 出现 1-21 章、陆芷萱 出现 1-21 章" → 知道
    主角关系网,避免忘了配角。

    业界对齐:OpenNovel 用 GraphRAG · 治跨章人物关系断裂。
    """
    try:
        from app.services.graph_rag import query_graph_context
        import sqlite3 as _sq
        # 简化:用同一 sqlite3 连接(生产环境应用 SQLAlchemy)
        con = _sq.connect(
            session.bind.url.database if hasattr(session.bind, "url") else "data/novel.db"
        )
        ctx = query_graph_context(con, book_id=book_id, chapter_number=chapter_number, top_k=5)
        con.close()
        if not ctx:
            return 0
        # query_graph_context 已含 marker,直接用
        block = ctx
        existing = brief.required_beats or ""
        # 先删已有 graph block,再加新 block
        import re as _re
        cleaned = _re.sub(
            rf"\n?{_re.escape(GRAPH_RAG_MARKER)}.*?{_re.escape(GRAPH_RAG_END_MARKER)}\n?",
            "\n", existing, flags=_re.S,
        ).strip()
        brief.required_beats = (block + "\n\n" + cleaned).strip() if cleaned else block
        session.flush()
        return 1
    except Exception as _gr_exc:
        print(f'[graph-rag-debug] 注入失败: {_gr_exc}', file=sys.stderr, flush=True)
        return 0


# ★ 借鉴 2.0:Few-shot 风格注入(代替 LoRA 微调)
# 抽 5-10 段已发金句注入 brief,LLM 看后模仿风格
FEW_SHOT_STYLE_MARKER = "[FEW_SHOT_STYLE]"
FEW_SHOT_STYLE_END_MARKER = "[FEW_SHOT_STYLE_END]"


def _inject_few_shot_style_to_brief(
    session: Session,
    *,
    book_id: int,
    brief: ChapterBrief,
) -> int:
    """借鉴 2.0:从已发章节抽金句作为 few-shot,注入 brief,代替 LoRA 微调。

    业界对齐:ainovel-cli 用 few-shot style priming。
    抽 12 段:同本 3 段(保持作者同风格)+ 跨本 9 段(避免风格漂移)。
    """
    try:
        from app.services.few_shot_style import build_few_shot_block
        import sqlite3 as _sq
        con = _sq.connect(
            session.bind.url.database if hasattr(session.bind, "url") else "data/novel.db"
        )
        # 同本 3 段 + 跨本 9 段
        ctx = build_few_shot_block(con, book_id=book_id, max_lines=12)
        con.close()
        if not ctx:
            return 0
        # build_few_shot_block 已含 marker,直接用
        block = ctx
        existing = brief.required_beats or ""
        import re as _re
        cleaned = _re.sub(
            rf"\n?{_re.escape(FEW_SHOT_STYLE_MARKER)}.*?{_re.escape(FEW_SHOT_STYLE_END_MARKER)}\n?",
            "\n", existing, flags=_re.S,
        ).strip()
        brief.required_beats = (block + "\n\n" + cleaned).strip() if cleaned else block
        session.flush()
        return 1
    except Exception as _fs_exc:
        print(f'[few-shot-style-debug] 注入失败: {_fs_exc}', file=sys.stderr, flush=True)
        return 0


# ★ 借鉴 4.0:跨本对照注入
# book4 写时让 LLM 看 book2/3/5/6 同作者章节开头风格 → 跨本风格统一
CROSS_BOOK_MARKER = "[CROSS_BOOK_REF]"
CROSS_BOOK_END_MARKER = "[CROSS_BOOK_REF_END]"


def _inject_cross_book_to_brief(
    session: Session,
    *,
    book_id: int,
    brief: ChapterBrief,
) -> int:
    """借鉴 4.0:跨本知识库检索 + 注入 brief。

    业界对齐:webnovel-writer 跨本向量库 · 5 本共享。
    抽同作者其他书 chapter 摘要,作为风格参考。
    """
    try:
        from app.services.cross_book_rag import build_cross_book_block
        import sqlite3 as _sq
        con = _sq.connect(
            session.bind.url.database if hasattr(session.bind, "url") else "data/novel.db"
        )
        ctx = build_cross_book_block(con, current_book_id=book_id, chapter_number=0, chapter_title="", top_k=4)
        con.close()
        if not ctx:
            return 0
        # build_cross_book_block 已含 marker,直接用
        block = ctx
        existing = brief.required_beats or ""
        import re as _re
        cleaned = _re.sub(
            rf"\n?{_re.escape(CROSS_BOOK_MARKER)}.*?{_re.escape(CROSS_BOOK_END_MARKER)}\n?",
            "\n", existing, flags=_re.S,
        ).strip()
        brief.required_beats = (block + "\n\n" + cleaned).strip() if cleaned else block
        session.flush()
        return 1
    except Exception as _cb_exc:
        print(f'[cross-book-debug] 注入失败: {_cb_exc}', file=sys.stderr, flush=True)
        return 0
