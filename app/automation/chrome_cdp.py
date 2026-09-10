"""番茄 CDP 连接封装：优先连已开 Chrome (9222)，没开就 headless 自启。

做法 1 + 做法 3 兜底 (2026-07-08 P56):
- 主路径: 用户点桌面"番茄发布 Chrome (CDP)" → 已开 → 直接连
- 兜底: 9222 无响应 → subprocess 启 headless Chrome (共享 .fanqie-profile) → 用完关

使用:
    from app.automation.chrome_cdp import cdp_connection
    with cdp_connection() as (page, cleanup):
        ...  # page 是番茄发布页
    # 退出时: 已开 Chrome 不会被杀；headless 子进程会被 cleanup 杀掉
"""
from __future__ import annotations
import contextlib
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Callable, Optional

CDP_PORT = 9222
CDP_URL = f"http://localhost:{CDP_PORT}"
USER_DATA_DIR = "/home/frank/.fanqie-profile"
CHROME_BINARY = "/opt/google/chrome/chrome"
FANQIE_WRITER_URL = "https://fanqienovel.com/writer/zone/?enter_from=menu"


def cdp_alive(port: int = CDP_PORT, timeout: float = 2.0) -> bool:
    """快速检查 CDP 端口是否响应。禁用代理（127.0.0.1 不该走 socks/http proxy）。"""
    try:
        # 不走系统代理 —— HTTPS_PROXY=socks5 会让 urllib 报 'unknown url type'
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(f"http://127.0.0.1:{port}/json/version")
        with opener.open(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def start_headless_chrome(port: int = CDP_PORT, wait_ready: float = 15.0, headless: bool = False) -> subprocess.Popen:
    """启动 Chrome，共享 .fanqie-profile 保留登录态。

    默认 headless=False：起 GUI Chrome（在 Wayland/X11 session 里可见），你能看到脚本操作。
    headless=True：无头模式，用于服务器/cron 场景（可能触发番茄反爬检测）。
    """
    if not Path(CHROME_BINARY).exists():
        raise RuntimeError(f"Chrome 不在 {CHROME_BINARY}")
    if not Path(USER_DATA_DIR).exists():
        raise RuntimeError(
            f"user-data-dir {USER_DATA_DIR} 不存在 · "
            f"请先手动开一次番茄 Chrome（桌面快捷方式）登录一次"
        )
    cmd = [
        CHROME_BINARY,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={USER_DATA_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        # 屏蔽番茄唤起"作家助手"客户端的 xdg-open 弹窗（会挡住 CDP）
        "--disable-features=ExternalProtocolDialog,IntentPicker",
        "--window-size=1600,1000",
    ]
    if headless:
        cmd += ["--headless=new", "--no-sandbox", "--disable-gpu"]
    cmd.append(FANQIE_WRITER_URL)

    # 继承 DISPLAY / WAYLAND_DISPLAY / XDG_RUNTIME_DIR 以支持 GUI
    env = os.environ.copy()
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )
    # 等 CDP ready
    deadline = time.time() + wait_ready
    while time.time() < deadline:
        if cdp_alive(port, timeout=1.0):
            return proc
        if proc.poll() is not None:
            raise RuntimeError(f"Chrome 启动后立即退出 (code={proc.returncode})")
        time.sleep(0.5)
    proc.kill()
    raise TimeoutError(f"Chrome {wait_ready}s 内未响应 CDP")


@contextlib.contextmanager
def cdp_connection():
    """
    yield (playwright_browser, cleanup_fn)。cleanup_fn 只杀我们自己启的 headless。
    调用者用 browser 找番茄页 / 用 playwright API 操作。
    """
    from playwright.sync_api import sync_playwright

    self_started_proc: Optional[subprocess.Popen] = None
    if not cdp_alive():
        # 先看 systemd 是否有 fanqie-chrome-cdp 在跑 · 如果有 · 不能重复 start
        # （同 user-data-dir 会立刻 crash · 之前 daily_dispatch 就栽在这）
        _systemd_active = False
        try:
            _r = subprocess.run(
                ["systemctl", "--user", "is-active", "fanqie-chrome-cdp.service"],
                capture_output=True, text=True, timeout=3,
            )
            _systemd_active = (_r.stdout.strip() == "active")
        except Exception:
            pass
        if _systemd_active:
            # 服务活着但 cdp_alive False = 网络/代理临时抽风 · 重试一次
            time.sleep(1.0)
            if not cdp_alive(timeout=5.0):
                raise RuntimeError(
                    "fanqie-chrome-cdp.service active 但 CDP 端口无响应 · "
                    "拒绝 auto-start 避免 user-data-dir 冲突 · "
                    "请手工 systemctl --user restart fanqie-chrome-cdp"
                )
        else:
            self_started_proc = start_headless_chrome()

    pw = sync_playwright().start()
    try:
        # P1 · 绕过 socks5 · Playwright connect_over_cdp 内部走 http/ws · 不认 socks5
        # 副作用范围：仅本函数 · 不动别处
        _saved = {}
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
            if k in os.environ:
                _saved[k] = os.environ.pop(k)
        os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost,*")
        try:
            browser = pw.chromium.connect_over_cdp(CDP_URL)
        finally:
            for k, v in _saved.items():
                os.environ[k] = v
        def cleanup():
            if self_started_proc:
                try:
                    self_started_proc.terminate()
                    self_started_proc.wait(timeout=5)
                except Exception:
                    try:
                        self_started_proc.kill()
                    except Exception:
                        pass
        yield browser, cleanup
    finally:
        try:
            pw.stop()
        except Exception:
            pass
        if self_started_proc:
            try:
                self_started_proc.terminate()
                self_started_proc.wait(timeout=5)
            except Exception:
                try:
                    self_started_proc.kill()
                except Exception:
                    pass


if __name__ == "__main__":
    # 自测
    print(f"CDP alive? {cdp_alive()}")
    with cdp_connection() as (browser, cleanup):
        ctxs = browser.contexts
        pages = [pg for c in ctxs for pg in c.pages]
        print(f"contexts={len(ctxs)} pages={len(pages)}")
        for pg in pages[:5]:
            print(f"  - {pg.url[:100]}")
