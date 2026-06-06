from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class WriterLoopPlan:
    status: str
    focus: str
    rewrite_directives: list[str]
    pov_card: dict[str, str]
    local_revision: dict[str, object]
    blocked_patterns: list[str]
    acceptance_checks: list[str]

    @property
    def prompt_block(self) -> str:
        lines = [
            "作家化生成闭环 v1（本轮必须执行，不要原样输出标题）：",
            f"- 当前重点：{self.focus}",
            "重写导演单：",
            *[f"- {item}" for item in self.rewrite_directives],
            "角色视角卡：",
            *[f"- {key}：{value}" for key, value in self.pov_card.items()],
            "旧模板警戒：",
            *[f"- {item}" for item in self.blocked_patterns],
            "验收点：",
            *[f"- {item}" for item in self.acceptance_checks],
        ]
        return "\n".join(line for line in lines if line)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "focus": self.focus,
            "rewrite_directives": self.rewrite_directives,
            "pov_card": self.pov_card,
            "local_revision": self.local_revision,
            "blocked_patterns": self.blocked_patterns,
            "acceptance_checks": self.acceptance_checks,
            "prompt_block": self.prompt_block,
        }


def build_writer_loop_plan(
    *,
    chapter_number: int,
    goal: str = "",
    required_beats: str = "",
    constraints: str = "",
    quality_report: str | dict | None = None,
    sample_report: dict | None = None,
    previous_content: str = "",
    mode: str = "draft",
) -> WriterLoopPlan:
    quality = _loads_report(quality_report)
    dimensions = quality.get("dimensions") if isinstance(quality.get("dimensions"), dict) else {}
    issues = [str(item) for item in quality.get("issues", [])] if isinstance(quality.get("issues"), list) else []
    sample_issues = [str(item) for item in (sample_report or {}).get("issues", [])]
    repeated_motifs = [str(item) for item in (sample_report or {}).get("repeated_motifs", [])]
    low_dims = _low_dimensions(dimensions)
    directives = _rewrite_directives(
        chapter_number=chapter_number,
        low_dims=low_dims,
        issues=[*issues, *sample_issues],
        repeated_motifs=repeated_motifs,
        mode=mode,
    )
    blocked = _blocked_patterns(issues=[*issues, *sample_issues], repeated_motifs=repeated_motifs)
    pov_card = _pov_card(
        chapter_number=chapter_number,
        goal=goal,
        required_beats=required_beats,
        constraints=constraints,
        issues=[*issues, *sample_issues],
        previous_content=previous_content,
    )
    local_revision = _local_revision_plan(low_dims=low_dims, issues=issues, dimensions=dimensions)
    checks = _acceptance_checks(low_dims=low_dims, issues=[*issues, *sample_issues])
    focus = _focus(low_dims=low_dims, issues=[*issues, *sample_issues], sample_report=sample_report)
    status = "needs_action" if directives or blocked else "ready"
    return WriterLoopPlan(
        status=status,
        focus=focus,
        rewrite_directives=directives,
        pov_card=pov_card,
        local_revision=local_revision,
        blocked_patterns=blocked,
        acceptance_checks=checks,
    )


def sample_failure_director(sample_report: dict, *, chapter_number: int = 1) -> dict:
    plan = build_writer_loop_plan(
        chapter_number=chapter_number,
        sample_report=sample_report,
        mode="sample_retry",
    )
    return {
        "score": int(sample_report.get("score") or 0),
        "focus": plan.focus,
        "rewrite_directives": plan.rewrite_directives,
        "blocked_patterns": plan.blocked_patterns,
        "acceptance_checks": plan.acceptance_checks,
        "retry_brief": "\n".join([*plan.rewrite_directives, *[f"避免复刻：{item}" for item in plan.blocked_patterns]]),
    }


def local_revision_brief_lines(quality_report: str | dict | None, *, chapter_number: int = 1) -> list[str]:
    plan = build_writer_loop_plan(chapter_number=chapter_number, quality_report=quality_report, mode="revision")
    local = plan.local_revision
    if not local.get("recommended"):
        return []
    lines = [
        f"局部修订闭环：优先修复 {local.get('target_dimension')}={local.get('score')}，不要整章换方向。",
        *[str(item) for item in local.get("directives", [])],
        "修订后必须重新评分；若同一维度仍低于阈值，再升级为结构重写。",
    ]
    return lines


def _loads_report(value: str | dict | None) -> dict:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return {"raw_report": value}
    return loaded if isinstance(loaded, dict) else {}


def _low_dimensions(dimensions: dict) -> dict[str, int]:
    thresholds = {
        "visual_staging": 60,
        "imageable_paragraphs": 60,
        "dialogue_fullness": 65,
        "memorable_dialogue": 55,
        "embodied_pov": 65,
        "observation_logic": 70,
        "reader_momentum": 65,
        "conflict_pressure": 65,
        "choice_and_cost": 65,
        "hook_strength": 65,
        "character_action": 65,
    }
    rows: dict[str, int] = {}
    for name, threshold in thresholds.items():
        value = dimensions.get(name)
        if isinstance(value, int) and value < threshold:
            rows[name] = value
    return rows


def _rewrite_directives(
    *,
    chapter_number: int,
    low_dims: dict[str, int],
    issues: list[str],
    repeated_motifs: list[str],
    mode: str,
) -> list[str]:
    rows: list[str] = []
    if repeated_motifs:
        rows.append(f"下一轮必须换叙事发动机，重复母题不得超过一个：{'、'.join(repeated_motifs[:6])}")
    if any("sample1" in issue for issue in issues):
        rows.append("样本一必须从本书核心场景内的具体困境或陌生关系压力起步；现实片场、演员履历、出租屋头盔和内测资格只能作为背景压力，不能再当开场发动机。")
    if any("actor_shortcut" in issue for issue in issues):
        rows.append("所有小样都必须拔掉职业经验拐杖：角色判断要来自现场证据、身体反应和人物矛盾，不能靠横店、龙套、导演教过或演过来解决冲突。")
    if "visual_staging" in low_dims or any("visual_underdeveloped" in issue for issue in issues):
        rows.append("重写低画面段：每个场景先交代站位、光源、可见物、动作路径，再让人物行动改变空间局势。")
    if "imageable_paragraphs" in low_dims:
        rows.append("把抽象说明改成可见画面：物件、手势、气味、声音、触感至少两项进入段落。")
    if "dialogue_fullness" in low_dims or "memorable_dialogue" in low_dims:
        rows.append("对白必须带人物处境和性格，禁止只用一两个字传递功能信息；每个关键配角至少一句有态度的话。")
    if "embodied_pov" in low_dims:
        rows.append("所有环境描写先经过角色感官和误判，不写摄像头式客观扫景。")
    if "observation_logic" in low_dims:
        rows.append("角色推断必须有可见证据、距离条件和不确定语气，不允许一眼认出不可见细节。")
    if {"reader_momentum", "conflict_pressure", "choice_and_cost"} & set(low_dims):
        rows.append("主角必须在本场有短期目标、被阻碍、做选择并付出可见代价。")
    if "hook_strength" in low_dims:
        rows.append("章末钩子必须由本章行动自然引出，不用突兀追杀、坠崖或陌生人硬塞秘密。")
    if not rows and mode.startswith("sample"):
        rows.append("三版小样必须换目标、压力来源、配角功能和章末诱因，不允许只换地点。")
    if not rows and chapter_number <= 1:
        rows.append("第一章优先建立主角处境、真实武侠世界压力和一个可复述的选择。")
    return rows[:8]


def _blocked_patterns(*, issues: list[str], repeated_motifs: list[str]) -> list[str]:
    rows = [f"重复母题：{item}" for item in repeated_motifs[:8]]
    if any("sample1" in issue for issue in issues):
        rows.append("样本一旧入口警戒：横店/演员/替身费/出租屋头盔/内测资格")
    if any("actor_shortcut" in issue for issue in issues):
        rows.append("职业经验万能解法：横店/龙套/导演教过/演过")
    if any("坠崖" in item for item in repeated_motifs):
        rows.append("追杀坠崖当万能钩子")
    if any("被盘问" in item or "茶棚欠账" in item for item in repeated_motifs):
        rows.append("欠账或盘问当默认开场冲突")
    return rows[:10]


def _pov_card(
    *,
    chapter_number: int,
    goal: str,
    required_beats: str,
    constraints: str,
    issues: list[str],
    previous_content: str,
) -> dict[str, str]:
    source = "\n".join([goal, required_beats, constraints, previous_content])
    fear = "被这个真实江湖看穿来路，或因一次误判付出无法撤销的代价。"
    if any(marker in source for marker in ("伤", "毒", "追", "血")):
        fear = "身体撑不住，判断却不能停；他怕露怯，也怕错过唯一活路。"
    elif chapter_number <= 1:
        fear = "他还没弄清规则，却已经需要用一次选择证明自己能活下去。"
    misread = "先用玩家/现代经验误判现场，再被人物反应或物证纠正。"
    if any("observation" in issue for issue in issues):
        misread = "只能根据近处可见证据试探判断，强结论必须改成多半、若是、除非。"
    return {
        "当下欲望": "先解决眼前一件会立刻伤身、丢钱、丢脸或暴露身份的小事。",
        "当下恐惧": fear,
        "感官入口": "从他先听见、闻到、摸到、疼到或看错的一处细节开场。",
        "误判机制": misread,
        "沉默理由": "他不立刻说真话，因为真话无法解释来历，且会让旁人掌握主动。",
        "身体反应": "写出饥饿、冷、疼、手心汗、喉咙发紧、脚步停顿中的至少两项。",
    }


def _local_revision_plan(*, low_dims: dict[str, int], issues: list[str], dimensions: dict) -> dict[str, object]:
    priority = [
        ("visual_staging", 60),
        ("imageable_paragraphs", 60),
        ("observation_logic", 70),
        ("dialogue_fullness", 65),
        ("memorable_dialogue", 55),
        ("embodied_pov", 65),
    ]
    for name, threshold in priority:
        score = low_dims.get(name)
        if score is None:
            continue
        return {
            "recommended": True,
            "target_dimension": name,
            "score": score,
            "threshold": threshold,
            "directives": _local_directives(name),
        }
    if any("visual_underdeveloped" in issue for issue in issues):
        return {
            "recommended": True,
            "target_dimension": "visual_staging",
            "score": dimensions.get("visual_staging", 0),
            "threshold": 60,
            "directives": _local_directives("visual_staging"),
        }
    return {"recommended": False, "target_dimension": "", "score": 0, "threshold": 0, "directives": []}


def _local_directives(name: str) -> list[str]:
    mapping = {
        "visual_staging": [
            "定位低画面段落，补站位、光源、可见物、人物移动路径。",
            "每段至少让一个动作改变空间关系，例如逼近、退后、遮挡、露出、跌坐、撞翻。",
        ],
        "imageable_paragraphs": [
            "把抽象设定句改成角色能看见或碰到的物件和痕迹。",
            "每个关键段落保留一个能被读者复述的画面中心。",
        ],
        "observation_logic": [
            "检查所有“一眼看出/认出/知道”，补足可见证据或改成试探。",
            "不可见细节改成鞋印、磨痕、袖口、刀鞘、气味、声音等可感知线索。",
        ],
        "dialogue_fullness": [
            "把惜字如金的功能对白扩成带身份、情绪和利益的说法。",
            "同一场至少让两个人说话方式明显不同。",
        ],
        "memorable_dialogue": [
            "为关键配角设计一句能暴露处境或性格的记忆点台词。",
            "删除只负责解释设定的对白。",
        ],
        "embodied_pov": [
            "所有环境句先落到主角感官、身体反应或误判。",
            "删掉摄像头扫景式段落。",
        ],
    }
    return mapping.get(name, ["按低分维度做局部修订，不改变可用主线。"])


def _acceptance_checks(*, low_dims: dict[str, int], issues: list[str]) -> list[str]:
    checks = [
        "读者能复述本轮换掉了哪个叙事发动机。",
        "至少一个场景能在脑中形成站位和动作画面。",
        "主角有明确短期目标、误判、选择和后果。",
    ]
    if "dialogue_fullness" in low_dims or "memorable_dialogue" in low_dims:
        checks.append("关键对白读起来像具体人物，而不是系统传话。")
    if "observation_logic" in low_dims or any("precision" in issue for issue in issues):
        checks.append("所有推断都有可见证据和合理语气。")
    if any("sample1" in issue for issue in issues):
        checks.append("样本一不把现实片场或内测头盔当开场发动机。")
    if any("actor_shortcut" in issue for issue in issues):
        checks.append("所有判断都来自现场证据，不来自演员职业经验。")
    return checks[:6]


def _focus(*, low_dims: dict[str, int], issues: list[str], sample_report: dict | None) -> str:
    if sample_report:
        return "小样方向重置：先换叙事发动机，再谈文笔。"
    if "visual_staging" in low_dims or any("visual_underdeveloped" in issue for issue in issues):
        return "正文局部修订：优先补画面调度和空间行动。"
    if "dialogue_fullness" in low_dims or "memorable_dialogue" in low_dims:
        return "正文局部修订：优先补人物对白声线。"
    if "embodied_pov" in low_dims:
        return "正文局部修订：优先贴住角色感官和误判。"
    return "生成前导演：保持反模板、贴身视角和表达准确。"
