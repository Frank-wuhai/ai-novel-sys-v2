from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(prog="run_generation_worker.py")
    parser.add_argument("--database-url", default="")
    parser.add_argument("--book-id", type=int, default=0)
    parser.add_argument("--token-budget", type=int, default=0)
    parser.add_argument("--max-supervisor-loops", type=int, default=1)
    parser.add_argument("--sleep-seconds", type=float, default=5.0)
    parser.add_argument("--max-tasks-per-loop", type=int, default=1)
    parser.add_argument("--recover-stale-before-run", action="store_true")
    parser.add_argument("--task-timeout-seconds", type=int, default=3600)
    parser.add_argument("--log-dir", default="logs")
    args = parser.parse_args()

    if args.max_supervisor_loops < 1:
        print("ERROR: --max-supervisor-loops must be >= 1", file=sys.stderr)
        return 1
    if args.max_tasks_per_loop < 1:
        print("ERROR: --max-tasks-per-loop must be >= 1", file=sys.stderr)
        return 1
    if args.task_timeout_seconds < 1:
        print("ERROR: --task-timeout-seconds must be >= 1", file=sys.stderr)
        return 1
    if args.token_budget and not args.book_id:
        print("ERROR: --book-id is required when --token-budget is set", file=sys.stderr)
        return 1

    log_dir = (ROOT / args.log_dir).resolve() if not Path(args.log_dir).is_absolute() else Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"generation-worker-{datetime.now().strftime('%Y%m%d')}.log"
    print(f"log_file={log_file}")

    exit_code = 0
    for loop_index in range(1, args.max_supervisor_loops + 1):
        _append(log_file, f"supervisor_loop={loop_index} started_at={_timestamp()}")
        health = _run_cli(["generation-queue-health"], database_url=args.database_url)
        _append_command(log_file, "generation-queue-health", health)
        if health.returncode != 0:
            exit_code = health.returncode
            break

        if args.recover_stale_before_run:
            recovery = _run_cli(
                ["recover-stale-generation-tasks", "--timeout-seconds", str(args.task_timeout_seconds)],
                database_url=args.database_url,
            )
            _append_command(log_file, f"recover-stale-generation-tasks --timeout-seconds {args.task_timeout_seconds}", recovery)
            if recovery.returncode != 0:
                exit_code = recovery.returncode
                break

        worker_args = [
            "run-generation-worker",
            "--max-loops",
            "1",
            "--sleep-seconds",
            "0",
            "--max-tasks-per-loop",
            str(args.max_tasks_per_loop),
        ]
        if args.recover_stale_before_run:
            worker_args.extend(["--recover-stale-before-run", "--task-timeout-seconds", str(args.task_timeout_seconds)])
        if args.book_id:
            worker_args.extend(["--book-id", str(args.book_id)])
        if args.token_budget:
            worker_args.extend(["--token-budget", str(args.token_budget)])
        worker = _run_cli(worker_args, database_url=args.database_url)
        _append_command(log_file, " ".join(worker_args), worker)
        if worker.returncode != 0:
            exit_code = worker.returncode
            break
        if loop_index < args.max_supervisor_loops and args.sleep_seconds > 0:
            time.sleep(args.sleep_seconds)

    _append(log_file, f"supervisor_done exit_code={exit_code} finished_at={_timestamp()}")
    return exit_code


def _run_cli(args: list[str], *, database_url: str) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "app.cli"]
    if database_url:
        cmd.extend(["--database-url", database_url])
    cmd.extend(args)
    return subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True)


def _append_command(log_file: Path, command: str, result: subprocess.CompletedProcess[str]) -> None:
    _append(log_file, f"command={command}")
    _append(log_file, f"returncode={result.returncode}")
    output = (result.stdout + result.stderr).strip()
    if output:
        _append(log_file, output)


def _append(log_file: Path, text: str) -> None:
    with log_file.open("a", encoding="utf-8") as handle:
        handle.write(text.rstrip() + "\n")


def _timestamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


if __name__ == "__main__":
    raise SystemExit(main())
