from __future__ import annotations


def author_status_text(value: str) -> str:
    text = str(value or "")
    if not text:
        return "系统正在检查当前状态。"
    if any(marker in text for marker in ("canon", "Canon", "旧设定锚点", "上下文污染", "反方向词")):
        return "作品设定和章节资料不同步，系统会先清理旧内容并同步当前设定。"
    if any(marker in text for marker in ("brief", "章节 brief", "旧质检", "旧修订", "作者意图")):
        return "章节说明需要重新整理，系统会按当前作品设定自动修复后再继续。"
    if any(marker in text for marker in ("skeleton", "骨架", "StoryBible", "StoryFoundation", "核心设定源")):
        return "作品设定需要整理，系统会先生成并应用一致的设定草案。"
    if any(marker in text for marker in ("failed", "失败", "structured_output", "JSON", "Expecting")):
        return "后台任务执行失败，系统会优先重试或清理可恢复任务。"
    if any(marker in text for marker in ("running", "pending", "queue", "队列")):
        return "后台已有生成任务，系统会等待或启动队列，不需要重复点击。"
    if any(marker in text for marker in ("evidence", "market", "市场", "证据")):
        return "生产准备资料不足，系统会自动补齐本地证据和市场信号。"
    if any(marker in text for marker in ("quality", "质检", "needs_revision", "修订")):
        return "当前稿需要编辑处理，系统会按质检结果选择局部修、升华修或回到最佳稿。"
    return text if len(text) <= 80 and not _looks_internal(text) else "系统发现继续生产会跑偏，正在选择可自动处理的修复路径。"


def author_next_action_text(value: str) -> str:
    text = author_status_text(value)
    if "不同步" in text or "章节说明" in text or "作品设定" in text:
        return "点击主按钮，让系统自动同步设定和章节说明。"
    if "后台任务" in text:
        return "点击主按钮，让系统处理失败任务或重试。"
    if "生产准备" in text:
        return "点击主按钮，让系统补齐准备项。"
    if "编辑处理" in text:
        return "点击主按钮，让系统继续审稿和修订。"
    return "点击主按钮继续。"


DIMENSION_LABELS = {
    "score": "整体完成度",
    "author_intent": "章节承诺没有落到具体行动和后果里",
    "brief_coverage": "章节说明里的关键承诺没有写足",
    "reader_momentum": "读者继续往下读的推力不够",
    "hook_strength": "章末钩子还不够具体",
    "payoff_grounding": "回报和代价没有充分落地",
    "chapter_necessity": "本章不可替代的变化不够清楚",
    "chapter_unit_flow": "段落之间的目标、阻碍、后果衔接不够顺",
    "readability": "阅读顺滑度还不够",
    "scene_atmosphere": "场景氛围没有真正改变人物判断或行动",
    "dialogue_fullness": "对白承担的信息、试探或情绪变化不够",
    "character_voice": "人物说话和反应的辨识度不够",
    "prose_voice": "句子质感还不够自然",
    "imageable_paragraphs": "画面不够具体，读者看不见场景",
    "paragraph_aesthetic": "段落动作、视角或情绪释放不足",
    "opening_grip": "开篇没有尽快抓住读者",
}


def editorial_blocker_text(value: str) -> str:
    text = str(value or "")
    name = text.split("=", 1)[0].strip()
    return DIMENSION_LABELS.get(name, text if text and not _looks_internal(text) else "读感门槛还有未解决的问题")


def editorial_summary_text(summary: str, blockers: list[str] | None = None) -> str:
    human_blockers = [editorial_blocker_text(item) for item in (blockers or [])]
    human_blockers = list(dict.fromkeys(item for item in human_blockers if item))[:3]
    if human_blockers:
        return "主编判断：" + "；".join(human_blockers) + "。主笔会按这些问题继续修。"
    text = str(summary or "").strip()
    if not text:
        return "主编判断：当前章可以进入下一步。"
    if _looks_internal(text):
        return author_status_text(text)
    return text


def _looks_internal(text: str) -> bool:
    markers = ("#", ":", "_", "Chapter", "Story", "brief", "canon", "quality", "version", "task")
    return any(marker in text for marker in markers)
