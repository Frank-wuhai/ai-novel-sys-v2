from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / "venv/bin/python"
TEST_DB = "sqlite:///data/author-command-center-regression.db"


def run(args: list[str], *, expect: int = 0) -> str:
    cmd = [str(PYTHON), "-m", "app.cli", "--database-url", TEST_DB, *args]
    result = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True)
    output = (result.stdout + result.stderr).strip()
    if result.returncode != expect:
        print("COMMAND FAILED")
        print(" ".join(cmd))
        print(f"expected={expect} actual={result.returncode}")
        print(output)
        raise SystemExit(1)
    return output


def extract_id(name: str, output: str) -> int:
    prefix = f"{name}="
    for line in output.splitlines():
        if line.startswith(prefix):
            return int(line.removeprefix(prefix))
    print(f"missing {name} in output:")
    print(output)
    raise SystemExit(1)


def assert_center(payload: dict, *, status: str, intent: str) -> None:
    actual_intent = payload.get("primary_action", {}).get("intent")
    if payload.get("status") != status or actual_intent != intent:
        print("unexpected command center state")
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        raise SystemExit(1)


def main() -> int:
    run(["reset-dev-db", "--yes"])
    run(["seed-prompts"])
    book_id = extract_id(
        "book_id",
        run(["create-book", "--title", "Command Center Regression", "--genre", "玄幻都市", "--platform", "manual"]),
    )
    run(
        [
            "create-foundation",
            "--book-id",
            str(book_id),
            "--premise",
            "主角陆沉在玄幻都市危机中获得有限推演能力，目标是在家族追杀和城市异变中夺回主动权；每次推演都要付出记忆、关系或身体代价。",
            "--reader-promise",
            "看主角用有限推演在高压场景中主动选择，每章都有爽点回报、章末钩子、可见代价和追读悬念。",
            "--world-engine",
            "设定通过选择、代价和后果推进。",
            "--protagonist-engine",
            "主角在压力下主动选择并承担代价。",
            "--conflict-engine",
            "外部压力逐章升级并驱动下一章钩子。",
        ]
    )

    before = json.loads(
        run(["author-command-center", "--book-id", str(book_id), "--chapter-number", "1", "--start", "1", "--count", "1"])
    )
    assert_center(before, status="blocked", intent="auto_resolve_blocker")

    preview = json.loads(run(["repair-production-scaffold", "--book-id", str(book_id)]))
    if preview.get("mode") != "preview":
        print("scaffold preview unexpectedly mutated state")
        print(json.dumps(preview, ensure_ascii=False, indent=2, sort_keys=True))
        raise SystemExit(1)
    still_blocked = json.loads(
        run(["author-command-center", "--book-id", str(book_id), "--chapter-number", "1", "--start", "1", "--count", "1"])
    )
    assert_center(still_blocked, status="blocked", intent="auto_resolve_blocker")

    run(["repair-production-scaffold", "--book-id", str(book_id), "--apply"])
    run(
        [
            "add-evidence-source",
            "--source-id",
            "command-center-market",
            "--title",
            "Command center market evidence",
            "--reliability",
            "4",
            "--status",
            "verified",
        ]
    )
    run(
        [
            "add-market-signal",
            "--source-id",
            "command-center-market",
            "--genre",
            "玄幻都市",
            "--signal",
            "玄幻都市开篇需要明确压力、爽点回报和章末钩子。",
            "--confidence",
            "86",
        ]
    )
    after = json.loads(
        run(["author-command-center", "--book-id", str(book_id), "--chapter-number", "1", "--start", "1", "--count", "1"])
    )
    assert_center(after, status="can_produce", intent="continue")
    print("author-command-center-regression: PASS")
    print(f"database={TEST_DB}")
    print(f"book_id={book_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
