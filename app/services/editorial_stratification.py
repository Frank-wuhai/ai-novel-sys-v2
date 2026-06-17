from __future__ import annotations

import json
from dataclasses import dataclass
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Chapter, ChapterBrief, ChapterVersion, QualityReport
from app.services.brief_sanitizer import sanitize_existing_chapter_brief
from app.services.feedback import submit_revision_suggestion


TIER_PUBLISH = "S_publish_ready"
TIER_NEAR_FINAL = "A_near_final"
TIER_SOLID_DRAFT = "B_solid_draft"
TIER_PROBLEM_DRAFT = "C_problem_draft"
TIER_REBUILD = "D_rebuild"
TIER_CONTAMINATED = "E_contaminated"


@dataclass(frozen=True)
class EditorialStratification:
    tier: str
    label: str
    recommended_mode: str
    should_auto_revise: bool
    summary: str
    preserve: list[str]
    elevate: list[str]
    blockers: list[str]
    forbidden: list[str]

    def to_dict(self) -> dict:
        return {
            "tier": self.tier,
            "label": self.label,
            "recommended_mode": self.recommended_mode,
            "should_auto_revise": self.should_auto_revise,
            "summary": self.summary,
            "preserve": self.preserve,
            "elevate": self.elevate,
            "blockers": self.blockers,
            "forbidden": self.forbidden,
        }


def stratify_quality_report(report_data: dict) -> EditorialStratification:
    dimensions = report_data.get("dimensions") if isinstance(report_data.get("dimensions"), dict) else {}
    issues = [str(item) for item in report_data.get("issues") or []]
    warnings = [str(item) for item in report_data.get("warnings") or []]
    review = report_data.get("llm_review") if isinstance(report_data.get("llm_review"), dict) else {}
    score = int(report_data.get("score") or 0)
    passed = bool(report_data.get("passed"))
    editor_score = int(review.get("score") or 0)
    review_issues = [str(item) for item in review.get("issues") or []]
    review_strengths = [str(item) for item in review.get("strengths") or []]
    review_suggestions = [str(item) for item in review.get("revision_suggestions") or []]

    contamination = _contamination_blockers(issues, warnings)
    if contamination:
        return EditorialStratification(
            tier=TIER_CONTAMINATED,
            label="污染稿",
            recommended_mode="contamination_repair",
            should_auto_revise=False,
            summary="正文或上下文存在污染信号，必须先隔离清理。",
            preserve=[],
            elevate=[],
            blockers=contamination,
            forbidden=_default_forbidden(),
        )

    hard_lows = _hard_low_dimensions(dimensions)
    if not passed and (score < 60 or len(hard_lows) >= 4):
        return EditorialStratification(
            tier=TIER_REBUILD,
            label="废稿/重构稿",
            recommended_mode="structural_rebuild",
            should_auto_revise=False,
            summary="方向或结构问题过重，不适合继续局部打补丁。",
            preserve=_preserve_from_review(review_strengths),
            elevate=_elevation_targets(dimensions, review_issues, review_suggestions),
            blockers=hard_lows or issues[:6],
            forbidden=_default_forbidden(),
        )

    if not passed:
        return EditorialStratification(
            tier=TIER_PROBLEM_DRAFT,
            label="问题稿",
            recommended_mode="targeted_fix",
            should_auto_revise=False,
            summary="当前稿有明确质量问题，应先定点修复阻断项。",
            preserve=_preserve_from_review(review_strengths),
            elevate=_elevation_targets(dimensions, review_issues, review_suggestions),
            blockers=hard_lows or issues[:6] or review_issues[:4],
            forbidden=_default_forbidden(),
        )

    weak_lows = _weak_pass_dimensions(dimensions)
    if score >= 88 and editor_score >= 88 and not weak_lows:
        return EditorialStratification(
            tier=TIER_PUBLISH,
            label="发布级",
            recommended_mode="approve",
            should_auto_revise=False,
            summary="质量与主编判断都已稳定，可以交给人工最终确认。",
            preserve=_preserve_from_review(review_strengths),
            elevate=[],
            blockers=[],
            forbidden=_default_forbidden(),
        )
    if score >= 80 and len(weak_lows) <= 1:
        return EditorialStratification(
            tier=TIER_NEAR_FINAL,
            label="准定稿",
            recommended_mode="polish",
            should_auto_revise=False,
            summary="整体已经接近定稿，只建议人工决定是否做轻润色。",
            preserve=_preserve_from_review(review_strengths),
            elevate=_elevation_targets(dimensions, review_issues, review_suggestions)[:3],
            blockers=weak_lows[:3],
            forbidden=_default_forbidden(),
        )

    return EditorialStratification(
        tier=TIER_SOLID_DRAFT,
        label="合格底稿",
        recommended_mode="targeted_elevation",
        should_auto_revise=True,
        summary="方向正确且可读，但还没有达到准定稿；下一步应升华读感，而不是推翻重写。",
        preserve=_preserve_from_review(review_strengths),
        elevate=_elevation_targets(dimensions, review_issues, review_suggestions),
        blockers=weak_lows[:6] or review_issues[:4],
        forbidden=_default_forbidden(),
    )


def maybe_apply_editorial_stratification(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    quality: QualityReport,
) -> EditorialStratification:
    report_data = _loads_json(quality.report)
    stratification = stratify_quality_report(report_data)
    report_data["editorial_stratification"] = stratification.to_dict()
    quality.report = json.dumps(report_data, ensure_ascii=False)
    if not stratification.should_auto_revise:
        session.flush()
        return stratification
    brief = _latest_brief_for_chapter(session, book_id=book_id, chapter_number=chapter_number)
    active_text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""]) if brief else ""
    marker = f"editorial_elevation_quality#{quality.id}"
    if marker in active_text:
        session.flush()
        return stratification
    source_version = session.get(ChapterVersion, quality.chapter_version_id)
    suggestion = build_elevation_contract(
        stratification=stratification,
        quality_id=quality.id,
        source_version=source_version,
    )
    _feedback, _adjustment, new_brief, version = submit_revision_suggestion(
        session,
        book_id=book_id,
        chapter_number=chapter_number,
        platform="editorial_stratification",
        suggestion_text=suggestion,
        revision_mode="targeted",
    )
    source_label = f"v{source_version.id}" if source_version else f"quality#{quality.id}"
    new_brief.goal = f"升华修订第{chapter_number}章：以{source_label}为唯一底稿，保留有效结构，把“能读”提升到“想追”。"
    new_brief.required_beats = "\n".join(
        [
            f"当前版本层级：{stratification.label}。",
            "修订模式：targeted_elevation；以源版本为底稿逐场增强，不重写整章，不重新选小样，不新开一版故事。",
            "源版本锁定：" + source_label + "。旧稿的主事件、场景顺序、人物行动链、章末事实必须保留。",
            "改动范围：只允许扩写画面、对白、人物反应、因果承接、奖励/代价落地和章末压力；不得替换核心处境。",
            "必须保留：" + "；".join(stratification.preserve[:6]),
            "升华目标：" + "；".join(stratification.elevate[:6]),
            "当前阻断：" + "；".join(stratification.blockers[:6]),
        ]
    )
    new_brief.constraints = "\n".join(
        [
            new_brief.constraints or "",
            f"{marker}: 自动编辑分层判定为{stratification.label}，本轮只做升华修订。",
            "升华失败熔断：若修订后跌到问题稿/废稿，自动回滚到源版本，不再沿坏稿继续修。",
            "禁止推翻已成立主线；禁止把合格底稿改成全新开场；禁止新增追杀、机构关注、门派通缉、系统面板直接解题。",
            "禁止只替换形容词或把句子写得更冷更短；必须把抽象判断改成可见动作、空间、对白和后果。",
            "验收：self_check 必须写明保留了源版本哪些主事件/场景顺序/章末事实，以及具体增强了哪些场景体验。",
            "验收：修订后应从合格底稿升到准定稿，读者能记住本章第一江湖门槛、金手指回报和章末压力。",
        ]
    )
    if version and version.status != "needs_revision":
        version.status = "needs_revision"
    sanitize_existing_chapter_brief(session, book_id=book_id, brief=new_brief)
    session.flush()
    return stratification


def maybe_rollback_failed_elevation(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    failed_version: ChapterVersion,
    quality: QualityReport,
) -> ChapterVersion | None:
    report_data = _loads_json(quality.report)
    stratification = report_data.get("editorial_stratification") if isinstance(report_data.get("editorial_stratification"), dict) else {}
    if stratification.get("tier") not in {TIER_PROBLEM_DRAFT, TIER_REBUILD}:
        return None
    active_brief = _latest_brief_for_chapter(session, book_id=book_id, chapter_number=chapter_number)
    active_text = "\n".join([active_brief.goal or "", active_brief.required_beats or "", active_brief.constraints or ""]) if active_brief else ""
    if "editorial_elevation_quality#" not in active_text:
        return None
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if not chapter:
        return None
    best = _elevation_source_version(session, active_text=active_text) or _best_previous_passed_version(
        session,
        chapter_id=chapter.id,
        before_version_id=failed_version.id,
    )
    if not best:
        return None
    best_quality = session.scalar(select(QualityReport).where(QualityReport.chapter_version_id == best.id).order_by(QualityReport.id.desc()))
    if not best_quality:
        return None
    rollback = ChapterVersion(
        chapter_id=chapter.id,
        version_number=_next_version_number(session, chapter.id),
        title=best.title,
        content=best.content,
        status="reviewed_pass",
        source=f"editorial_rollback:v{best.id}",
    )
    session.add(rollback)
    session.flush()
    restored_report = _loads_json(best_quality.report)
    restored_report["editorial_rollback"] = {
        "from_failed_version_id": failed_version.id,
        "from_failed_quality_id": quality.id,
        "restored_source_version_id": best.id,
        "reason": "升华修订结果低于原合格底稿，自动回滚到上一版最佳可读稿。",
    }
    session.add(
        QualityReport(
            chapter_version_id=rollback.id,
            score=best_quality.score,
            passed=best_quality.passed,
            report=json.dumps(restored_report, ensure_ascii=False),
        )
    )
    if active_brief:
        active_brief.status = "superseded"
    session.flush()
    return rollback


def build_elevation_contract(
    *,
    stratification: EditorialStratification,
    quality_id: int,
    source_version: ChapterVersion | None = None,
) -> str:
    source_block = _source_version_block(source_version)
    return "\n".join(
        [
            f"editorial_elevation_quality#{quality_id}",
            *source_block,
            f"当前版本层级：{stratification.label}",
            f"推荐修订模式：{stratification.recommended_mode}",
            f"编辑判断：{stratification.summary}",
            "源版本处理规则：",
            "- 本轮不是重写，不是换开场，不是重选样稿；必须以源版本正文为底本逐场增强。",
            "- 保留源版本的主事件、场景顺序、人物行动链、关键因果和章末事实。",
            "- 只改读者体验不足的单元：补画面、补动作、补对白、补误判过程、补收益代价落地。",
            "- 不允许新增追杀、机构关注、门派通缉、系统面板直接解题等俗套压力源来制造强度。",
            "必须保留：",
            *[f"- {item}" for item in stratification.preserve[:8]],
            "升华目标：",
            *[f"- {item}" for item in stratification.elevate[:8]],
            "当前阻断：",
            *[f"- {item}" for item in stratification.blockers[:8]],
            "修订边界：",
            "- 不要整章重写；不要重新生成小样；不要推翻已成立结构；不要写成全新版本。",
            "- 不要为了显得高级而压短句子、减少信息、制造冷硬腔。",
            "- 把低分维度转化成正文可见改变：场景气味/光线/站位、人物犹豫和误判、奖励递进、章末压力。",
            "- 禁止只补几个形容词；必须让关键场景的读者体验升级。",
            "执行步骤：",
            "- 先识别源版本中的 4-7 个主要场景单元。",
            "- 每个场景只问三件事：读者看不看得见、人物反应是否递进、行动后果是否落地。",
            "- 对不合格场景做局部扩写或替换表达；合格场景只做少量润色。",
            "- 结尾必须继承源版本章末事实，只增强具体压力或期待，不改成另一种危机。",
            "验收清单：",
            "- self_check 必须说明保留了哪些源版本结构。",
            "- self_check 必须说明增强了哪些具体场景体验。",
            "- self_check 必须说明没有新增俗套追杀/机构关注/系统面板解法。",
            "禁止：",
            *[f"- {item}" for item in stratification.forbidden],
        ]
    )


def _source_version_block(source_version: ChapterVersion | None) -> list[str]:
    if not source_version:
        return ["源版本：无法定位；按当前 latest version 为唯一底稿。"]
    content = source_version.content or ""
    opening = _compact_line(content[:240])
    ending = _compact_line(content[-240:])
    return [
        f"源版本：ChapterVersion#{source_version.id} / version_number={source_version.version_number} / title={source_version.title or ''}",
        f"源版本正文长度：{len(content)} 字符。",
        f"源版本开头锚点：{opening}",
        f"源版本结尾锚点：{ending}",
    ]


def _hard_low_dimensions(dimensions: dict) -> list[str]:
    thresholds = {
        "brief_coverage": 45,
        "canon_consistency": 70,
        "author_intent": 55,
        "narrative_logic": 55,
        "anti_ai_flavor": 55,
        "writer_craft": 50,
    }
    return [f"{name}={int(dimensions.get(name) or 0)}<{threshold}" for name, threshold in thresholds.items() if int(dimensions.get(name) or 0) < threshold]


def _weak_pass_dimensions(dimensions: dict) -> list[str]:
    thresholds = {
        "brief_coverage": 60,
        "reader_momentum": 62,
        "hook_strength": 68,
        "scene_atmosphere": 55,
        "observation_logic": 60,
        "dialogue_fullness": 60,
        "chapter_unit_flow": 65,
        "payoff_grounding": 65,
        "chapter_necessity": 65,
        "scene_expansion": 65,
    }
    return [f"{name}={int(dimensions.get(name) or 0)}<{threshold}" for name, threshold in thresholds.items() if int(dimensions.get(name) or 0) < threshold]


def _elevation_targets(dimensions: dict, issues: list[str], suggestions: list[str]) -> list[str]:
    targets: list[str] = []
    if int(dimensions.get("scene_atmosphere") or 100) < 55:
        targets.append("把场景氛围从概括词升级为可记忆的空间、气味、光线、声音和人物压力。")
    if int(dimensions.get("observation_logic") or 100) < 60:
        targets.append("补强观察证据链：先给可见证据，再给误判，再让人物反应修正判断。")
    if int(dimensions.get("dialogue_fullness") or 100) < 60:
        targets.append("让关键对白带身份、情绪、试探、威胁或利益算盘，不只承担功能。")
    if int(dimensions.get("hook_strength") or 100) < 68:
        targets.append("章末压力要更具体，让读者感到下一章必须解决。")
    if int(dimensions.get("payoff_grounding") or 100) < 65:
        targets.append("把奖励和爽点落到身体变化、局面变化或新选择上，形成递进。")
    for item in [*issues, *suggestions]:
        if item and item not in targets:
            targets.append(item)
    return targets or ["把合格内容升华为更强的读者记忆点和追读压力。"]


def _preserve_from_review(strengths: list[str]) -> list[str]:
    if strengths:
        return strengths[:8]
    return ["当前已成立的主线结构", "有效人物行动链", "可用场景顺序", "章末方向"]


def _contamination_blockers(issues: list[str], warnings: list[str]) -> list[str]:
    markers = ("forbidden_marker", "context_contamination", "旧设定", "反方向词", "系统字段", "brief")
    return [item for item in [*issues, *warnings] if any(marker in item for marker in markers)]


def _default_forbidden() -> list[str]:
    return ["追杀模板", "现实机构关注", "系统面板直接解题", "冷硬装酷式精炼", "质检术语进入正文"]


def _compact_line(text: str, *, limit: int = 180) -> str:
    compact = " ".join(str(text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "..."


def _latest_brief_for_chapter(session: Session, *, book_id: int, chapter_number: int) -> ChapterBrief | None:
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if not chapter:
        return None
    return session.scalar(select(ChapterBrief).where(ChapterBrief.chapter_id == chapter.id).order_by(ChapterBrief.id.desc()))


def _best_previous_passed_version(session: Session, *, chapter_id: int, before_version_id: int) -> ChapterVersion | None:
    rows = list(
        session.scalars(
            select(ChapterVersion)
            .where(ChapterVersion.chapter_id == chapter_id, ChapterVersion.id < before_version_id)
            .order_by(ChapterVersion.id.desc())
            .limit(24)
        )
    )
    best_version: ChapterVersion | None = None
    best_score = -1
    for version in rows:
        quality = session.scalar(select(QualityReport).where(QualityReport.chapter_version_id == version.id).order_by(QualityReport.id.desc()))
        if not quality or not quality.passed:
            continue
        score = int(quality.score or 0)
        if score > best_score:
            best_version = version
            best_score = score
    return best_version


def _elevation_source_version(session: Session, *, active_text: str) -> ChapterVersion | None:
    match = re.search(r"editorial_elevation_quality#(\d+)", active_text or "")
    if not match:
        return None
    quality = session.get(QualityReport, int(match.group(1)))
    if not quality or not quality.passed:
        return None
    return session.get(ChapterVersion, quality.chapter_version_id)


def _next_version_number(session: Session, chapter_id: int) -> int:
    latest = session.scalar(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter_id).order_by(ChapterVersion.version_number.desc()))
    return (latest.version_number if latest else 0) + 1


def _loads_json(value: str | None) -> dict:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
