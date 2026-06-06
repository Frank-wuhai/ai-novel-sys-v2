from __future__ import annotations

import json
import time
from dataclasses import dataclass

from openai import BadRequestError, OpenAI

from app.core.config import settings


@dataclass
class LLMResponse:
    text: str
    provider: str
    model: str
    prompt_chars: int = 0
    response_chars: int = 0
    estimated_prompt_tokens: int = 0
    estimated_response_tokens: int = 0
    elapsed_ms: int = 0
    usage: dict | None = None
    request_id: str = ""


class BaseLLMProvider:
    name = "base"

    def generate(
        self,
        prompt: str,
        *,
        max_tokens: int = 2000,
        temperature: float | None = None,
        response_format: dict | None = None,
        model: str | None = None,
    ) -> LLMResponse:
        raise NotImplementedError


class DryRunProvider(BaseLLMProvider):
    name = "dry_run"

    def generate(
        self,
        prompt: str,
        *,
        max_tokens: int = 2000,
        temperature: float | None = None,
        response_format: dict | None = None,
        model: str | None = None,
    ) -> LLMResponse:
        started = time.perf_counter()
        if "reviewer_json_schema" in prompt:
            text = json.dumps(
                {
                    "verdict": "pass",
                    "score": 82,
                    "strengths": ["dry-run reviewer confirms visible pressure, choice, cost, and hook"],
                    "issues": [],
                    "revision_suggestions": ["正式使用时请启用 live reviewer 获取真实审稿意见"],
                    "risk_flags": [],
                },
                ensure_ascii=False,
            )
            return LLMResponse(
                text=text,
                provider=self.name,
                model="dry-run",
                prompt_chars=len(prompt),
                response_chars=len(text),
                estimated_prompt_tokens=estimate_tokens(prompt),
                estimated_response_tokens=estimate_tokens(text),
                elapsed_ms=_elapsed_ms(started),
                usage=None,
                request_id="dry-run",
            )
        paragraphs = [
            "林澈站在旧楼天台边缘时，异象已经逼近到第三次闪烁。远处的广告牌像被看不见的手拧弯，红光一层层压下来，所有声音都被挤成细线。他知道这一章不能只躲开危机，必须把压力推到选择面前。",
            "楼梯口的保安最先看见他，手电光扫过水泥地上的碎玻璃，也扫到他掌心渗出的血。“小伙子，你刚才在天台上看见什么了？”保安声音发紧，另一只手已经摸向对讲机。林澈没立刻答，他先看见栏杆外侧挂着半截红色书包带，又听见楼下孩子母亲压着哭声喊人。",
            "他闭上眼，短暂推演三条结果。第一条路会让楼下的人群安全撤离，却会暴露他的能力；第二条路能保住秘密，但那名被困的孩子会被坠落的水箱砸中；第三条路最安静，也最残忍，需要他付出一段关于母亲声音的记忆。",
            "代价不是抽象的。林澈听见脑海里有东西被擦掉，像旧磁带忽然空白。他伸手抓住栏杆，还是选择了第三条路，因为这能同时换来救人和继续追查源头的机会。收益和损耗在同一秒落地，他没有无损解决危机。",
            "水箱偏离原本轨迹，砸碎了天台角落的玻璃棚。孩子获救，人群只看见一道模糊倒影。林澈却发现倒影比现实慢了半拍，镜面里还有另一个自己抬头，看向城市中心那片没有星光的黑雾。",
            "这一发现把剧情段目标往前推了一步：异象并非偶发事件，能力也不是单纯的幸运。林澈把破碎玻璃收进掌心，疼痛提醒他保持在已知规则边界内，不能把推演当成万能答案。",
            "回到楼梯间时，他遇见熟人。对方叫出了他的名字，林澈却想不起那人的姓氏。那人盯着他掌心的血，皱眉问：“你又用那东西了？上次你说过，再用就会忘掉重要的人。”选择的后果终于具象化，代价钉进日常。林澈喉咙发紧，只能反问：“你最近有没有见过同样慢半拍的倒影？”",
            "对方脸色变了，压低声音说：“昨晚零号线也出事了，车窗里的人比站台上的人晚眨眼。有人拍到，第二天视频全没了。”这个信息增量像一枚钩子，把林澈从单点危机拉进更大的秘密。他意识到自己追查的不是一场事故，而是一条正在扩散的异常。",
            "林澈沿着楼梯往下走，每一级台阶都像在提醒他刚才的交换。他不敢立刻再次使用能力，因为 Story Bible 里最重要的禁区已经写得很清楚：不得无代价解决危机，也不得推翻已登记能力限制。剧情段边界同样压在他心里，这一段必须围绕第一次代价推演继续推进。",
            "他把手机录音打开，让自己复述刚得到的线索：城市中心、地铁站、镜面延迟、黑雾源头。复述不是解释，而是防止记忆继续剥落。选择已经做出，后果也已经开始扩散，他必须在下一次危机逼近前确认能力还能承受多少损耗。",
            "楼下警笛声越来越近，新的阻碍随之出现。有人看见了天台上的倒影，有人正在寻找那个救下孩子的人。林澈如果暴露，就会失去追查秘密的主动权；如果沉默，更多人可能在下一场异象里受伤。压力没有解除，只是换成了更难的选择。",
            "他最终把碎玻璃藏进衣袋，决定先去地铁站确认源头。孩子母亲追到楼道口，声音发抖：“救人的人是不是你？我不问你怎么做到的，我只问你还会不会回来作证。”林澈看见她怀里孩子还攥着那截红书包带，心里一沉，点头说：“会。但在警察来之前，你先别靠近任何反光的东西。”",
            "章末，黑雾深处传来一声很轻的玻璃碎裂声。林澈手机屏幕亮起，陌生号码只发来一句话：如果你还记得她的声音，就别再推演下一次。秘密、代价和新的危机同时压来，他只能继续选择。",
            "他没有立刻回复。楼道的声控灯一盏盏亮起，又一盏盏熄灭，像有人从看不见的地方走下来。林澈扶着墙站稳，先确认孩子已经被邻居抱走，再把天台门上的血迹用袖口擦掉。这个动作很笨，也很必要，因为他知道下一次追查会从这些痕迹开始。",
            "楼下有人喊他的名字，语气里带着怀疑。林澈把手机攥紧，逼自己把刚才的选择重新想一遍：救人暴露能力，保密会死人，推演会丢记忆。他没有找到完美答案，只找到一个还能承受的答案。读者能看见他不是被剧情推着走，而是在压力里自己选了一条更难的路。",
            "当他推开单元门，雨已经停了。地面上的积水映出城市中心那团黑雾，倒影里却多出一座不存在的车站。林澈弯腰去看，水面忽然浮起一行模糊的站名，和陌生短信里的警告对应上了。新的信息不是旁白解释，而是从他刚做出的决定里冒出来。",
            "他终于回复那个号码，只打了三个字：你是谁。发送成功的瞬间，手机电量从百分之四十跳到百分之一，像有什么东西顺着信号线咬了他一口。屏幕上跳出一行新字：“带着碎玻璃来零号线，别带警察。”收益、代价和更大麻烦同时落地，章节没有停在胜利，而是停在更危险的下一步。",
            "警笛停在小区门口时，林澈已经绕进侧门。保安亭里的监控屏一格格闪烁，每一格里都有一秒钟前的他。他意识到镜面延迟不只存在于玻璃，也开始侵入摄像头、积水和所有会反光的东西。危机扩大了，但线索也更清楚了。",
            "他原本可以马上逃走，可楼上传来孩子母亲的哭声，那声音把他钉在原地。他回头看了一眼，确认没有新的坠落物，才压低帽檐往地铁站方向走。这个停顿让追踪者更接近，也让他的选择更像一个活人会做的选择，而不是剧情为了推进硬拽出来的动作。",
            "路口的电子站牌忽然跳出一行不存在的线路：零号线，终点站，黑雾中心。林澈胸口一紧，丢失的那段母亲声音又在耳边断断续续响起。他知道这是诱饵，也知道自己没有资格无视。上一场选择救了一个人，下一场选择可能会决定整条街还能不能醒来。",
            "他把碎玻璃握得更紧，掌心被割开的疼痛让他保持清醒。身后有人踏进水洼，倒影却比脚步更先靠近。林澈没有回头，只在心里数到三，然后冲进即将关闭的末班地铁。车门合拢前，陌生号码再次亮起：欢迎抵达第一次真正的推演现场。",
            "车厢里没有乘客，只有每一扇窗都映着不同时间的他。林澈靠在门边喘息，第一次清楚意识到，自己不是在结束一场异象，而是在亲手打开通往更深处的门。下一站的广播声响起时，报出的却是他母亲的名字。",
            "林澈的手指僵在门边。他想退回站台，可车厢已经开始滑动，窗外的广告灯被拉成一条条红线。每条红线里都藏着一个小小的画面：天台、水箱、孩子、黑雾，以及他刚刚丢掉的那段记忆。",
            "这不是单纯的恐吓。对方知道他的代价，也知道他还没学会怎么保护自己。林澈强迫自己把呼吸压稳，先观察车厢，再确认出口，最后把碎玻璃藏到袖口里。动作越具体，他越能从恐惧里夺回一点主动权。",
            "车厢广播第二次响起，声音却变成了那个陌生号码的语调：“下一站，终点站。林澈，别再假装你还有退路。”林澈抬头，看见线路图上多出一个黑色圆点，正一点点吞掉周围站名。他终于明白，黑雾不是在等他靠近，而是在用他的选择把他拖进来。",
            "他可以砸窗，可以拉紧急制动，也可以继续坐到终点。前两种选择可能立刻暴露位置，第三种选择最危险，却能看到源头。林澈想起刚被救下的孩子，想起自己正在消失的记忆，最终把手从制动阀上移开。",
            "选择落定，代价立刻出现。他手机通讯录里母亲的号码变成了一串空白，备注、头像、通话记录全都被抹掉。林澈眼眶发热，却没有停下记录。他在备忘录里写下四个字：不能白丢。",
            "列车冲进黑雾前，车窗上浮现出另一行字：如果你能在终点站活下来，就能拿回第一块记忆碎片。林澈握紧碎玻璃，知道下一章已经不是追查，而是交易。危险、收益和代价一起摆上桌，他只能继续往前。",
            "列车停下时，车门外不是站台，而是一条被雨水淹没的旧街。街边所有招牌都倒着亮，像有人把城市翻进了镜子里。林澈没有急着下车，他先把能看见的出口、反光面和声音来源记下来，因为刚才的教训已经足够清楚：每一次鲁莽都会拿走他身上某样重要的东西。",
            "车门上方的摄像头转了一下，红点正对他的脸。林澈把帽檐压低，听见车厢顶棚里传来细小电流声，像有人贴着铁皮呼吸。他没有再碰手机，只用指甲在掌心划出三道浅痕，提醒自己还欠着三件事：救人作证、查清零号线、把那段被抹掉的声音找回来。",
            "车厢尽头传来轻轻的敲击声。一个穿校服的女孩站在玻璃另一侧，嘴唇无声开合，手里举着林澈母亲年轻时的照片。林澈的呼吸停了一瞬，随即意识到这是黑雾给出的诱饵。它知道他缺什么，也知道他愿意为了什么付出代价。",
            "他把碎玻璃贴近掌心，借疼痛逼自己别被照片牵着走。先找规则，再谈救人，这是他刚刚用一段记忆换来的经验。可女孩身后的水面忽然鼓起，像有什么东西正从旧街下面爬出来。林澈知道自己没有太多观察时间。",
            "于是他做了进入终点站后的第一个选择：不冲向照片，也不立刻逃走，而是把车门卡住，给自己留下退路。这个选择看似保守，却让他第一次没有被异象牵着鼻子走。黑雾里的敲击声停了，像是某个看不见的东西也没想到他会这样选。",
        ]
        content = "测试草稿\n\n" + "\n\n".join(paragraphs)
        text = json.dumps(
            {
                "title": "测试草稿",
                "content": content,
                "self_check": [
                    "dry-run output only",
                    "not publishable prose",
                    "structure fields present",
                ],
                "used_brief_points": [
                    "goal",
                    "required_beats",
                    "constraints",
                ],
            },
            ensure_ascii=False,
        )
        return LLMResponse(
            text=text,
            provider=self.name,
            model="dry-run",
            prompt_chars=len(prompt),
            response_chars=len(text),
            estimated_prompt_tokens=estimate_tokens(prompt),
            estimated_response_tokens=estimate_tokens(text),
            elapsed_ms=_elapsed_ms(started),
            usage=None,
            request_id="dry-run",
        )


class ArkOpenAIProvider(BaseLLMProvider):
    name = "ark_openai_compatible"

    def __init__(self) -> None:
        api_key = _ark_api_key_for_current_plan()
        if not api_key or not settings.ark_base_url:
            raise RuntimeError(_missing_ark_credentials_message())
        _validate_ark_plan_base_url(settings.ark_base_url)
        self.client = OpenAI(api_key=api_key, base_url=settings.ark_base_url)

    def generate(
        self,
        prompt: str,
        *,
        max_tokens: int = 2000,
        temperature: float | None = None,
        response_format: dict | None = None,
        model: str | None = None,
    ) -> LLMResponse:
        started = time.perf_counter()
        kwargs = {
            "model": model or settings.model_name,
            "messages": [
                {"role": "system", "content": "你是网文生产系统里的受控写作工位，只按结构化输入生成草稿。"},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_tokens,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if response_format is not None:
            kwargs["response_format"] = response_format
        try:
            result = self.client.chat.completions.create(
                **kwargs,
            )
        except BadRequestError as exc:
            if response_format is None or not _is_unsupported_response_format_error(exc):
                raise
            kwargs.pop("response_format", None)
            result = self.client.chat.completions.create(
                **kwargs,
            )
        text = result.choices[0].message.content or ""
        usage = _usage_dict(getattr(result, "usage", None))
        request_id = getattr(result, "id", "") or ""
        return LLMResponse(
            text=text,
            provider=self.name,
            model=model or settings.model_name,
            prompt_chars=len(prompt),
            response_chars=len(text),
            estimated_prompt_tokens=estimate_tokens(prompt),
            estimated_response_tokens=estimate_tokens(text),
            elapsed_ms=_elapsed_ms(started),
            usage=usage,
            request_id=request_id,
        )


def get_provider(dry_run: bool) -> BaseLLMProvider:
    if dry_run:
        return DryRunProvider()
    return ArkOpenAIProvider()


def _is_unsupported_response_format_error(exc: BadRequestError) -> bool:
    text = str(exc)
    return "response_format" in text and "not supported" in text


def _validate_ark_plan_base_url(base_url: str) -> None:
    plan = settings.llm_plan
    if settings.llm_require_coding_plan:
        plan = "coding_plan"
    if plan in {"coding", "coding_plan"} and not _is_coding_plan_base_url(base_url):
        raise RuntimeError(
            "Coding Plan guard blocked live LLM call: ARK_BASE_URL must be "
            "https://ark.cn-beijing.volces.com/api/coding/v3 when LLM_PLAN=coding_plan "
            "or LLM_REQUIRE_CODING_PLAN=true"
        )
    if plan in {"agent", "agent_plan"} and not _is_agent_plan_base_url(base_url):
        raise RuntimeError(
            "Agent Plan guard blocked live LLM call: ARK_BASE_URL must be the dedicated "
            "Agent Plan OpenAI-compatible endpoint https://ark.cn-beijing.volces.com/api/plan/v3. "
            "Do not use the regular Ark API endpoint or the old Coding Plan gateway."
        )


def _ark_api_key_for_current_plan() -> str:
    if settings.llm_require_coding_plan or settings.llm_plan in {"coding", "coding_plan"}:
        return settings.ark_api_key
    if settings.llm_plan in {"agent", "agent_plan"}:
        return settings.ark_agent_plan_api_key
    return settings.ark_api_key


def _missing_ark_credentials_message() -> str:
    if settings.llm_plan in {"agent", "agent_plan"} and not settings.llm_require_coding_plan:
        return "ARK_AGENT_PLAN_API_KEY and ARK_BASE_URL are required for Agent Plan live LLM calls"
    return "ARK_API_KEY and ARK_BASE_URL are required for live LLM calls"


def _is_agent_plan_base_url(base_url: str) -> bool:
    normalized = base_url.rstrip("/")
    return normalized == "https://ark.cn-beijing.volces.com/api/plan/v3"


def _is_coding_plan_base_url(base_url: str) -> bool:
    normalized = base_url.rstrip("/")
    return normalized == "https://ark.cn-beijing.volces.com/api/coding/v3"


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    non_ascii_chars = len(text) - ascii_chars
    return max(1, round(ascii_chars / 4 + non_ascii_chars / 1.6))


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))


def _usage_dict(usage: object) -> dict | None:
    if usage is None:
        return None
    data = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = getattr(usage, key, None)
        if value is not None:
            data[key] = value
    return data or None
