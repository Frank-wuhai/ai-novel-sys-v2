from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / "venv/bin/python"
TEST_DB = "sqlite:///data/readiness-regression.db"


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


def assert_contains(output: str, expected: str) -> None:
    if expected not in output:
        print(f"missing expected text: {expected}")
        print(output)
        raise SystemExit(1)


def main() -> int:
    run(["reset-dev-db", "--yes"])
    run(["seed-prompts"])
    run(
        [
            "add-evidence-source",
            "--source-id",
            "wrong-genre-source",
            "--title",
            "Wrong genre evidence",
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
            "wrong-genre-source",
            "--genre",
            "玄幻都市",
            "--signal",
            "这个信号只适用于玄幻都市。",
            "--confidence",
            "80",
        ]
    )
    book_id = extract_id(
        "book_id",
        run(["create-book", "--title", "Readiness Genre Regression", "--genre", "科幻", "--platform", "manual"]),
    )
    run(
        [
            "create-foundation",
            "--book-id",
            str(book_id),
            "--premise",
            "测试题材证据匹配",
            "--reader-promise",
            "生产前必须有本题材证据",
        ]
    )
    run(["add-character", "--book-id", str(book_id), "--name", "沈星", "--role", "主角"])
    run(["add-world-rule", "--book-id", str(book_id), "--category", "科技边界", "--rule", "技术突破必须有成本。"])
    run(["add-power-system", "--book-id", str(book_id), "--name", "星图演算", "--rules", "只能模拟局部航线。", "--status", "active"])
    run(["create-chapter-plan", "--book-id", str(book_id), "--start", "1", "--count", "1", "--goal-prefix", "回归测试"])

    wrong_genre_report = run(["production-readiness", "--book-id", str(book_id), "--start", "1", "--count", "1"])
    assert_contains(wrong_genre_report, "check\tevidence\tpassed=False")
    assert_contains(wrong_genre_report, "no usable market signals for genre=科幻")

    run(
        [
            "add-evidence-source",
            "--source-id",
            "right-genre-source",
            "--title",
            "Right genre evidence",
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
            "right-genre-source",
            "--genre",
            "科幻",
            "--signal",
            "科幻章节需要把技术代价前置为剧情压力。",
            "--confidence",
            "80",
        ]
    )
    right_genre_report = run(["production-readiness", "--book-id", str(book_id), "--start", "1", "--count", "1"])
    assert_contains(right_genre_report, "check\tevidence\tpassed=True")
    assert_contains(right_genre_report, "genre=科幻 usable_market_signals=1")
    print("readiness-regression-test: PASS")
    print(f"database={TEST_DB}")
    print(f"book_id={book_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
