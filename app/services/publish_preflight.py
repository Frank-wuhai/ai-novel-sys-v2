from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.entities import ChapterVersion
from app.services.bias import evaluate_generation_bias
from app.services.quality import chinese_chars


def build_publish_preflight(session: Session, *, version_id: int) -> dict:
    version = session.get(ChapterVersion, version_id)
    if not version:
        raise ValueError(f"chapter version not found: {version_id}")
    blockers: list[str] = []
    warnings: list[str] = []
    content = version.content or ""
    if version.status != "approved":
        blockers.append(f"版本状态不是 approved: {version.status}")
    chars = chinese_chars(content)
    if chars < 2500:
        blockers.append(f"正文过短: {chars}")
    if any(marker in content for marker in ("修订模式", "修订合同", "系统提示", "作为AI")):
        blockers.append("正文含后台/模型元信息")
    bias = evaluate_generation_bias(content=content)
    if bias.model_bias_hits:
        blockers.append("正文仍含模型默认套路词: " + ",".join(bias.model_bias_hits))
    if not version.title:
        warnings.append("章节标题为空")
    return {
        "passed": not blockers,
        "version_id": version.id,
        "status": version.status,
        "title": version.title,
        "chinese_chars": chars,
        "blockers": blockers,
        "warnings": warnings,
        "export_preview": _format_export(version)[:1200],
    }


def _format_export(version: ChapterVersion) -> str:
    title = version.title or f"第{version.version_number}版"
    return f"{title}\n\n{version.content or ''}".strip()
