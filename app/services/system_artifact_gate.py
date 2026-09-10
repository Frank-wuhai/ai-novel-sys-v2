from __future__ import annotations

import re
from dataclasses import dataclass, field


SYSTEM_ARTIFACT_PATTERNS: tuple[tuple[str, str], ...] = (
    ("hard_truncation_marker", r"\[?本章末尾按字数硬门自动截断\]?|\[?字数硬门自动截断\]?"),
    ("runtime_draft_marker", r"Runtime Draft|generated_by_agent|model_used"),
    ("prompt_artifact", r"系统提示(?:词|：|:)|作为AI|作为 AI|下面是(?:正文|草稿|修订后)"),
    ("json_artifact", r"^\s*```|^\s*\{[\s\S]{0,400}\"content\"\s*:"),
    ("isolated_latin_fragment", r"(?m)^\s*[A-Za-z]{1,8}\s*$"),
)
REVIEW_ARTIFACT_TERMS = ("质检报告", "修订合同", "导演单", "生成策略", "返修说明")
REVIEW_ARTIFACT_LINE_PREFIXES = (
    "质检报告",
    "修订合同",
    "导演单",
    "生成策略",
    "返修说明",
    "执行修订合同",
    "依据质检报告",
)
REVIEW_ARTIFACT_META_NEIGHBORS = (
    "score",
    "weak_",
    "hard_gate",
    "passed",
    "验收",
    "修订模式",
    "必须满足",
    "问题清单",
    "质量报告",
)


@dataclass
class SystemArtifactReport:
    passed: bool
    issues: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "issues": self.issues,
            "examples": self.examples[:8],
        }


def evaluate_system_artifacts(text: str) -> SystemArtifactReport:
    issues: list[str] = []
    examples: list[str] = []
    content = text or ""
    for issue, pattern in SYSTEM_ARTIFACT_PATTERNS:
        match = re.search(pattern, content, flags=re.I)
        if match:
            issues.append(issue)
            examples.append(_excerpt(content, match.start(), match.end()))
    review_example = _review_artifact_example(content)
    if review_example:
        issues.append("review_artifact")
        examples.append(review_example)
    return SystemArtifactReport(passed=not issues, issues=issues, examples=examples)


def _review_artifact_example(text: str) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip(" \t-#：:")
        if any(stripped.startswith(prefix) for prefix in REVIEW_ARTIFACT_LINE_PREFIXES):
            return stripped[:120]
        if any(term in stripped for term in REVIEW_ARTIFACT_TERMS) and any(
            marker in stripped for marker in REVIEW_ARTIFACT_META_NEIGHBORS
        ):
            return stripped[:120]
    return ""


def _excerpt(text: str, start: int, end: int, radius: int = 36) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    return re.sub(r"\s+", " ", text[left:right]).strip()
