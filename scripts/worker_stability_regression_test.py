from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / "venv/bin/python"
TEST_DB = "sqlite:///data/worker-stability-regression.db"
TEST_DB_PATH = ROOT / "data/worker-stability-regression.db"


def main() -> int:
    TEST_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _unlink_sqlite_files(TEST_DB_PATH)

    reset = _run_cli(["reset-dev-db", "--yes"])
    if "reset-dev-db: PASS" not in reset:
        return _fail("reset failed", reset)
    book_id = _extract_id(
        "book_id",
        _run_cli(["create-book", "--title", "Worker Stability Regression", "--genre", "测试", "--platform", "manual"]),
    )
    _run_cli(
        [
            "create-foundation",
            "--book-id",
            str(book_id),
            "--premise",
            "稳定性测试用长任务队列",
            "--reader-promise",
            "每章都有压力、选择、代价和钩子",
        ]
    )
    task_ids: list[int] = []
    for chapter_number in range(1, 6):
        _run_cli(
            [
                "create-chapter-brief",
                "--book-id",
                str(book_id),
                "--chapter-number",
                str(chapter_number),
                "--goal",
                f"稳定性测试第{chapter_number}章",
                "--required-beats",
                "压力,选择,代价,钩子",
                "--constraints",
                "dry-run only",
            ]
        )
        task_ids.append(
            _extract_id(
                "generation_task_id",
                _run_cli(
                    [
                        "enqueue-draft",
                        "--book-id",
                        str(book_id),
                        "--chapter-number",
                        str(chapter_number),
                        "--max-attempts",
                        "2",
                        "--task-timeout-seconds",
                        "1",
                    ]
                ),
            )
        )

    _mark_stale_running(task_ids[0], chapter_number=1)
    health = _run_cli(["generation-queue-health", "--stale-after-seconds", "1"])
    if "running_count=1" not in health or "stale_running_count=1" not in health:
        return _fail("stale task was not visible before supervisor run", health)

    supervisor = _run_script(
        [
            "scripts/run_generation_worker.py",
            "--database-url",
            TEST_DB,
            "--max-supervisor-loops",
            "6",
            "--sleep-seconds",
            "0",
            "--max-tasks-per-loop",
            "2",
            "--recover-stale-before-run",
            "--task-timeout-seconds",
            "1",
            "--log-dir",
            "data/worker-stability-logs",
        ]
    )
    log_file = Path(_extract_value("log_file", supervisor))
    if not log_file.exists():
        return _fail("supervisor log was not created", supervisor)
    log_text = log_file.read_text(encoding="utf-8")
    if "command=recover-stale-generation-tasks --timeout-seconds 1" not in log_text:
        return _fail("supervisor log did not include recovery command", log_text)

    final_health = _run_cli(["generation-queue-health", "--failure-limit", "5", "--stale-after-seconds", "1"])
    if "counts=completed=5" not in final_health or "running_count=0" not in final_health or "stale_running_count=0" not in final_health:
        return _fail("queue did not drain cleanly", final_health)

    print("worker-stability-regression-test: PASS")
    print(f"database={TEST_DB}")
    print(f"book_id={book_id}")
    print(f"log_file={log_file}")
    return 0


def _run_cli(args: list[str], *, expect: int = 0) -> str:
    cmd = [str(PYTHON), "-m", "app.cli", "--database-url", TEST_DB, *args]
    return _run(cmd, expect=expect)


def _run_script(args: list[str], *, expect: int = 0) -> str:
    cmd = [str(PYTHON), *args]
    return _run(cmd, expect=expect)


def _run(cmd: list[str], *, expect: int) -> str:
    result = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True)
    output = (result.stdout + result.stderr).strip()
    if result.returncode != expect:
        print("worker-stability-regression-test: FAIL")
        print("command=" + " ".join(cmd))
        print(f"expected_returncode={expect}")
        print(f"actual_returncode={result.returncode}")
        print(output)
        raise SystemExit(1)
    return output


def _mark_stale_running(task_id: int, *, chapter_number: int) -> None:
    conn = sqlite3.connect(TEST_DB_PATH)
    try:
        conn.execute(
            "update generation_tasks set status='running', input_json=? where id=?",
            (
                json.dumps(
                    {
                        "chapter_number": chapter_number,
                        "dry_run": True,
                        "attempt": 1,
                        "max_attempts": 2,
                        "task_timeout_seconds": 1,
                        "running_started_at": "2000-01-01T00:00:00",
                    },
                    ensure_ascii=False,
                ),
                task_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _unlink_sqlite_files(db_path: Path) -> None:
    for path in [db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")]:
        if path.exists():
            path.unlink()


def _extract_id(name: str, output: str) -> int:
    return int(_extract_value(name, output))


def _extract_value(name: str, output: str) -> str:
    prefix = f"{name}="
    for line in output.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    raise ValueError(f"missing {name} in output:\n{output}")


def _fail(message: str, detail: str) -> int:
    print("worker-stability-regression-test: FAIL")
    print(message)
    print(detail)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
