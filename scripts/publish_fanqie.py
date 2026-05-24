from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish one prepared chapter artifact to Fanqie writer backend.")
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--confirm", action="store_true", help="Click final publish controls when explicitly enabled in plan.")
    parser.add_argument("--timeout-ms", type=int, default=15000)
    args = parser.parse_args()

    artifact_dir = Path(args.artifact_dir).resolve()
    plan_path = artifact_dir / "fanqie_publish_plan.json"
    content_path = artifact_dir / "content.txt"
    report_path = artifact_dir / "fanqie_execution_report.json"
    if not plan_path.exists() or not content_path.exists():
        return _fail(report_path, "missing_plan_or_content", "fanqie_publish_plan.json and content.txt are required")

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    content = content_path.read_text(encoding="utf-8")
    safety = plan.get("safety", {})
    browser_plan = plan.get("browser", {})
    if args.confirm and not safety.get("real_publish_enabled"):
        return _fail(report_path, "real_publish_disabled", "plan does not enable real Fanqie publish")
    if not browser_plan.get("cdp_url") and not browser_plan.get("user_data_dir"):
        return _fail(report_path, "browser_not_configured", "configure cdp_url or user_data_dir before running Fanqie automation")

    started_at = datetime.utcnow().isoformat(timespec="seconds")
    screenshots: list[str] = []
    try:
        with sync_playwright() as p:
            browser, context = _open_context(p, browser_plan)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(plan.get("writer_url") or "https://fanqienovel.com/author", wait_until="domcontentloaded", timeout=args.timeout_ms)
                screenshots.append(_screenshot(page, artifact_dir, "01-opened"))

                selectors = plan.get("selectors", {})
                _fill_first(page, selectors.get("title", "input"), plan.get("chapter_title", ""), timeout=args.timeout_ms)
                _fill_first(page, selectors.get("editor", "textarea"), content, timeout=args.timeout_ms)
                screenshots.append(_screenshot(page, artifact_dir, "02-filled"))

                if args.confirm:
                    _click_optional(page, selectors.get("next_button", "text=下一步"), timeout=args.timeout_ms)
                    screenshots.append(_screenshot(page, artifact_dir, "03-next"))
                    _click_optional(page, selectors.get("publish_button", "text=发布"), timeout=args.timeout_ms)
                    _click_optional(page, selectors.get("confirm_button", "text=确认"), timeout=args.timeout_ms)
                    screenshots.append(_screenshot(page, artifact_dir, "04-confirmed"))
                    status = "published_attempted"
                else:
                    status = "filled_only"

                _write_report(
                    report_path,
                    {
                        "status": status,
                        "started_at": started_at,
                        "finished_at": datetime.utcnow().isoformat(timespec="seconds"),
                        "artifact_dir": str(artifact_dir),
                        "screenshots": screenshots,
                    },
                )
                print(f"fanqie_publish: {status}")
                print(f"report={report_path}")
                return 0
            finally:
                if browser is not None:
                    browser.close()
                else:
                    context.close()
    except Exception as exc:
        return _fail(report_path, "automation_error", str(exc), screenshots=screenshots)


def _open_context(playwright: Any, browser_plan: dict) -> tuple[Any | None, Any]:
    cdp_url = browser_plan.get("cdp_url", "")
    if cdp_url:
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        return browser, context
    user_data_dir = browser_plan.get("user_data_dir", "")
    headless = bool(browser_plan.get("headless", False))
    context = playwright.chromium.launch_persistent_context(user_data_dir, headless=headless)
    return None, context


def _fill_first(page: Any, selector: str, value: str, *, timeout: int) -> None:
    locator = page.locator(selector).first
    locator.wait_for(state="visible", timeout=timeout)
    try:
        locator.fill(value, timeout=timeout)
    except PlaywrightTimeoutError:
        locator.click(timeout=timeout)
        page.keyboard.press("Control+A")
        page.keyboard.insert_text(value)


def _click_optional(page: Any, selector: str, *, timeout: int) -> None:
    locator = page.locator(selector).first
    locator.wait_for(state="visible", timeout=timeout)
    locator.click(timeout=timeout)


def _screenshot(page: Any, artifact_dir: Path, name: str) -> str:
    path = artifact_dir / f"fanqie-{name}.png"
    page.screenshot(path=str(path), full_page=True)
    return str(path)


def _fail(report_path: Path, code: str, message: str, *, screenshots: list[str] | None = None) -> int:
    _write_report(
        report_path,
        {
            "status": "failed",
            "error_code": code,
            "error": message,
            "finished_at": datetime.utcnow().isoformat(timespec="seconds"),
            "screenshots": screenshots or [],
        },
    )
    print("fanqie_publish: failed")
    print(f"error_code={code}")
    print(f"error={message}")
    print(f"report={report_path}")
    return 1


def _write_report(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
