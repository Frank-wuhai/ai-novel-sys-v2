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


def _looks_internal(text: str) -> bool:
    markers = ("#", ":", "_", "Chapter", "Story", "brief", "canon", "quality", "version", "task")
    return any(marker in text for marker in markers)
