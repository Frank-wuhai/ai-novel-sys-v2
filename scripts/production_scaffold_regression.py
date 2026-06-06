from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / "venv/bin/python"
TEST_DB = "sqlite:///data/production-scaffold-regression.db"


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


def assert_not_contains(output: str, unexpected: str) -> None:
    if unexpected in output:
        print(f"unexpected text: {unexpected}")
        print(output)
        raise SystemExit(1)


def main() -> int:
    run(["reset-dev-db", "--yes"])
    run(["seed-prompts"])
    book_id = extract_id(
        "book_id",
        run(["create-book", "--title", "Scaffold Regression", "--genre", "玄幻都市", "--platform", "manual"]),
    )
    preview = json.loads(run(["repair-production-scaffold", "--book-id", str(book_id)]))
    if preview.get("mode") != "preview" or not preview.get("planned_count"):
        print("preview did not report planned changes")
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        raise SystemExit(1)
    assert_contains(run(["show-story-bible", "--book-id", str(book_id)], expect=1), "story bible not found")
    readiness_before = run(["production-readiness", "--book-id", str(book_id), "--start", "1", "--count", "1"])
    assert_contains(readiness_before, "passed=False")
    assert_contains(readiness_before, "check\tstory_bible\tpassed=False")

    applied = json.loads(run(["repair-production-scaffold", "--book-id", str(book_id), "--apply"]))
    if applied.get("mode") != "applied" or not applied.get("created_count"):
        print("apply did not create scaffold records")
        print(json.dumps(applied, ensure_ascii=False, indent=2))
        raise SystemExit(1)
    readiness_after = run(["production-readiness", "--book-id", str(book_id), "--start", "1", "--count", "1"])
    assert_contains(readiness_after, "passed=True")
    assert_contains(readiness_after, "check\tstory_bible\tpassed=True")
    assert_contains(readiness_after, "check\tevidence\tpassed=True")
    assert_contains(readiness_after, "check\tcanon\tpassed=True")
    print("production-scaffold-regression: PASS")
    print(f"database={TEST_DB}")
    print(f"book_id={book_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
