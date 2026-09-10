"""番茄发布完整流程封装。C-plus 核心。

流程 (2026-07-08 P56 摸清):
1. 打开发布页 (goto writer)
2. 填章节序号 (input.serial-input:not(.serial-editor-input-hint-area))
3. 填章节标题 (input[placeholder*='标题'])  · 剪掉 "第X章 " 前缀，番茄自动加
4. 填正文 (.ProseMirror 第 0 个)
5. 点 下一步 (button:has-text('下一步'))
6. 错别字弹窗 → 点 提交 (button:has-text('提交'))  · 校验弹窗含"错别字"
7. 检测方式弹窗 → 点 全面检测 (button:has-text('全面检测'))  · 校验弹窗含"内容检测方式"
8. 发布设置弹窗 → 选 是否使用AI = 是 → 点 确认发布 (button:has-text('确认发布'))

错误码:
0  success
1  UI 失效 (selector 找不到 / 元素点不了)
2  CAPTCHA / 需要人工介入
3  登录过期 (跳到 login 页)
4  配额爆 (章节序号重复等业务错误)
5  其他
"""
from __future__ import annotations
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.automation.chrome_cdp import cdp_connection

FANQIE_WRITER_HOME = "https://fanqienovel.com/writer/zone/?enter_from=menu"
CHAPTER_MANAGE_URL_TMPL = "https://fanqienovel.com/main/writer/chapter-manage/{book_id}"
BOOK_PUBLISH_URL_TMPL = "https://fanqienovel.com/main/writer/{book_id}/publish/new?enter_from=newchapter"

# 番茄自动加 "第X章" 前缀，所以我们填标题时要剪掉
CHAPTER_PREFIX_RE = re.compile(r"^第\s*[0-9零一二三四五六七八九十百千]+\s*章\s*[:：\s]*")


def strip_chapter_prefix(title: str) -> str:
    """DB 里 '第1章 这游戏不对劲' → 番茄填 '这游戏不对劲'。"""
    return CHAPTER_PREFIX_RE.sub("", title).strip()


@dataclass
class PublishResult:
    ok: bool
    error_code: int = 0  # 0=success
    error_msg: str = ""
    started_at: str = ""
    finished_at: str = ""
    screenshots: list = field(default_factory=list)
    stage_reached: str = ""  # 走到哪一步失败了
    ai_flag: str = ""        # "yes" / "no"


def _screenshot(page, artifact_dir: Path, stage: str) -> str:
    path = artifact_dir / f"{stage}.png"
    try:
        page.screenshot(path=str(path), full_page=True, timeout=15000)
        return str(path)
    except Exception:
        return ""


def _react_set_input_value(page, selector: str, value: str) -> None:
    """
    React 受控 input 破解：
    keyboard.type 直接改 DOM.value 但不触发 React onChange。
    用 native setter + dispatchEvent('input',bubbles=true) 才能让 React setState。
    """
    js = """
    (args) => {
        const [selector, value] = args;
        const el = document.querySelector(selector);
        if (!el) return {ok: false, err: 'no element'};
        el.focus();
        const proto = el.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
        setter.call(el, value);
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
        return {ok: true, value: el.value};
    }
    """
    page.evaluate(js, [selector, value])
    # blur to close any tooltip
    page.evaluate("(sel) => document.querySelector(sel)?.blur()", selector)


def _find_fanqie_page(browser, book_id: str, artifact_dir: Path):
    """定位/打开 book_id 对应的发布页。

    关键：不直接 goto publish/new URL — 那样页面初始化不全 (下一步按钮永远 disabled)。
    必须先进章节管理页，再点"新建章节"按钮，走完整的 Vue/React 挂载。
    """
    # 复用已开的发布页
    for ctx in browser.contexts:
        for pg in ctx.pages:
            if "fanqienovel.com" in pg.url and "publish" in pg.url and str(book_id) in pg.url:
                return pg

    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    pg = ctx.new_page() if not ctx.pages else ctx.pages[0]

    # 1. 进章节管理页
    manage_url = CHAPTER_MANAGE_URL_TMPL.format(book_id=book_id)
    pg.goto(manage_url, wait_until="domcontentloaded", timeout=30000)
    try:
        pg.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    # 等章节列表 or 新建按钮真正渲染出来
    try:
        pg.locator("button:has-text('新建章节')").first.wait_for(state="visible", timeout=15000)
    except Exception as e:
        print(f"[find] 等新建章节按钮超时: {e}")
    time.sleep(1)

    # 2. 点"新建章节"按钮 (走标准发布流程 · 而非 URL 直达)
    # 注意：番茄的"新建章节"是 <a target="_blank"><button>新建章节</button></a>
    # playwright 的 btn.click() 有时不会真正打开 target=_blank popup ·
    # 用 context.expect_page() 显式等新 tab。
    publish_pg = None
    clicked = False
    for text in ["新建章节", "新章节", "添加章节", "新增章节"]:
        btn = pg.locator(f"button:has-text('{text}')").first
        try:
            if btn.count() > 0:
                btn.scroll_into_view_if_needed(timeout=3000)
                # 关键：expect_page 拿到新 tab
                try:
                    with pg.context.expect_page(timeout=10000) as new_pg_info:
                        btn.click(timeout=5000)
                    publish_pg = new_pg_info.value
                    print(f"[find] 点了 '{text}' · 新 tab: {publish_pg.url[:100]}")
                except Exception as e_pop:
                    # popup 没等到 · 可能同 tab 跳了 · 后面再扫
                    print(f"[find] 点了 '{text}' · 未捕获 popup ({e_pop.__class__.__name__})")
                clicked = True
                break
        except Exception as e:
            print(f"[find] 点 '{text}' 失败: {e}")
            continue
    if not clicked:
        # 找不到按钮，退回直连 (可能会 disabled，但至少不 crash)
        print("[find] 所有按钮文案都没匹配上，退回直连")
        pg.goto(BOOK_PUBLISH_URL_TMPL.format(book_id=book_id),
                wait_until="domcontentloaded", timeout=30000)

    # 等跳转到 /publish/{draft_id}（如果 expect_page 拿到就不用扫）
    if publish_pg is None:
        print("[find] 等发布 tab 出现（可能新 tab）...")
        import re
        pat = re.compile(r"/publish/\d+")
        for i in range(30):  # 最多 15s
            time.sleep(0.5)
            for ctx2 in browser.contexts:
                all_pages = list(ctx2.pages)
                for candidate in all_pages:
                    try:
                        url = candidate.url
                    except Exception:
                        continue
                    if "fanqienovel" in url and pat.search(url):
                        publish_pg = candidate
                        break
                if publish_pg: break
            if publish_pg: break
            if i in (2, 10, 20):
                print(f"[find] round {i} · 当前 pages: {[p.url[:80] for c in browser.contexts for p in c.pages]}")
    if publish_pg is None:
        print(f"[find] 没找到发布 tab，回退到原 pg (url={pg.url})")
    else:
        print(f"[find] 使用发布 tab: {publish_pg.url}")
        publish_pg.bring_to_front()
        pg = publish_pg
    try:
        pg.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    # 等到 .ProseMirror 可见 = 编辑器 mount 完成
    try:
        pg.locator(".ProseMirror").first.wait_for(state="visible", timeout=20000)
        print("[find] ProseMirror ready")
    except Exception as e:
        print(f"[find] ProseMirror 未挂载: {e}")
    # 再等序号输入框可见
    try:
        pg.locator("input.serial-input:not(.serial-editor-input-hint-area)").first.wait_for(state="visible", timeout=10000)
        print("[find] serial-input ready")
    except Exception as e:
        print(f"[find] serial-input 未出现: {e}")
    time.sleep(2)
    return pg


def _detect_login_or_captcha(page) -> Optional[int]:
    """快速检查页面是不是跳到了登录/风控页。返回 error_code 或 None。"""
    url = page.url.lower()
    if "login" in url or "passport" in url or "sso" in url:
        return 3
    body = ""
    try:
        body = page.locator("body").inner_text()[:2000]
    except Exception:
        pass
    if any(kw in body for kw in ["请登录", "登录后查看", "登录 番茄"]):
        return 3
    if any(kw in body for kw in ["安全验证", "拖动滑块", "点击图中", "captcha"]):
        return 2
    return None


def publish_chapter(
    *,
    book_id: str,
    chapter_number: int,
    title: str,
    content: str,
    ai_flag: str = "yes",
    artifact_dir: Path,
    timeout_ms: int = 20000,
) -> PublishResult:
    """
    发一章到番茄。
    ai_flag: "yes"|"no" —— 番茄"是否使用AI"单选
    """
    artifact_dir.mkdir(parents=True, exist_ok=True)
    result = PublishResult(
        ok=False,
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ai_flag=ai_flag,
    )

    filled_title = strip_chapter_prefix(title)
    result.stage_reached = "cdp_connect"

    try:
        with cdp_connection() as (browser, cleanup):
            page = _find_fanqie_page(browser, book_id, artifact_dir)
            time.sleep(2)
            result.screenshots.append(_screenshot(page, artifact_dir, "01-page-opened"))

            # 登录/风控预检
            code = _detect_login_or_captcha(page)
            if code is not None:
                result.error_code = code
                result.error_msg = f"登录过期或 CAPTCHA (url={page.url[:100]})"
                result.stage_reached = "login_check"
                return result

            # === 步骤 1.5: 处理"有刚刚更新的章节，是否继续编辑？"弹窗 ===
            # 上次发章填到一半失败·番茄会把半成品存成草稿·再次进写作页时弹此框拦截操作。
            # 点"放弃"·从干净状态重填(避免残草稿的序号/正文污染当前章)。
            try:
                modal_txt = _current_modal_text(page)
                if "继续编辑" in modal_txt or "刚刚更新" in modal_txt:
                    print(f"[modal] 检测到继续编辑弹窗: {modal_txt!r} · 点『放弃』从干净状态开始")
                    discard_btn = page.locator(".byte-modal-footer button:has-text('放弃')").first
                    discard_btn.wait_for(state="visible", timeout=5000)
                    discard_btn.click(timeout=5000)
                    time.sleep(1.5)
                    result.screenshots.append(_screenshot(page, artifact_dir, "01b-discarded-draft-modal"))
            except Exception as e_modal:
                print(f"[modal] 弹窗处理跳过/失败(可能无弹窗): {type(e_modal).__name__}: {str(e_modal)[:120]}")

            # === 步骤 2: 章节序号 ===
            # 番茄用 React：直接 keyboard.type 改 DOM value 但不触发 setState。
            # 用 native input value setter + dispatchEvent('input') 才能真正触发 React onChange。
            result.stage_reached = "fill_serial"
            serial_input = page.locator("input.serial-input:not(.serial-editor-input-hint-area)").first
            serial_input.wait_for(state="visible", timeout=timeout_ms)
            serial_input.scroll_into_view_if_needed()
            _react_set_input_value(page, "input.serial-input:not(.serial-editor-input-hint-area)", str(chapter_number))

            # === 步骤 3: 章节标题 (剪前缀) ===
            result.stage_reached = "fill_title"
            title_box = page.locator("input[placeholder*='标题']").first
            title_box.wait_for(state="visible", timeout=timeout_ms)
            title_box.scroll_into_view_if_needed()
            _react_set_input_value(page, "input[placeholder*='标题']", filled_title)

            # === 步骤 4: 正文 (第一个 .ProseMirror) ===
            result.stage_reached = "fill_body"
            editor = page.locator(".ProseMirror").first
            editor.wait_for(state="visible", timeout=timeout_ms)
            editor.click()
            page.keyboard.press("Control+A")
            page.keyboard.press("Delete")
            # 逐段插入
            paragraphs = content.split("\n")
            for i, para in enumerate(paragraphs):
                if para.strip():
                    page.keyboard.insert_text(para)
                if i < len(paragraphs) - 1:
                    page.keyboard.press("Enter")
            time.sleep(2)  # 等 React onChange 传播
            result.screenshots.append(_screenshot(page, artifact_dir, "02-filled"))

            # 校验：序号+标题+正文都进去了吗
            serial_val = serial_input.input_value()
            title_val = title_box.input_value()
            editor_text = editor.inner_text()[:80]
            print(f"[DEBUG] serial={serial_val!r} title={title_val!r} editor_head={editor_text!r}")
            if not serial_val or not title_val:
                result.error_code = 1
                result.error_msg = f"序号或标题未填入: serial={serial_val!r} title={title_val!r}"
                return result

            # === 步骤 5: 点下一步 ===
            result.stage_reached = "click_next"
            next_btn = page.locator("button:has-text('下一步')").first
            btn_state = next_btn.evaluate("""el => ({
                disabled: el.disabled,
                ariaDisabled: el.getAttribute('aria-disabled'),
                className: el.className || '',
                text: (el.innerText||'').trim(),
            })""")
            print(f"[DEBUG] next_btn state: {btn_state}")
            # arco 有时用 aria-disabled 或 className 里含 'disabled'
            is_disabled = (
                btn_state.get("disabled") is True
                or btn_state.get("ariaDisabled") == "true"
                or "disabled" in (btn_state.get("className") or "").lower()
            )
            if is_disabled:
                result.error_code = 4
                result.error_msg = f"下一步按钮 disabled: {btn_state}"
                return result
            next_btn.click()
            time.sleep(2.5)
            result.screenshots.append(_screenshot(page, artifact_dir, "03-after-next"))

            # === 步骤 6: 错别字弹窗 → 提交 (有时没这一步，直接跳到检测) ===
            result.stage_reached = "typo_confirm"
            modal_text = _current_modal_text(page)
            if "错别字" in modal_text and "确定提交" in modal_text:
                page.locator(".arco-modal button:has-text('提交')").first.click()
                time.sleep(2)
                result.screenshots.append(_screenshot(page, artifact_dir, "04-after-typo-submit"))

            # === 步骤 7: 内容检测方式 → 仅基础检测 ===
            # 番茄给 2 个选项：全面检测（每章限 2 次）· 仅基础检测（无限）
            # 用基础检测足够 · 避免耗尽全面检测次数
            result.stage_reached = "content_check"
            modal_text = _current_modal_text(page)
            if "内容检测方式" in modal_text:
                page.locator(".arco-modal button:has-text('仅基础检测')").first.click()
                # 检测要跑几秒 · 等发布设置弹窗出现（最多 60s）
                print("[flow] 基础检测中 · 等发布设置弹窗...")
                deadline = time.time() + 60
                got_settings = False
                while time.time() < deadline:
                    t = _current_modal_text(page)
                    if "发布设置" in t or "是否使用AI" in t or "使用 AI" in t:
                        print("[flow] 发布设置弹窗出现")
                        got_settings = True
                        break
                    time.sleep(1)
                result.screenshots.append(_screenshot(page, artifact_dir, "05-after-check"))
                # P1-7 · wait 超时明确回填 · 不让下面的 stage 覆盖真实失败点
                if not got_settings:
                    result.stage_reached = "content_check:wait_settings_timeout_90s"
                    result.error_code = 1
                    result.error_msg = "全面检测 90s 后未出现发布设置弹窗（可能被内容检测拦截或番茄卡了）"
                    return result
            else:
                # 没检测弹窗？可能直接到发布设置了，继续
                pass

            # === 步骤 8: 发布设置 → 选 AI + 确认发布 ===
            result.stage_reached = "publish_settings"
            modal_text = _current_modal_text(page)
            if "发布设置" not in modal_text and "是否使用AI" not in modal_text:
                result.error_code = 1
                result.error_msg = f"预期'发布设置'弹窗未出现，实际弹窗: {modal_text[:200]}"
                return result

            # 选 AI radio：番茄用 arco-radio · 页面里只有 .arco-modal (无 .arco-modal-body)
            # 必须点里面的 input[type=radio] · 用 native .click() 触发 React onChange
            ai_target = "是" if ai_flag == "yes" else "否"

            radio_info = page.evaluate(f"""() => {{
                const modals = [...document.querySelectorAll('.arco-modal')].filter(m=>m.offsetParent!==null);
                if (!modals[0]) return null;
                const radios = [...modals[0].querySelectorAll('input[type=radio]')];
                const target = radios.find(r => (r.closest('label')?.innerText||'').includes('{ai_target}'));
                if (!target) return null;
                target.click();
                return {{clicked: true, checked_after: target.checked, total: radios.length}};
            }}""")
            if not radio_info:
                result.error_code = 1
                result.error_msg = f"找不到 AI radio '{ai_target}' (modal 里无 radio input)"
                return result
            print(f"[flow] AI radio '{ai_target}' clicked · checked={radio_info.get('checked_after')} · total={radio_info.get('total')}")
            time.sleep(0.8)

            confirmed = page.evaluate(f"""() => {{
                const modals = [...document.querySelectorAll('.arco-modal')].filter(m=>m.offsetParent!==null);
                if (!modals[0]) return false;
                const radios = [...modals[0].querySelectorAll('input[type=radio]')];
                const t = radios.find(r => (r.closest('label')?.innerText||'').includes('{ai_target}'));
                return t ? t.checked : false;
            }}""")
            if not confirmed:
                # fallback: 真鼠标点 label
                try:
                    label_box = page.locator(f".arco-modal label:has-text('{ai_target}')").first.bounding_box()
                    if label_box:
                        page.mouse.click(label_box['x'] + label_box['width']/2, label_box['y'] + label_box['height']/2)
                        time.sleep(0.8)
                        print(f"[flow] AI radio fallback mouse.click at {label_box}")
                except Exception as e:
                    print(f"[flow] AI radio fallback failed: {e}")

            result.screenshots.append(_screenshot(page, artifact_dir, "06-ai-selected"))

            # === 挂番茄 publish_article API 响应拦截器 ===
            # 番茄发布章节调用 /api/author/publish_article/v0/ · 响应体 JSON 里
            # code=0 成功 · code=-1020 "更新作品数超出每日上限" · 其它 code=业务错误
            # 之前所有失败都是 UI 静默不 toast · 但 API 响应有明确 error · 必须拦截
            _publish_api_result: dict = {"captured": False, "code": None, "message": ""}

            def _on_response(resp):
                try:
                    if "publish_article" in resp.url and resp.request.method == "POST":
                        body = resp.text()
                        import json as _json
                        j = _json.loads(body)
                        _publish_api_result["captured"] = True
                        _publish_api_result["code"] = j.get("code")
                        _publish_api_result["message"] = j.get("message", "")
                        print(f"[publish_api] code={j.get('code')} msg={j.get('message')!r}")
                except Exception as _e:
                    print(f"[publish_api] parse err: {_e}")

            page.on("response", _on_response)

            # 点确认发布（arco btn 的 playwright click 可能不触发 React handler ·
            # fallback 到 native dispatchEvent · 再等 URL 跳转或 modal 消失）
            result.stage_reached = "click_confirm_publish"
            confirm_btn = page.locator(".arco-modal button:has-text('确认发布')").first
            if confirm_btn.evaluate("el => el.disabled"):
                result.error_code = 1
                result.error_msg = "确认发布按钮 disabled"
                return result
            # 先试标准 click
            try:
                confirm_btn.click(timeout=3000)
            except Exception:
                pass
            time.sleep(1.5)
            # 检查 modal 还在没：还在就用 native dispatchEvent 补一发
            still_modal = page.evaluate("""() => {
                const m = document.querySelector('.arco-modal .arco-modal-body');
                return m ? (m.innerText || '').includes('发布设置') : false;
            }""")
            if still_modal:
                print("[flow] click 未触发 · 用 native dispatchEvent")
                page.evaluate("""() => {
                    const btns = [...document.querySelectorAll('.arco-modal button')];
                    const t = btns.find(b => (b.innerText||'').includes('确认发布'));
                    if (t) {
                        t.dispatchEvent(new MouseEvent('mousedown', {bubbles:true,cancelable:true}));
                        t.dispatchEvent(new MouseEvent('mouseup', {bubbles:true,cancelable:true}));
                        t.dispatchEvent(new MouseEvent('click', {bubbles:true,cancelable:true}));
                    }
                }""")
            time.sleep(4)
            result.screenshots.append(_screenshot(page, artifact_dir, "07-after-confirm"))

            # === 检查 publish_article API 响应 · 优先业务级错误 ===
            # 如果拦截到 code≠0 · 直接失败 · 别再走 verify（verify 靠列表匹配 · 不准）
            if _publish_api_result["captured"]:
                api_code = _publish_api_result["code"]
                api_msg = _publish_api_result["message"]
                if api_code == 0:
                    print(f"[publish_api] ✅ 服务端接收成功 code=0")
                    result.ok = True
                    result.stage_reached = "done:api:code=0"
                    return result
                elif api_code == -1020:
                    # 每日作品数超上限 · 特殊标记 · 上层可 sleep 到明天再试
                    result.error_code = 1020
                    result.error_msg = f"番茄 API 拒绝: code=-1020 {api_msg}（每日作品更新上限）"
                    result.stage_reached = "publish_api:daily_book_limit"
                    print(f"[publish_api] ❌ {result.error_msg}")
                    return result
                elif api_code == -1019:
                    # 每日提交字数超上限 · 章级限流 · 上层可 sleep 到明天再试
                    result.error_code = 1019
                    result.error_msg = f"番茄 API 拒绝: code=-1019 {api_msg}（每日字数上限）"
                    result.stage_reached = "publish_api:daily_word_limit"
                    print(f"[publish_api] ❌ {result.error_msg}")
                    return result
                else:
                    result.error_code = 1
                    result.error_msg = f"番茄 API 拒绝: code={api_code} {api_msg}"
                    result.stage_reached = f"publish_api:code={api_code}"
                    print(f"[publish_api] ❌ {result.error_msg}")
                    return result

            # === 验证：主路 = list_published_chapters API 按序号匹配 ===
            # P1-1 · 从"body substring 主路 · API fallback"翻转为"API 主路 · body substring fallback"
            #        API 用番茄章节管理页的结构化解析 · 比 body 关键词更可靠
            result.stage_reached = "post_publish_verify"
            time.sleep(3)
            final_url = page.url
            verified = False
            verify_via = "unknown"

            # 主路：拉章节列表 · 按序号匹配（权威）· 复用当前 browser 避免嵌套
            # 只认 type=1 已发布 · 不看审核中/草稿
            # 原因：番茄网页作家后台的 type=2"审核中" tab 显示的其实是自动化留下的草稿
            #       APP 里"审核中" == 未提交成功。只有 type=1"已发布" tab 才是真正入番茄的章节
            try:
                pubs = _list_chapters_on_browser(browser, book_id, include_review=False)
                for pc in pubs:
                    if int(pc.get("number") or 0) == int(serial_val):
                        title = str(pc.get("title", ""))
                        status = str(pc.get("status", ""))
                        # 只认"已发布"状态 · 其他都是假阳
                        if status == "已发布":
                            verified = True
                            verify_via = f"api:serial_match:{status}"
                        break
            except Exception as e:
                print(f"[flow] list_published_chapters 主路失败: {e} · 走 body fallback")

            # 兜底：body substring + URL（降级路径）· 只认"已发布"关键字
            if not verified:
                body_text = page.locator("body").inner_text()[:500]
                if "已发布" in body_text:
                    verified = True
                    verify_via = "body_fallback:已发布"
            if verified:
                result.ok = True
                result.stage_reached = f"done:{verify_via}"
            else:
                extra_modal = _current_modal_text(page)
                result.error_code = 5
                result.error_msg = f"发布后未验证到章节 (url={final_url[:100]}) modal={extra_modal[:150]} verify_via={verify_via}"

    except Exception as e:
        result.error_code = 5
        result.error_msg = f"{type(e).__name__}: {e}"
    finally:
        result.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    return result


def _current_modal_text(page) -> str:
    """当前可见的 .arco-modal 全文；没弹窗返回 ''。"""
    modals = page.locator(".arco-modal")
    for i in range(modals.count()):
        m = modals.nth(i)
        if m.is_visible():
            try:
                return m.inner_text()
            except Exception:
                return ""
    return ""


def _wait_for_modal_change(page, prev_marker: str, timeout: float = 15.0):
    """等当前弹窗（含 prev_marker）消失或变内容。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        text = _current_modal_text(page)
        if prev_marker not in text:
            return
        time.sleep(0.5)


def _list_chapters_on_browser(browser, book_id: str, include_review: bool = True) -> list[dict]:
    """内部版：复用已连接的 browser · 避免嵌套 sync_playwright。

    include_review=True 时同时拉 type=1(已发布) + type=2(审核中)。
    这是关键 · 因为新发章刚提交时在 type=2 · 老代码只看 type=1 导致 verify 永远失败。
    """
    result: list[dict] = []
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    page = ctx.new_page() if not ctx.pages else ctx.pages[0]

    tabs_to_query = [("1", "已发布")]
    if include_review:
        tabs_to_query.append(("2", "审核中"))

    for type_id, tab_name in tabs_to_query:
        url = f"{CHAPTER_MANAGE_URL_TMPL.format(book_id=book_id)}?type={type_id}"
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            time.sleep(3)
            if _detect_login_or_captcha(page):
                continue
            body = page.locator("body").inner_text()
            current = None
            for line in body.split("\n"):
                line = line.strip()
                if not line:
                    if current and current.get("title"):
                        # type=2 tab 里没有 status 文字 · 默认给它标"审核中"
                        if "status" not in current:
                            current["status"] = tab_name
                        result.append(current)
                    current = None
                    continue
                m = re.match(r"^第(\d+)章\s+(.+)$", line)
                if m:
                    current = {"number": int(m.group(1)), "title": line}
                    continue
                if current is not None:
                    if line in ("审核中", "已发布", "审核不通过", "下架", "草稿"):
                        current["status"] = line
                    elif re.match(r"^\d{4}-\d{2}-\d{2}", line):
                        current["published_at"] = line
            # tab 末尾如果还有未 append 的 current · 收尾
            if current and current.get("title"):
                if "status" not in current:
                    current["status"] = tab_name
                result.append(current)
        except Exception:
            pass
    return result


def list_published_chapters(book_id: str) -> list[dict]:
    """守夜用：拉番茄章节管理页的所有章节。返回 [{number, title, status, published_at}]。"""
    with cdp_connection() as (browser, cleanup):
        try:
            return _list_chapters_on_browser(browser, book_id)
        finally:
            cleanup()


def unpublish_chapter(book_id: str, chapter_number: int, artifact_dir: Path) -> PublishResult:
    """下架章节（点 chapter-manage 页对应行的删除按钮 → 确认弹窗）。冷静期内可用。"""
    result = PublishResult(
        ok=False,
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)

    try:
        with cdp_connection() as (browser, cleanup):
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            manage_url = CHAPTER_MANAGE_URL_TMPL.format(book_id=book_id)
            page.goto(manage_url, wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            time.sleep(2)
            result.screenshots.append(_screenshot(page, artifact_dir, "u01-manage-page"))

            target_marker = f"第{chapter_number}章"
            deleted = page.evaluate("""(marker) => {
                const rows = [...document.querySelectorAll('tr.arco-table-tr')];
                for (const r of rows) {
                    const titleEl = r.querySelector('.table-title');
                    const t = (titleEl?.innerText || '').trim();
                    if (t.startsWith(marker + ' ') || t === marker || t.startsWith(marker + '第')) {
                        const del = r.querySelector('.icon-delete, .auto-editor-chapter-delete');
                        if (del) { del.click(); return 'clicked'; }
                        return 'row_found_no_del_btn';
                    }
                }
                return 'row_not_found';
            }""", target_marker)
            print(f"[unpublish] evaluate result: {deleted}")
            if deleted != "clicked":
                result.error_code = 5
                result.error_msg = f"未找到 {target_marker} 的删除按钮: {deleted}"
                return result
            time.sleep(1.5)
            result.screenshots.append(_screenshot(page, artifact_dir, "u02-delete-modal"))

            confirmed = False
            for txt in ["确定", "确认", "确定删除", "删除"]:
                try:
                    btn = page.locator(f".arco-modal button:has-text('{txt}')").first
                    if btn.count() > 0 and btn.is_visible():
                        btn.click()
                        confirmed = True
                        print(f"[unpublish] 点了确认按钮 '{txt}'")
                        break
                except Exception:
                    continue
            if not confirmed:
                result.error_code = 5
                result.error_msg = "确认弹窗未找到 确定/删除 按钮"
                return result
            time.sleep(2)
            result.screenshots.append(_screenshot(page, artifact_dir, "u03-after-confirm"))

            # 验证：刷新一次，看是否消失
            page.reload(wait_until="domcontentloaded", timeout=20000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            time.sleep(2)
            still_there = page.evaluate("""(marker) => {
                const rows = [...document.querySelectorAll('tr.arco-table-tr')];
                for (const r of rows) {
                    const titleEl = r.querySelector('.table-title');
                    const t = (titleEl?.innerText || '').trim();
                    if (t.startsWith(marker + ' ') || t === marker || t.startsWith(marker + '第')) return true;
                }
                return false;
            }""", target_marker)
            if still_there:
                result.error_code = 1
                result.error_msg = f"确认后 {target_marker} 仍在列表 · 下架未生效"
                return result

            result.ok = True
            result.error_code = 0
            result.stage_reached = "done"
            page.close()
    except Exception as e:
        result.error_code = 5
        result.error_msg = f"{type(e).__name__}: {e}"

    result.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return result


def _open_manage_and_find_edit_icon(page, book_id: str, chapter_number: int, timeout_ms: int = 30000):
    """进章节管理页，按章号文本精确定位编辑图标并点击进入编辑页。

    返回 (ok: bool, err: str)。
    定位策略：遍历 span.icon-edit.tomato-edit，向上找含"第N章"的祖先行，
    精确匹配 chapter_number（防跳章/错位）。每页仅 15 章，超出需滚动加载。
    """
    manage_url = f"https://fanqienovel.com/main/writer/chapter-manage/{book_id}?type=1"
    page.goto(manage_url, wait_until="domcontentloaded", timeout=timeout_ms)
    time.sleep(5)

    def _try_click_on_current_page():
        return page.evaluate(
            """(target) => {
                const icons=[...document.querySelectorAll('span.icon-edit.tomato-edit')];
                for(const ic of icons){
                    let el=ic, chn=null;
                    for(let i=0;i<8 && el;i++){
                        el=el.parentElement;
                        if(el){
                            const m=(el.innerText||'').match(/第(\\d+)章/);
                            if(m){chn=parseInt(m[1],10); break;}
                        }
                    }
                    if(chn===target){
                        ic.scrollIntoView({block:'center'});
                        ic.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true}));
                        return true;
                    }
                }
                return false;
            }""",
            chapter_number,
        )

    def _chapters_on_page():
        return page.evaluate(
            """() => {
                const icons=[...document.querySelectorAll('span.icon-edit.tomato-edit')];
                const chs=[];
                icons.forEach(ic=>{let el=ic;for(let i=0;i<8&&el;i++){el=el.parentElement;if(el){const m=(el.innerText||'').match(/第(\\d+)章/);if(m){chs.push(parseInt(m[1]));break;}}}});
                return chs;
            }"""
        )

    # 章节列表是分页的（每页15章，倒序）。遍历所有页找目标章。
    # 番茄用 arco 分页：点 .arco-pagination-item 或"下一页"箭头。
    seen_pages = set()
    for _page_i in range(12):  # 最多 12 页（够 180 章）
        chs = _chapters_on_page()
        sig = tuple(chs)
        if chapter_number in chs:
            if _try_click_on_current_page():
                return True, ""
            return False, f"第{chapter_number}章在当前页但点击失败"
        if sig in seen_pages:
            # 翻页没变化 → 到底了
            break
        seen_pages.add(sig)
        # 翻下一页：点分页的"下一页"按钮
        went = page.evaluate(
            """() => {
                // arco 下一页箭头
                const next = document.querySelector('.arco-pagination-item-next, [class*=pagination] [class*=next]');
                if(next && !next.className.includes('disabled')){
                    next.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true}));
                    return 'next_arrow';
                }
                // fallback：找当前高亮页的下一个数字页
                const items=[...document.querySelectorAll('.arco-pagination-item, [class*=pagination-item]')].filter(e=>/^\\d+$/.test((e.innerText||'').trim()));
                const active=items.find(e=>e.className.includes('active'));
                if(active){
                    const cur=parseInt(active.innerText);
                    const nxt=items.find(e=>parseInt(e.innerText)===cur+1);
                    if(nxt){nxt.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true}));return 'num_'+(cur+1);}
                }
                return null;
            }"""
        )
        if not went:
            break
        time.sleep(3)

    return False, f"章节管理页遍历所有分页未找到第{chapter_number}章（已翻页{len(seen_pages)}页）"


def edit_published_chapter(
    *,
    book_id: str,
    chapter_number: int,
    title: str,
    content: str,
    artifact_dir: Path,
    timeout_ms: int = 20000,
    update_title: bool = True,
) -> PublishResult:
    """编辑一章【已发布】的章节，用新正文替换旧正文。

    与 publish_chapter 的差异（2026-07-23 实地勘察）：
    - 入口：章节管理页点 span.icon-edit.tomato-edit（按章号匹配），进 ?enter_from=modifychapter
    - 末尾流程简化：点"下一步" → "发布提示:错别字未修改,是否确定提交?" → 点"提交"
    - 无"内容检测方式"、无"发布设置/是否使用AI"、无"确认发布"——沿用原发布设置

    安全设计：进编辑页后校验 serial-input value == chapter_number（双保险防错位），
    不符立即中止，绝不在错误章节上覆写。
    """
    artifact_dir.mkdir(parents=True, exist_ok=True)
    result = PublishResult(
        ok=False,
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ai_flag="edit",
    )
    filled_title = strip_chapter_prefix(title)
    result.stage_reached = "cdp_connect"

    try:
        with cdp_connection() as (browser, cleanup):
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page() if not ctx.pages else ctx.pages[0]

            # 登录/风控预检（先进管理页）
            result.stage_reached = "open_manage"
            ok, err = _open_manage_and_find_edit_icon(page, book_id, chapter_number, timeout_ms=30000)
            code = _detect_login_or_captcha(page)
            if code is not None:
                result.error_code = code
                result.error_msg = f"登录过期或 CAPTCHA (url={page.url[:100]})"
                result.stage_reached = "login_check"
                return result
            if not ok:
                result.error_code = 1
                result.error_msg = err
                result.stage_reached = "find_edit_icon"
                return result

            time.sleep(6)  # 等编辑页加载

            # === 处理"有刚刚更新的章节，是否继续编辑？"弹窗 ===
            # 点"放弃"→ 加载线上已发布正式版（干净起点），而非未提交草稿。
            # 我们要用 DB 新正文完全替换，不需要任何旧草稿。
            try:
                dialog = page.evaluate(
                    """() => {
                        const modals=[...document.querySelectorAll('.byte-modal, .arco-modal')].filter(m=>m.offsetParent!==null);
                        for(const m of modals){
                            if((m.innerText||'').includes('是否继续编辑')){
                                const btns=[...m.querySelectorAll('button')];
                                const giveup=btns.find(b=>(b.innerText||'').trim()==='放弃');
                                if(giveup){giveup.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true})); return 'clicked_giveup';}
                            }
                        }
                        return 'no_dialog';
                    }"""
                )
                if dialog == "clicked_giveup":
                    print("[edit] 检测到'继续编辑'弹窗 → 点'放弃'（用线上正式版）")
                    time.sleep(3)
            except Exception as _e:
                print(f"[edit] 处理继续编辑弹窗异常(忽略): {_e}")

            result.screenshots.append(_screenshot(page, artifact_dir, "01-edit-page"))

            # === 校验：确实进了正确章节的编辑页（防错位）===
            result.stage_reached = "verify_chapter"
            if "modifychapter" not in page.url:
                result.error_code = 1
                result.error_msg = f"未进入编辑页 (url={page.url[:100]})"
                return result
            serial_input = page.locator("input.serial-input:not(.serial-editor-input-hint-area)").first
            serial_input.wait_for(state="visible", timeout=timeout_ms)
            serial_val = serial_input.input_value().strip()
            if serial_val != str(chapter_number):
                result.error_code = 1
                result.error_msg = f"❌ 章号校验失败！编辑页序号={serial_val!r} 目标={chapter_number}，中止防错位"
                result.stage_reached = "verify_chapter:mismatch"
                return result
            print(f"[edit] ✅ 章号校验通过 serial={serial_val}")

            # === 改标题（可选）===
            if update_title:
                result.stage_reached = "fill_title"
                title_box = page.locator("input[placeholder*='标题']").first
                title_box.wait_for(state="visible", timeout=timeout_ms)
                _react_set_input_value(page, "input[placeholder*='标题']", filled_title)

            # === 清空正文 + 重填 ===
            result.stage_reached = "fill_body"
            editor = page.locator(".ProseMirror").first
            editor.wait_for(state="visible", timeout=timeout_ms)
            editor.click()
            page.keyboard.press("Control+A")
            page.keyboard.press("Delete")
            time.sleep(0.5)
            # 关键：按"非空段落"切分，段落间只按一次 Enter。
            # DB 正文用 \n\n 分段；若逐个 \n 都按 Enter，ProseMirror 会生成空段落 → 多余空行。
            paras = [p.strip() for p in re.split(r"\n+", content) if p.strip()]
            for i, para in enumerate(paras):
                page.keyboard.insert_text(para)
                if i < len(paras) - 1:
                    page.keyboard.press("Enter")
            time.sleep(2)
            result.screenshots.append(_screenshot(page, artifact_dir, "02-filled"))

            # 校验正文进去了
            editor_text = editor.inner_text()
            if len(editor_text.strip()) < 100:
                result.error_code = 1
                result.error_msg = f"正文填充异常，编辑器内容过短: {len(editor_text)}字"
                return result

            # === 挂 API 响应拦截器（编辑走 publish_article 或 modify 端点）===
            _api_result: dict = {"captured": False, "code": None, "message": ""}

            def _on_response(resp):
                try:
                    u = resp.url
                    if resp.request.method == "POST" and "fanqienovel" in u and any(
                        k in u for k in ["publish_article", "modify", "update_chapter", "save_article", "article/publish", "edit_article"]
                    ):
                        j = json.loads(resp.text())
                        # 只记录带业务 code 的（过滤掉 get_speak_popup 之类无关的）
                        if "code" in j and ("article" in u or "chapter" in u or "publish" in u):
                            _api_result["captured"] = True
                            _api_result["code"] = j.get("code")
                            _api_result["message"] = j.get("message", "")
                            _api_result["url"] = u.split("?")[0][-50:]
                            print(f"[edit_api] url={u.split('?')[0][-50:]} code={j.get('code')} msg={j.get('message')!r}")
                except Exception as _e:
                    print(f"[edit_api] parse err: {_e}")

            page.on("response", _on_response)

            # === 点"下一步" ===
            result.stage_reached = "click_next"
            next_btn = page.locator("button:has-text('下一步')").first
            next_btn.wait_for(state="visible", timeout=timeout_ms)
            next_btn.click()
            time.sleep(3)
            result.screenshots.append(_screenshot(page, artifact_dir, "03-after-next"))

            # === 下一步后可能出现两种弹窗（每章不定）===
            #  A. "发布提示:检测到错别字未修改,是否确定提交?" → 点"提交" → 再出发布设置
            #  B. 直接出"发布设置"弹窗（本章无错别字提示）→ 跳过提交，直接确认发布
            # 关键坑（2026-07-23 ch2 暴露）：错别字弹窗不是每章都出，不能强制找"提交"。
            result.stage_reached = "submit_confirm"
            modal_text = _current_modal_text(page)
            print(f"[edit] 下一步后弹窗: {modal_text[:120]}")
            if "错别字" in modal_text or ("是否确定提交" in modal_text):
                # A：错别字确认弹窗 → 点"提交"
                submit_btn = page.locator(".arco-modal button:has-text('提交'), .byte-modal button:has-text('提交'), button:has-text('提交')").first
                if submit_btn.count() > 0:
                    try:
                        submit_btn.click(timeout=3000)
                    except Exception:
                        page.evaluate("""() => {
                            const b=[...document.querySelectorAll('button')].find(x=>(x.innerText||'').trim()==='提交');
                            if(b){b.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true}));}
                        }""")
                    time.sleep(4)
                    print("[edit] 已点'提交'(错别字弹窗)")
                else:
                    print("[edit] 错别字弹窗但未找到'提交'按钮，尝试直接进发布设置")
            else:
                print("[edit] 无错别字弹窗，直接进发布设置")
            result.screenshots.append(_screenshot(page, artifact_dir, "04-after-submit"))

            # === "发布设置"弹窗：选 是否使用AI = 是 → 确认发布 ===
            # 实测（2026-07-23）：编辑已发布章节点"提交"后，仍会出现发布设置弹窗，
            # 和发布新章一样需要选 AI + 确认发布，才是真正的提交。
            result.stage_reached = "publish_settings"
            modal_text = _current_modal_text(page)
            print(f"[edit] 提交后弹窗: {modal_text[:120]}")
            if "发布设置" in modal_text or "是否使用AI" in modal_text:
                # 选 AI = 是
                page.evaluate("""() => {
                    const modals=[...document.querySelectorAll('.arco-modal,.byte-modal')].filter(m=>m.offsetParent!==null);
                    for(const m of modals){
                        if((m.innerText||'').includes('是否使用AI')){
                            const radios=[...m.querySelectorAll('input[type=radio]')];
                            const yes=radios.find(r=>(r.closest('label')?.innerText||'').includes('是'));
                            if(yes){yes.click();}
                            return;
                        }
                    }
                }""")
                time.sleep(1)
                # 点确认发布
                confirmed = page.evaluate("""() => {
                    const modals=[...document.querySelectorAll('.arco-modal,.byte-modal')].filter(m=>m.offsetParent!==null);
                    for(const m of modals){
                        const b=[...m.querySelectorAll('button')].find(x=>(x.innerText||'').trim()==='确认发布');
                        if(b){
                            b.dispatchEvent(new MouseEvent('mousedown',{bubbles:true}));
                            b.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));
                            b.dispatchEvent(new MouseEvent('click',{bubbles:true}));
                            return true;
                        }
                    }
                    return false;
                }""")
                print(f"[edit] 点确认发布: {confirmed}")
                time.sleep(5)
                result.screenshots.append(_screenshot(page, artifact_dir, "05-after-confirm"))

            # === 验证结果 ===
            if _api_result["captured"]:
                api_code = _api_result["code"]
                api_msg = _api_result["message"]
                if api_code == 0:
                    print(f"[edit_api] ✅ 服务端接收成功 code=0")
                    result.ok = True
                    result.stage_reached = "done:api:code=0"
                    return result
                else:
                    result.error_code = 1
                    result.error_msg = f"番茄 API 拒绝编辑: code={api_code} {api_msg}"
                    result.stage_reached = f"edit_api:code={api_code}"
                    return result

            # API 没拦到 → 看是否跳回章节管理页（编辑成功的信号）
            time.sleep(3)
            if "chapter-manage" in page.url or "modifychapter" not in page.url:
                print(f"[edit] 已跳离编辑页 → 推定提交成功 (url={page.url[:80]})")
                result.ok = True
                result.stage_reached = "done:url_left_editor"
                return result

            result.error_code = 1
            result.error_msg = f"提交后状态未知：API未拦截且仍在编辑页 (url={page.url[:100]})"
            result.stage_reached = "submit:unknown"
            return result

    except Exception as e:
        result.error_code = 5
        result.error_msg = f"{type(e).__name__}: {e}"

    result.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return result
