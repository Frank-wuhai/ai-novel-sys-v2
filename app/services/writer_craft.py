from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Book, Chapter, ChapterVersion, GenerationTask
from app.services.expression_precision import precision_prompt_rules


@dataclass(frozen=True)
class WriterCraftContext:
    author_intent: dict[str, str]
    narrative_experiments: list[dict[str, str]]
    design_assets: list[str]
    voice_cards: list[str]
    negative_feedback: list[str]
    pov_rules: list[str]
    precision_rules: list[str]
    revision_checklist: list[str]
    memory_targets: list[str]

    @property
    def prompt_block(self) -> str:
        lines = [
            "作家化创作层（优先级高于机械节拍，禁止原样输出标题）：",
            "作者意图：",
            f"- 核心情绪：{self.author_intent.get('core_emotion', '')}",
            f"- 主角转折：{self.author_intent.get('protagonist_turn', '')}",
            f"- 读者记忆点：{self.author_intent.get('reader_memory', '')}",
            f"- 本章审美判断：{self.author_intent.get('aesthetic_judgment', '')}",
            f"- 必须避开的俗套：{self.author_intent.get('avoid_cliche', '')}",
            "",
            "本章可用叙事实验：",
            *[f"- {item['name']}：{item['method']}；风险：{item['risk']}" for item in self.narrative_experiments[:4]],
            "",
            "设计感素材要求：",
            *[f"- {item}" for item in self.design_assets[:6]],
            "",
            "人物声音卡：",
            *[f"- {item}" for item in self.voice_cards[:5]],
            "",
            "负反馈避让：",
            *[f"- {item}" for item in self.negative_feedback[:7]],
            "",
            "角色贴身视角：",
            *[f"- {item}" for item in self.pov_rules],
            "",
            "语言表述准确性：",
            *[f"- {item}" for item in self.precision_rules],
            "",
            "作家修订自检：",
            *[f"- {item}" for item in self.revision_checklist],
            "",
            "章节记忆点验收：",
            *[f"- {item}" for item in self.memory_targets],
        ]
        return "\n".join(line for line in lines if line is not None)

    def to_dict(self) -> dict:
        return {
            "author_intent": self.author_intent,
            "narrative_experiments": self.narrative_experiments,
            "design_assets": self.design_assets,
            "voice_cards": self.voice_cards,
            "negative_feedback": self.negative_feedback,
            "pov_rules": self.pov_rules,
            "precision_rules": self.precision_rules,
            "revision_checklist": self.revision_checklist,
            "memory_targets": self.memory_targets,
        }


def build_writer_craft_context(
    session: Session,
    *,
    book: Book,
    chapter_number: int,
    goal: str,
    required_beats: str,
    constraints: str,
    previous_chapter_context: str,
    canon_context: str,
) -> WriterCraftContext:
    source = "\n".join([goal, required_beats, constraints, previous_chapter_context, canon_context])
    author_intent = _author_intent(source=source, chapter_number=chapter_number)
    negative_feedback = _negative_feedback(session, book_id=book.id, chapter_number=chapter_number)
    return WriterCraftContext(
        author_intent=author_intent,
        narrative_experiments=_narrative_experiments(chapter_number=chapter_number, source=source),
        design_assets=_design_assets(book=book, source=source),
        voice_cards=_voice_cards(source=source),
        negative_feedback=negative_feedback,
        pov_rules=_pov_rules(),
        precision_rules=precision_prompt_rules(),
        revision_checklist=_revision_checklist(),
        memory_targets=_memory_targets(),
    )


def evaluate_writer_craft(text: str) -> dict:
    paragraphs = [item.strip() for item in str(text or "").splitlines() if item.strip()]
    memorable_image = _score_memorable_image(text)
    memorable_dialogue = _score_memorable_dialogue(text)
    designed_asset = _score_designed_asset(text)
    character_action = _score_character_action(text)
    chapter_necessity = _score_chapter_necessity(text)
    embodied_pov = _score_embodied_pov(text)
    score = round(
        (
            memorable_image
            + memorable_dialogue
            + designed_asset
            + character_action
            + chapter_necessity
            + embodied_pov
        )
        / 6
    )
    issues = []
    if memorable_image < 60:
        issues.append(f"memory_image_low:{memorable_image}")
    if memorable_dialogue < 55:
        issues.append(f"memory_dialogue_low:{memorable_dialogue}")
    if designed_asset < 60:
        issues.append(f"designed_asset_low:{designed_asset}")
    if character_action < 60:
        issues.append(f"character_action_low:{character_action}")
    if chapter_necessity < 60:
        issues.append(f"chapter_necessity_low:{chapter_necessity}")
    if embodied_pov < 60:
        issues.append(f"embodied_pov_low:{embodied_pov}")
    if len(paragraphs) < 8:
        issues.append("scene_sequence_thin")
    return {
        "score": score,
        "checks": {
            "memorable_image": memorable_image,
            "memorable_dialogue": memorable_dialogue,
            "designed_asset": designed_asset,
            "character_action": character_action,
            "chapter_necessity": chapter_necessity,
            "embodied_pov": embodied_pov,
        },
        "issues": issues,
    }


def _author_intent(*, source: str, chapter_number: int) -> dict[str, str]:
    if chapter_number <= 1:
        return {
            "core_emotion": "先让读者看见主角的具体处境，再让异常规则压进生活。",
            "protagonist_turn": "主角从旁观/误判转为主动做一个有代价的选择。",
            "reader_memory": "至少留下一个能复述的画面、一个具体物件和一个未解压力。",
            "aesthetic_judgment": "开局要像作者精心摆过场，不像临时拼装的新手任务。",
            "avoid_cliche": "避免同款追杀、坠崖、欠账、NPC盘问和万能职业经验。",
        }
    if any(marker in source for marker in ("伤", "毒", "追", "危", "死")):
        emotion = "压迫感里带清醒判断，恐惧、疼痛、误判和硬撑都要具体。"
    elif any(marker in source for marker in ("秘密", "规则", "发现", "试探")):
        emotion = "好奇和风险并行，每个发现都要换来更麻烦的后果。"
    else:
        emotion = "让人物目标推动场景，不靠设定说明推着读者走。"
    return {
        "core_emotion": emotion,
        "protagonist_turn": "主角必须在证据增加后修正判断，并承担一个可见后果。",
        "reader_memory": "本章要有一个强画面、一句带人物声线的话、一个带代价的物件/规矩。",
        "aesthetic_judgment": "每个专名和场景都要像被作者设计过，有来源、功能或利益关系。",
        "avoid_cliche": "避开上一章/近期样本重复的开场、冲突来源、结尾钩子和解法。",
    }


def _narrative_experiments(*, chapter_number: int, source: str) -> list[dict[str, str]]:
    rows = [
        ("人物处境型", "从主角当前困境、欲望或羞耻感开场，让选择先于设定出现", "节奏可能慢，必须尽快给出压力"),
        ("关系压力型", "让一个有私心的配角带来请求、威胁、误会或旧账", "配角不能只当传话工具"),
        ("规则误判型", "让主角按旧经验行动，立刻被真实世界规则打脸", "不能写成作者旁白讲规则"),
        ("场景奇观型", "用一个有空间、光源、气味和物件的强场景吸住读者", "奇观必须推动选择"),
        ("道德选择型", "给主角两个都要付代价的选项，让人物立起来", "不能假选择"),
        ("信息悬疑型", "先给异常证据，再让主角试探和误判", "信息不能只靠偷听"),
    ]
    if chapter_number <= 1:
        rows = [rows[0], rows[2], rows[4], rows[5], rows[1], rows[3]]
    return [{"name": name, "method": method, "risk": risk} for name, method, risk in rows]


def _design_assets(*, book: Book, source: str) -> list[str]:
    genre = book.genre or "小说"
    return [
        f"新地名/组织名必须符合《{book.title}》的{genre}气质，并在首次出现时给出来源、外观、功能、利益关系或代价中的两项。",
        "关键物件不要只当道具名，必须带触感、旧痕、使用方式或所属关系。",
        "门派/家族/帮派不能只作为敌我标签，至少带一个具体规矩、营生或旧债。",
        "场景要有空间边界、光源/天气、人物站位和可互动的关键物。",
        "避免一章内连续堆新专名；宁可少而深，也不要多而散。",
        "设定释放优先通过交易、误判、伤痛、盘问、规矩执行和后果呈现。",
    ]


def _voice_cards(*, source: str) -> list[str]:
    return [
        "主角：可以嘴硬、找补、试探、误判；不要永远用冷静旁白解释自己。",
        "强势配角：说话要带位置感，威胁、试探或利益算盘藏在句子里。",
        "弱势配角：不要只求救，要有顾虑、隐瞒、讨价还价或自保动作。",
        "江湖/异世界人物：话里带身份、规矩、旧怨、生计和地方判断。",
        "对白验收：删掉后如果人物性格不受影响，这句就是功能对白，必须重写。",
    ]


def _pov_rules() -> list[str]:
    return [
        "每个主要场景必须贴住一个角色的当下感知写：先写他/她听见、看见、闻到、摸到或疼到什么，再让环境成立。",
        "环境描写不能像摄像头扫景；同一处环境要带角色判断、误解、害怕、厌烦、贪念或迟疑。",
        "对话前后要有角色接收反应：哪句话刺到了他、他先误会了什么、身体哪里先紧了、为什么改口。",
        "切换观察对象时要明确视角落点，避免上帝视角同时知道所有人的表情和动机。",
        "每 3-5 段至少出现一次角色身体反应、感官细节或内心误判；不要只客观记录谁说了什么、谁走到哪里。",
    ]


def _negative_feedback(session: Session, *, book_id: int, chapter_number: int) -> list[str]:
    rows = []
    recent_versions = list(
        session.scalars(
            select(ChapterVersion)
            .join(Chapter, ChapterVersion.chapter_id == Chapter.id)
            .where(Chapter.book_id == book_id, Chapter.chapter_number <= chapter_number)
            .order_by(ChapterVersion.id.desc())
            .limit(5)
        )
    )
    text = "\n".join(version.content[:1200] for version in recent_versions if version.content)
    motif_rules = [
        ("坠崖/山洞/奇遇", ("坠崖", "山洞", "崖", "秘籍", "奇遇")),
        ("欠账/茶棚/盘问", ("欠", "茶棚", "饭钱", "盘问", "路引")),
        ("追杀逼近", ("追杀", "火把", "别让", "围住", "灭口")),
        ("万能演员经验", ("龙套", "演员", "表演", "片场", "演过")),
        ("系统提示替代场景", ("检测到", "奖励生成", "系统提示", "相似度")),
    ]
    for label, markers in motif_rules:
        if any(marker in text for marker in markers):
            rows.append(f"近期已出现“{label}”，本章/小样不得换皮复用。")
    sample_tasks = list(
        session.scalars(
            select(GenerationTask)
            .where(GenerationTask.book_id == book_id, GenerationTask.task_type == "chapter_sample_lab")
            .order_by(GenerationTask.id.desc())
            .limit(4)
        )
    )
    if sample_tasks:
        rows.append("近期小样只作为避让历史，不得把采用/生成过的方向当作作者偏好。")
    return rows or ["主动检查是否复用了最近章节的开场、冲突来源、解法和章末钩子。"]


def _revision_checklist() -> list[str]:
    return [
        "删除解释性总结，把抽象判断换成动作、物件、站位、气味或对话反应。",
        "把客观摄像头式段落改成角色贴身视角：感官入口、身体反应、当下误判和情绪判断至少补两项。",
        "核对每个细节判断是否准确：角色能不能看见这个部位，物件和动词是否搭配，推理是否缺少中间证据。",
        "检查每段是否能画出来；不能画出来的段落补空间、光源、人物动作或关键物。",
        "检查每个新专名是否有设计锚点；没有锚点就删、合并或补来源/代价。",
        "检查对白是否暴露性格和关系位置；纯功能对白必须改。",
        "删除英译中式连接词和分析腔，让句子像中文小说自然流出来。",
        "章末钩子必须由本章行动导致，不能突然空降。",
    ]


def _memory_targets() -> list[str]:
    return [
        "本章最强画面：读者闭眼能看见人物站位和关键物。",
        "本章最强对白：一句话能听出说话人的身份、脾气或隐瞒。",
        "本章最强设定：不是名词，而是带代价的规矩、物件或关系。",
        "本章最强人物动作：主角做了一个改变局面的选择。",
        "本章不可删除性：删掉本章，人物关系、信息或压力链会明显断裂。",
    ]


def _score_memorable_image(text: str) -> int:
    markers = ("灯", "雨", "血", "门", "窗", "桌", "墙", "影", "手", "刀", "鞋", "泥", "火", "风", "味")
    action_markers = ("站", "坐", "跪", "推", "抓", "抬", "转", "撞", "塞", "捏", "按")
    score = 35 + min(35, sum(1 for marker in markers if marker in text) * 4)
    score += min(25, sum(1 for marker in action_markers if marker in text) * 4)
    return max(0, min(100, score))


def _score_memorable_dialogue(text: str) -> int:
    dialogue = re.findall(r"“([^”]{2,80})”", text or "")
    if not dialogue:
        return 25
    varied = len(set(item[:6] for item in dialogue))
    charged = sum(1 for item in dialogue if any(marker in item for marker in ("你", "我", "欠", "死", "走", "怕", "敢", "谁", "凭什么", "别")))
    return max(30, min(100, 35 + varied * 6 + charged * 5))


def _score_designed_asset(text: str) -> int:
    names = re.findall(r"[\u4e00-\u9fff]{2,8}(?:帮|派|门|谷|寨|楼|司|令|符|刀|剑|谱|诀|印|铃|帖|镖局)", text or "")
    anchors = sum(1 for marker in ("旧", "锈", "血", "刻", "规矩", "来历", "欠", "价", "代价", "门规", "账") if marker in text)
    return max(35, min(100, 45 + len(set(names)) * 8 + anchors * 5))


def _score_character_action(text: str) -> int:
    markers = ("决定", "选择", "改口", "伸手", "抓起", "推开", "咬牙", "抬手", "转身", "试探", "交换", "拒绝")
    cost = ("代价", "后果", "欠", "伤", "疼", "暴露", "失去", "误会")
    return max(30, min(100, 35 + sum(1 for marker in markers if marker in text) * 5 + sum(1 for marker in cost if marker in text) * 5))


def _score_chapter_necessity(text: str) -> int:
    causal = ("因此", "所以", "换来", "导致", "这才", "原来", "从此", "不得不", "只能", "必须")
    thread = ("秘密", "线索", "旧账", "规矩", "证据", "仇", "债", "约定", "追", "下一")
    tail = (text or "")[-600:]
    score = 35 + sum(1 for marker in causal if marker in text) * 5 + sum(1 for marker in thread if marker in text) * 4
    if any(marker in tail for marker in thread):
        score += 15
    return max(30, min(100, score))


def _score_embodied_pov(text: str) -> int:
    body = str(text or "")
    paragraphs = [item.strip() for item in body.splitlines() if item.strip()]
    if not paragraphs:
        return 0
    sensory = ("看见", "听见", "闻到", "摸到", "尝到", "疼", "冷", "热", "痒", "麻", "硌", "刺", "腥", "臭", "香", "汗", "喉咙", "后背", "手心", "指尖", "心口")
    cognition = ("以为", "觉得", "想", "意识到", "不对", "愣", "迟疑", "明白", "怀疑", "误会", "下意识", "本能")
    emotion = ("怕", "慌", "窘", "恼", "羞", "怒", "烦", "悔", "不甘", "发紧", "发凉", "发麻")
    objective_markers = ("只见", "与此同时", "此时", "众人", "所有人", "镜头", "画面", "场景")
    sensory_hits = sum(1 for marker in sensory if marker in body)
    cognition_hits = sum(1 for marker in cognition if marker in body)
    emotion_hits = sum(1 for marker in emotion if marker in body)
    pov_paragraphs = sum(
        1
        for paragraph in paragraphs
        if any(marker in paragraph for marker in sensory)
        or any(marker in paragraph for marker in cognition)
        or any(marker in paragraph for marker in emotion)
    )
    ratio = pov_paragraphs / len(paragraphs)
    score = 30 + min(25, sensory_hits * 3) + min(20, cognition_hits * 3) + min(15, emotion_hits * 3) + round(ratio * 20)
    score -= min(20, sum(body.count(marker) for marker in objective_markers) * 4)
    return max(0, min(100, score))
