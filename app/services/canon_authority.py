from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from app.models.entities import Book, CanonAuthorityProfile, StoryBible, StoryFoundation, WorldRule


PROFILE_KEYS = (
    "must_keep",
    "allowed_but_limited",
    "forbidden_misread",
    "deprecated_pollution",
    "opening_contract",
)


@dataclass(frozen=True)
class AuthorityProfile:
    book_id: int
    status: str = "derived"
    source: str = "metadata_derived"
    must_keep: list[str] = field(default_factory=list)
    allowed_but_limited: list[str] = field(default_factory=list)
    forbidden_misread: list[str] = field(default_factory=list)
    deprecated_pollution: list[str] = field(default_factory=list)
    opening_contract: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "book_id": self.book_id,
            "status": self.status,
            "source": self.source,
            "must_keep": self.must_keep,
            "allowed_but_limited": self.allowed_but_limited,
            "forbidden_misread": self.forbidden_misread,
            "deprecated_pollution": self.deprecated_pollution,
            "opening_contract": self.opening_contract,
        }

    def authority_text(self) -> str:
        rows: list[str] = []
        labels = {
            "must_keep": "必须保留",
            "allowed_but_limited": "允许但限用",
            "forbidden_misread": "禁止误写",
            "deprecated_pollution": "废弃污染",
            "opening_contract": "开篇契约",
        }
        data = self.to_dict()
        for key in PROFILE_KEYS:
            values = data.get(key) or []
            if values:
                rows.append(f"{labels[key]}:" + "；".join(values))
        return "\n".join(rows)


def get_authority_profile(session: Session, *, book_id: int) -> AuthorityProfile:
    persisted = _load_active_profile(session, book_id=book_id)
    if persisted:
        return persisted
    return derive_authority_profile(session, book_id=book_id)


def upsert_authority_profile(
    session: Session,
    *,
    book_id: int,
    profile: AuthorityProfile,
    status: str = "pending",
    source: str = "system",
) -> CanonAuthorityProfile:
    for row in session.scalars(
        select(CanonAuthorityProfile)
        .where(CanonAuthorityProfile.book_id == book_id, CanonAuthorityProfile.status == status)
        .order_by(CanonAuthorityProfile.id.desc())
    ):
        row.status = "superseded"
    entity = CanonAuthorityProfile(
        book_id=book_id,
        status=status,
        source=source,
        profile_json=json.dumps(profile.to_dict(), ensure_ascii=False, sort_keys=True),
    )
    session.add(entity)
    session.flush()
    return entity


def derive_authority_profile(session: Session, *, book_id: int) -> AuthorityProfile:
    book = session.get(Book, book_id)
    foundation = session.scalar(select(StoryFoundation).where(StoryFoundation.book_id == book_id).order_by(StoryFoundation.id.desc()))
    bible = session.scalar(select(StoryBible).where(StoryBible.book_id == book_id).order_by(StoryBible.id.desc()))
    rules = list(session.scalars(select(WorldRule).where(WorldRule.book_id == book_id, WorldRule.status == "active").order_by(WorldRule.id)))
    text = "\n".join(
        str(item or "")
        for item in [
            book.title if book else "",
            book.genre if book else "",
            foundation.premise if foundation else "",
            foundation.reader_promise if foundation else "",
            foundation.world_engine if foundation else "",
            foundation.protagonist_engine if foundation else "",
            foundation.conflict_engine if foundation else "",
            bible.positioning if bible else "",
            bible.reader_promise if bible else "",
            bible.main_plot if bible else "",
            bible.protagonist_arc if bible else "",
            bible.power_curve if bible else "",
            bible.forbidden_rules if bible else "",
            bible.style_guide if bible else "",
            *[f"{rule.category}:{rule.rule_text}" for rule in rules],
        ]
    )
    must_keep: list[str] = []
    allowed: list[str] = []
    forbidden: list[str] = []
    deprecated: list[str] = []
    opening: list[str] = []

    if book and book.title:
        must_keep.append(f"当前作品名《{book.title}》")
    protagonist = _first_match(text, r"([\u4e00-\u9fff]{2,3})\s*\(\s*22\s*岁") or _first_match(text, r"主角([\u4e00-\u9fff]{2,3})")
    if protagonist:
        must_keep.append(f"主角：{protagonist}")

    if _has_any(text, ("虚拟现实网游", "全息网游", "蜀山问道")) and _has_any(text, ("NPC", "面板", "游戏世界")):
        must_keep.extend(
            [
                "蜀山背景虚拟现实网游是入口外壳",
                "主角登录游戏后意外进入真实游戏世界",
                "游戏世界里的本地人/NPC必须按真实人物写",
                "只有主角保留游戏面板",
            ]
        )
        allowed.extend(
            [
                "正规二手/租赁/旧分期全感头盔作为登录设备",
                "熟人代练群/工作室群作为接单来源",
                "新服开荒/外门入门考核/跑日常作为正常网游业务",
                "NPC/玩家/面板等游戏词只在现实误判和少量界面中使用",
            ]
        )
        forbidden.extend(
            [
                "改成纯现实穿越",
                "改成药材棚短工入山",
                "把游戏世界写成普通VR副本或赛博空间",
                "把NPC写成机械任务工具人",
                "用系统面板直接解题",
            ]
        )
        deprecated.extend(["五十块旧头盔", "验机跑号", "胶布保险丝修脑机", "药材棚短工入山", "纯现实穿越入口"])
        opening.extend(["现实底层处境", "正常网游代练急单", "正规设备登录流程", "NPC真实化反应", "面板异常", "退出失败"])
    elif _has_any(text, ("数据异常", "数据壁垒", "世界融合", "神经接驳头盔", "物理意义上过去")):
        must_keep.extend(
            [
                "数据异常/数据壁垒是入口机制",
                "沈渡整个人物理意义上坠入写实仙侠世界" if "沈渡" in text else "主角整个人物理意义上坠入当前世界",
            ]
        )
        allowed.extend(["神经接驳头盔作为触发设备", "游戏世界/玩家/面板作为长期设定但需克制"])
        forbidden.extend(["把入口写成 VR 游玩", "写成意识上传/虚拟接入/赛博空间", "写成主角主动进游戏"])
        opening.extend(["现实底层处境", "入口事故", "物理坠入", "当前世界真实代价", "求活目标"])
    if _has_any(text, ("现实保持普通社会", "现实里他还是", "不外溢现实", "现实与")):
        forbidden.append("现实修为/伤势/身份外溢")
    if _has_any(text, ("论坛", "NPC", "玩家", "系统面板", "数据化")):
        allowed.append("数据化提示只能点到为止，不能替代人物行动")
        forbidden.append("论坛/NPC/玩家口吻刷屏污染正文")
    if _has_any(text, ("凡人阶段", "打杂求活", "武馆", "凡间")):
        must_keep.append("凡人阶段先打杂求活/学武，再接触修仙界")
        forbidden.append("修真高阶提前下场")
    if _has_any(text, ("无大能转世", "无显赫血统", "无逆天金手指")):
        must_keep.append("无血统/无大能转世/无逆天金手指")

    for title in _reference_titles(text):
        if not (book and title == book.title):
            deprecated.append(f"参考作品名《{title}》不得作为正文世界名")

    return AuthorityProfile(
        book_id=book_id,
        must_keep=_dedupe(must_keep),
        allowed_but_limited=_dedupe(allowed),
        forbidden_misread=_dedupe(forbidden),
        deprecated_pollution=_dedupe(deprecated),
        opening_contract=_dedupe(opening),
    )


def term_authority_policy(session: Session, *, book_id: int, term: str) -> str:
    profile = get_authority_profile(session, book_id=book_id)
    value = term or ""
    if _contains(profile.deprecated_pollution, value):
        return "deprecated_pollution"
    if _contains(profile.must_keep, value):
        return "must_keep"
    if _contains(profile.allowed_but_limited, value):
        return "allowed_but_limited"
    if _contains(profile.forbidden_misread, value):
        return "forbidden_misread"
    return "unspecified"


def authority_prompt_lines(session: Session, *, book_id: int, include_deprecated: bool = False) -> list[str]:
    profile = get_authority_profile(session, book_id=book_id)
    rows: list[str] = []
    if profile.must_keep:
        rows.append("设定裁决-必须保留:" + "；".join(profile.must_keep[:8]))
    if profile.allowed_but_limited:
        rows.append("设定裁决-允许但限用:" + "；".join(profile.allowed_but_limited[:8]))
    if profile.forbidden_misread:
        rows.append("设定裁决-禁止误写:" + "；".join(profile.forbidden_misread[:8]))
    if include_deprecated and profile.deprecated_pollution:
        rows.append("废弃污染:" + "；".join(profile.deprecated_pollution[:8]))
    if profile.opening_contract:
        rows.append("设定裁决-开篇契约:" + "；".join(profile.opening_contract[:8]))
    return rows


def _load_active_profile(session: Session, *, book_id: int) -> AuthorityProfile | None:
    try:
        row = session.scalar(
            select(CanonAuthorityProfile)
            .where(CanonAuthorityProfile.book_id == book_id, CanonAuthorityProfile.status == "active")
            .order_by(CanonAuthorityProfile.id.desc())
        )
    except (OperationalError, ProgrammingError):
        session.rollback()
        return None
    if not row:
        return None
    try:
        data = json.loads(row.profile_json or "{}")
    except json.JSONDecodeError:
        return None
    return AuthorityProfile(
        book_id=book_id,
        status=row.status,
        source=row.source,
        must_keep=_as_list(data.get("must_keep")),
        allowed_but_limited=_as_list(data.get("allowed_but_limited")),
        forbidden_misread=_as_list(data.get("forbidden_misread")),
        deprecated_pollution=_as_list(data.get("deprecated_pollution")),
        opening_contract=_as_list(data.get("opening_contract")),
    )


def _as_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


def _contains(values: list[str], term: str) -> bool:
    return any(term and (term in value or value in term) for value in values)


def _has_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in (text or "") for needle in needles)


def _first_match(text: str, pattern: str) -> str:
    match = re.search(pattern, text or "")
    return match.group(1) if match else ""


def _reference_titles(text: str) -> list[str]:
    rows: list[str] = []
    for match in re.finditer(r"《([^》]{2,24})》", text or ""):
        title = match.group(1).strip()
        line_start = (text or "").rfind("\n", 0, match.start()) + 1
        line_prefix = (text or "")[line_start:match.start()]
        prefix = (text or "")[max(0, match.start() - 18):match.start()]
        if any(marker in prefix or marker in line_prefix for marker in ("参考", "借鉴", "致敬", "类似", "像")):
            rows.append(title)
    return _dedupe(rows)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    rows: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        rows.append(item)
    return rows
