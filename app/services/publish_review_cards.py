"""
P55 · Phase 1+2: 番茄发布人工审核卡

生成飞书审核卡片、维护 publish_jobs 的人审状态机、承接 /approve /reject /preview 命令。

状态机：
    queued
      → awaiting_human_review   (审核卡已推给作者)
        → human_approved        (作者 /approve)
        → human_rejected        (作者 /reject <理由>)
        → human_skipped         (作者 /skip；今天不发，明天再问)

依赖：
    * chapter_versions.content, chapter_versions.title
    * quality_reports.report (platform_risk / setting_risk / dimensions)
    * platform="番茄小说" 的 publishing_targets

设计原则：cycle-loop 只读，本模块只在 cron / bot handler 里被调用（写路径干净、
不与 worker 争 SQLite 写锁）。生产环境下 worker 的 `rebuild_chapter_candidates`
可能持仓 SQLite 写锁 60-180 秒，我们所有写函数都通过 `_with_write_retry` 包一层
指数退避重试，等 worker 松手再写。
"""
from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, TypeVar

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models.entities import (
    Book,
    Chapter,
    ChapterVersion,
    PublishJob,
    QualityReport,
)


# ── 写路径重试装饰器（对付 worker 长事务）─────────────────────────────────
T = TypeVar("T")


def _with_write_retry(
    fn: Callable[[], T],
    *,
    max_attempts: int = 6,
    initial_delay: float = 2.0,
    max_delay: float = 30.0,
    op_name: str = "write",
) -> T:
    """指数退避重试 SQLite is-locked。fn 内部应重启 session/事务。"""
    delay = initial_delay
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except OperationalError as e:
            msg = str(e).lower()
            if "database is locked" not in msg and "database table is locked" not in msg:
                raise
            last_exc = e
            if attempt == max_attempts:
                break
            jitter = random.uniform(0, delay * 0.3)
            time.sleep(min(delay + jitter, max_delay))
            delay = min(delay * 2, max_delay)
    assert last_exc is not None
    raise RuntimeError(
        f"{op_name} failed after {max_attempts} attempts due to SQLite lock"
    ) from last_exc


# ── 状态字符串常量（避免 typo） ───────────────────────────────────────────
STATUS_QUEUED = "queued"
STATUS_AWAITING = "awaiting_human_review"
STATUS_APPROVED = "human_approved"
STATUS_REJECTED = "human_rejected"
STATUS_SKIPPED = "human_skipped"
STATUS_PUBLISHING = "publishing"
STATUS_PUBLISHED = "published"
STATUS_PUBLISH_FAILED = "publish_failed"

HUMAN_REVIEW_STATES = {
    STATUS_AWAITING,
    STATUS_APPROVED,
    STATUS_REJECTED,
    STATUS_SKIPPED,
}


# ── 审核卡数据类 ─────────────────────────────────────────────────────────
@dataclass
class ReviewCard:
    """一条章节审核卡的结构化数据。渲染成飞书 markdown 前先按此结构落库。"""

    publish_job_id: int
    chapter_version_id: int
    book_id: int
    book_title: str
    chapter_number: int
    chapter_title: str
    char_count: int
    summary: str  # LLM 生成 / 首段兜底
    opening_excerpt: str  # 前 300 字原文
    quality_score: int
    quality_passed: bool
    ai_smell_hits: list[str] = field(default_factory=list)
    platform_risk_flags: list[str] = field(default_factory=list)
    setting_risk_flags: list[str] = field(default_factory=list)
    generated_at: str = ""

    def to_markdown(self) -> str:
        """飞书 markdown 卡片。"""
        lines = [
            f"📖 **《{self.book_title}》· 第{self.chapter_number}章 · 待审核**",
            "",
            f"**标题**：{self.chapter_title}",
            f"**字数**：{self.char_count}",
            f"**质量分**：{self.quality_score} · "
            + ("✅ 已过闸" if self.quality_passed else "⚠️ 未过硬闸"),
            "",
            f"**摘要**（{len(self.summary)}字）：",
            f"> {self.summary}",
            "",
            "**开篇 300 字**：",
            f"> {self.opening_excerpt.replace(chr(10), chr(10) + '> ')}",
            "",
        ]
        risk_lines: list[str] = []
        if self.ai_smell_hits:
            hits = "、".join(f"`{h}`" for h in self.ai_smell_hits[:6])
            risk_lines.append(f"⚠️ **AI 味词**（{len(self.ai_smell_hits)}处）：{hits}")
        if self.platform_risk_flags:
            risk_lines.append(f"⚠️ **平台风险**：{'; '.join(self.platform_risk_flags)}")
        if self.setting_risk_flags:
            risk_lines.append(f"⚠️ **设定风险**：{'; '.join(self.setting_risk_flags)}")
        if not risk_lines:
            risk_lines.append("✅ 平台/设定/AI 味风险：均未触发")
        lines.extend(risk_lines)
        lines.append("")
        lines.append(
            f"**操作**（回复 job id `{self.publish_job_id}`）：\n"
            f"`/approve {self.publish_job_id}` 批准 · "
            f"`/reject {self.publish_job_id} <理由>` 打回 · "
            f"`/preview {self.publish_job_id}` 看全文 · "
            f"`/skip {self.publish_job_id}` 今天不发"
        )
        return "\n".join(lines)


# ── AI 味启发式规则 ──────────────────────────────────────────────────────
# 常见 LLM 中文写作滥用词。命中率高 = 需要 revise。
AI_SMELL_MARKERS = (
    "然而",
    "不禁",
    "似乎",
    "仿佛",
    "彷佛",
    "或许",
    "也许",
    "毕竟",
    "总而言之",
    "综上所述",
    "值得注意的是",
    "在这个过程中",
    "作为一名",
    "让我们",
    "首先", "其次", "再次", "最后",  # 番茄读者不喜欢 essay 感
    "总的来说",
    "在某种程度上",
    "无独有偶",
)


def _scan_ai_smell(content: str) -> list[str]:
    """扫描 AI 味词，返回命中列表（含重复次数）。"""
    hits: list[str] = []
    for marker in AI_SMELL_MARKERS:
        count = content.count(marker)
        if count >= 2:  # 单次出现放过；≥2 才计入
            hits.append(f"{marker}×{count}")
    return hits


# ── 摘要生成（兜底：首段前 100 字 + 结尾钩子 30 字） ────────────────────
_SENTENCE_SEP = re.compile(r"[。！？!?]")


def _bootstrap_summary(content: str, target_chars: int = 100) -> str:
    """无 LLM 摘要器时的启发式：抓开篇冲突 + 结尾钩子。"""
    stripped = content.strip()
    if not stripped:
        return "(空章节)"
    # 首 3 句
    sentences = [s.strip() for s in _SENTENCE_SEP.split(stripped) if s.strip()]
    if not sentences:
        return stripped[: target_chars] + "…"
    front = "".join(sentences[:3])
    back = sentences[-1] if len(sentences) > 3 else ""
    combined = f"{front[:target_chars]}……{back[:30]}"
    return combined.strip("…")


# ── quality_report 抽取 ──────────────────────────────────────────────────
def _extract_risks(report_json: dict[str, Any]) -> tuple[list[str], list[str], int, bool]:
    """从 quality_report 抽 platform/setting 风险 + 分数/闸。"""
    platform: list[str] = []
    setting: list[str] = []

    dims = report_json.get("dimensions", {}) or {}
    if int(dims.get("platform_risk", 100)) < 80:
        platform.append(f"platform_risk={dims.get('platform_risk')}")
    if int(dims.get("setting_risk", 100)) < 80:
        setting.append(f"setting_risk={dims.get('setting_risk')}")

    hard_gate = report_json.get("hard_gate", {}) or {}
    hard_gate_issues = hard_gate.get("issues") or []
    for issue in hard_gate_issues:
        text = str(issue)
        if "平台" in text or "publish" in text.lower():
            platform.append(text)
        elif "设定" in text or "canon" in text.lower():
            setting.append(text)

    score = int(report_json.get("score", 0))
    passed = bool(hard_gate.get("passed"))
    return platform, setting, score, passed


# ── 主函数：build_review_card ────────────────────────────────────────────
def build_review_card(session: Session, *, publish_job_id: int) -> ReviewCard:
    """从一个 publish_job 构造一张审核卡（不写库）。"""
    job = session.get(PublishJob, publish_job_id)
    if not job:
        raise ValueError(f"publish_job not found: {publish_job_id}")
    version = session.get(ChapterVersion, job.chapter_version_id)
    if not version:
        raise ValueError(f"chapter_version not found: {job.chapter_version_id}")
    chapter = session.get(Chapter, version.chapter_id)
    if not chapter:
        raise ValueError(f"chapter not found: {version.chapter_id}")
    book = session.get(Book, chapter.book_id)
    book_title = book.title if book else f"book#{chapter.book_id}"

    content = version.content or ""
    char_count = len(content)

    # 最近一份 quality_report（不强依赖）
    quality = session.scalar(
        select(QualityReport)
        .where(QualityReport.chapter_version_id == version.id)
        .order_by(QualityReport.id.desc())
    )
    if quality:
        try:
            report_json = json.loads(quality.report) if quality.report else {}
        except json.JSONDecodeError:
            report_json = {}
        platform_flags, setting_flags, score, passed = _extract_risks(report_json)
    else:
        platform_flags, setting_flags = [], []
        score, passed = 0, False

    summary = _bootstrap_summary(content)
    opening_excerpt = content.strip()[:300]
    ai_hits = _scan_ai_smell(content)

    return ReviewCard(
        publish_job_id=job.id,
        chapter_version_id=version.id,
        book_id=chapter.book_id,
        book_title=book_title,
        chapter_number=chapter.chapter_number,
        chapter_title=version.title or chapter.title or f"第{chapter.chapter_number}章",
        char_count=char_count,
        summary=summary,
        opening_excerpt=opening_excerpt,
        quality_score=score,
        quality_passed=passed,
        ai_smell_hits=ai_hits,
        platform_risk_flags=platform_flags,
        setting_risk_flags=setting_flags,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


# ── 待审列表拉取 ─────────────────────────────────────────────────────────
def list_queued_for_review(
    session: Session,
    *,
    book_id: int | None = None,
    platform: str = "番茄小说",
    limit: int = 5,
) -> list[PublishJob]:
    """按 chapter_number asc 取 queued 中最靠前的 N 章，用于每日推送。

    守卫（发布顺序）：
      1. 每章只保留最新一个活跃 job（created_at desc），老 job 视为 superseded
      2. 从平台最后已发布章节 + 1 起严格连续，跳章即停止（避免第 5 章无第 4 章）
      3. 已 awaiting_human_review 的章占位——排队时该 chapter_number 不再重复推
    """
    # 1) 每章取最新 job：拿本 book 该 platform 所有 queued/awaiting/approved 章节号集合
    from sqlalchemy import func

    # 已进入审核/审批/发布流的章节号（占位，不再推同章）
    inflight_chapters_stmt = (
        select(Chapter.chapter_number)
        .join(ChapterVersion, ChapterVersion.chapter_id == Chapter.id)
        .join(PublishJob, PublishJob.chapter_version_id == ChapterVersion.id)
        .where(PublishJob.platform == platform)
        .where(PublishJob.status.in_([
            STATUS_AWAITING, "human_approved", "human_rejected",
            "publishing", "published", "failed",
        ]))
    )
    if book_id is not None:
        inflight_chapters_stmt = inflight_chapters_stmt.where(Chapter.book_id == book_id)
    inflight_chapter_numbers = set(session.scalars(inflight_chapters_stmt).all())

    # 2) 找平台最后已发布章节号（可从 published 状态推断）
    published_stmt = (
        select(func.max(Chapter.chapter_number))
        .join(ChapterVersion, ChapterVersion.chapter_id == Chapter.id)
        .join(PublishJob, PublishJob.chapter_version_id == ChapterVersion.id)
        .where(PublishJob.platform == platform)
        .where(PublishJob.status == "published")
    )
    if book_id is not None:
        published_stmt = published_stmt.where(Chapter.book_id == book_id)
    last_published = session.scalar(published_stmt) or 0
    # next_expected 从「已发布最大章」和「已进入审核流最大章」之间取大 +1
    # 因为审核中/已批准的章节实际上已占位，下一批应该从它们之后接续
    last_inflight = max(inflight_chapter_numbers) if inflight_chapter_numbers else 0
    next_expected = max(last_published, last_inflight) + 1

    # 3) 取所有 queued job，按章节号排序
    stmt = (
        select(PublishJob, Chapter.chapter_number)
        .join(ChapterVersion, ChapterVersion.id == PublishJob.chapter_version_id)
        .join(Chapter, Chapter.id == ChapterVersion.chapter_id)
        .where(PublishJob.status == STATUS_QUEUED)
        .where(PublishJob.platform == platform)
    )
    if book_id is not None:
        stmt = stmt.where(Chapter.book_id == book_id)
    stmt = stmt.order_by(
        Chapter.book_id.asc(),
        Chapter.chapter_number.asc(),
        PublishJob.created_at.desc(),  # 同章多个 queued 取最新
    )

    # 4) 去重（每章保留最新）+ 顺序守卫（跳章停止）
    seen_chapters: set[int] = set()
    selected: list[PublishJob] = []
    for job, ch_num in session.execute(stmt).all():
        if ch_num in seen_chapters:
            continue  # 老 job 跳过
        seen_chapters.add(ch_num)
        if ch_num in inflight_chapter_numbers:
            continue  # 该章已在审核中/已发布，不再推
        # 顺序守卫：必须严格连续
        if ch_num < next_expected:
            continue  # 落后的章跳过
        if ch_num > next_expected:
            # 跳章了，停止推送（不能越过前面章节）
            break
        selected.append(job)
        next_expected += 1
        if len(selected) >= limit:
            break

    return selected


def list_awaiting_review(
    session: Session, *, platform: str = "番茄小说"
) -> list[PublishJob]:
    """已推送、等作者回复的。"""
    stmt = (
        select(PublishJob)
        .where(PublishJob.status == STATUS_AWAITING)
        .where(PublishJob.platform == platform)
    )
    return list(session.scalars(stmt))


# ── 状态机操作 ───────────────────────────────────────────────────────────
def _load_payload(job: PublishJob) -> dict:
    if not job.automation_payload:
        return {}
    try:
        return json.loads(job.automation_payload)
    except json.JSONDecodeError:
        return {}


def _save_payload(job: PublishJob, payload: dict) -> None:
    job.automation_payload = json.dumps(payload, ensure_ascii=False)


def _append_report_line(job: PublishJob, line: str) -> None:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    prev = (job.result_report or "").rstrip()
    job.result_report = f"{prev}\n[{stamp}] {line}".strip()


import os

def _should_auto_approve(session: Session, job: PublishJob) -> tuple[bool, str]:
    """P0-3 · 发布自动放行判定。

    条件：hard_gate 通过 AND score ≥ AUTO_APPROVE_MIN_SCORE
          AND 无 exhaustion_escalation soft_pass 标记
          AND 无 chapter_type_gate 硬失败

    返回 (是否自动放行, 原因)。灰度开关：PUBLISH_AUTO_APPROVE=1 才启用。
    """
    if os.environ.get("PUBLISH_AUTO_APPROVE", "0") != "1":
        return False, "auto_approve_disabled"
    min_score = int(os.environ.get("PUBLISH_AUTO_APPROVE_MIN_SCORE", "72"))
    # 拉最新 QualityReport
    qr = session.execute(
        select(QualityReport)
        .where(QualityReport.chapter_version_id == job.chapter_version_id)
        .order_by(QualityReport.id.desc())
    ).scalars().first()
    if not qr:
        return False, "no_quality_report"
    try:
        report = json.loads(qr.report or "{}")
    except json.JSONDecodeError:
        return False, "report_parse_error"
    # 分数门槛
    if qr.score < min_score:
        return False, f"score_{qr.score}_below_{min_score}"
    # hard_gate 硬门槛
    hard_gate = report.get("hard_gate") or {}
    if not hard_gate.get("passed", False):
        return False, f"hard_gate_failed:{hard_gate.get('failures', [])[:3]}"
    # exhaustion_escalation 打了 soft-pass 兜底不放行
    if report.get("exhaustion_escalation"):
        return False, "exhaustion_soft_pass"
    # chapter_type_gate
    # 2026-07-24 根本修复：type_gate 判定尊重 strict 标记，与 production_optimization.py
    # 架构语义对齐。常规连载章(serial_progress·strict=False)裁决权归 quality 层——
    # 只要 hard_gate 过 + score≥65 soft_pass 即放行(type_gate.passed=False 不否决)。
    # 只有生死线章型(opening/early_serial/turning_point·strict=True)保留 type_gate 高标准
    # 否决权。soft_pass_active=True 的章也放行(编辑+base 双认可+gap≤15)。
    # 这打通了"常规 B 版 score 65-71 被 type_gate(72) 越权二次否决"的口语化陷阱，
    # 同时守住转折章/开局章的商业质量红线。
    type_gate = report.get("chapter_type_gate") or {}
    if os.environ.get("PUBLISH_AUTO_APPROVE_SKIP_TYPE_GATE", "0") != "1":
        if type_gate and not type_gate.get("passed", True):
            is_strict = bool(type_gate.get("strict", False))
            soft_pass_active = bool(type_gate.get("soft_pass", False))
            # 生死线章型未过 type_gate 且未激活 soft_pass → 否决(保留高标准)
            if is_strict and not soft_pass_active:
                return False, f"type_gate_failed:strict_chapter={type_gate.get('chapter_type')}"
    return True, f"auto_approved:score={qr.score}"


def mark_awaiting_review(
    session: Session, *, job_id: int, feishu_message_id: str = "", card_md: str = ""
) -> PublishJob:
    job = session.get(PublishJob, job_id)
    if not job:
        raise ValueError(f"publish_job not found: {job_id}")
    if job.status not in {STATUS_QUEUED, STATUS_REJECTED, STATUS_SKIPPED}:
        raise ValueError(
            f"cannot push job {job_id} to review from status={job.status!r}"
        )
    # P0-3 · 尝试自动放行（灰度开关：PUBLISH_AUTO_APPROVE=1）
    auto_ok, reason = _should_auto_approve(session, job)
    if auto_ok:
        job.status = STATUS_APPROVED
        payload = _load_payload(job)
        auto_log = payload.setdefault("auto_approve", {})
        auto_log["approved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        auto_log["reason"] = reason
        _save_payload(job, payload)
        _append_report_line(job, f"auto-approved: {reason}")
        session.flush()
        return job
    job.status = STATUS_AWAITING
    payload = _load_payload(job)
    review_log = payload.setdefault("human_review", {})
    review_log["pushed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    review_log["auto_approve_skip_reason"] = reason
    if feishu_message_id:
        review_log["feishu_message_id"] = feishu_message_id
    if card_md:
        review_log["card_md_len"] = len(card_md)
    _save_payload(job, payload)
    _append_report_line(job, f"pushed to human review · auto_approve_skip: {reason}")
    session.flush()
    return job


def approve(session: Session, *, job_id: int, reviewer: str = "human") -> PublishJob:
    job = session.get(PublishJob, job_id)
    if not job:
        raise ValueError(f"publish_job not found: {job_id}")
    if job.status != STATUS_AWAITING:
        raise ValueError(
            f"cannot approve job {job_id} from status={job.status!r} (expected awaiting_human_review)"
        )
    job.status = STATUS_APPROVED
    payload = _load_payload(job)
    payload.setdefault("human_review", {}).update(
        {
            "approved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "reviewer": reviewer,
        }
    )
    _save_payload(job, payload)
    _append_report_line(job, f"approved by {reviewer}")
    session.flush()
    return job


def reject(
    session: Session, *, job_id: int, reason: str, reviewer: str = "human"
) -> PublishJob:
    job = session.get(PublishJob, job_id)
    if not job:
        raise ValueError(f"publish_job not found: {job_id}")
    if job.status != STATUS_AWAITING:
        raise ValueError(
            f"cannot reject job {job_id} from status={job.status!r} (expected awaiting_human_review)"
        )
    job.status = STATUS_REJECTED
    payload = _load_payload(job)
    payload.setdefault("human_review", {}).update(
        {
            "rejected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "reviewer": reviewer,
            "reject_reason": reason,
        }
    )
    _save_payload(job, payload)
    _append_report_line(job, f"rejected by {reviewer}: {reason}")
    session.flush()
    return job


def skip(session: Session, *, job_id: int, reviewer: str = "human") -> PublishJob:
    """今天不发；status 回 queued，明天 cron 再问一次。"""
    job = session.get(PublishJob, job_id)
    if not job:
        raise ValueError(f"publish_job not found: {job_id}")
    if job.status != STATUS_AWAITING:
        raise ValueError(
            f"cannot skip job {job_id} from status={job.status!r} (expected awaiting_human_review)"
        )
    job.status = STATUS_QUEUED
    payload = _load_payload(job)
    review_log = payload.setdefault("human_review", {})
    skips = review_log.setdefault("skips", [])
    skips.append(
        {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "reviewer": reviewer,
        }
    )
    _save_payload(job, payload)
    _append_report_line(job, f"skipped by {reviewer} (will re-ask tomorrow)")
    session.flush()
    return job


# ── 命令解析（供飞书 handler 或 CLI 复用） ────────────────────────────────
# 中英同义词表：飞书回复 "批准 4" 与 "/approve 4" 等价
_ALIAS = {
    "approve": "approve", "批准": "approve", "通过": "approve", "同意": "approve",
    "reject": "reject", "打回": "reject", "驳回": "reject", "拒绝": "reject",
    "skip": "skip", "跳过": "skip", "暂缓": "skip",
    "preview": "preview", "预览": "preview", "看全文": "preview", "查看": "preview",
}
# 允许 /approve 4 · approve 4 · 批准 4 · 批准 #4 · 批准 job 4
_COMMAND_RE = re.compile(
    r"^\s*[/]?\s*(" + "|".join(re.escape(k) for k in _ALIAS.keys()) + r")"
    r"\s*(?:job\s*)?#?\s*(\d+)\s*(.*)$",
    re.IGNORECASE,
)


@dataclass
class ParsedCommand:
    action: str  # approve / reject / preview / skip
    job_id: int
    remainder: str


def parse_command(text: str) -> ParsedCommand | None:
    if not text:
        return None
    m = _COMMAND_RE.match(text.strip())
    if not m:
        return None
    verb = m.group(1).lower()
    canonical = _ALIAS.get(verb) or _ALIAS.get(m.group(1))  # 中文 key 不 lower
    if canonical is None:
        return None
    return ParsedCommand(
        action=canonical,
        job_id=int(m.group(2)),
        remainder=m.group(3).strip(),
    )


def full_content_for_preview(session: Session, *, job_id: int) -> str:
    job = session.get(PublishJob, job_id)
    if not job:
        raise ValueError(f"publish_job not found: {job_id}")
    version = session.get(ChapterVersion, job.chapter_version_id)
    if not version:
        raise ValueError(f"chapter_version not found: {job.chapter_version_id}")
    chapter = session.get(Chapter, version.chapter_id)
    return f"**{version.title or (chapter.title if chapter else '')}**\n\n{version.content or ''}"
