from __future__ import annotations

import argparse
import subprocess
import sys


def _run(args: list[str]) -> None:
    command = [sys.executable, "-m", "app.cli", *args]
    print("$ " + " ".join(command), flush=True)
    subprocess.run(command, check=True)


def status(args: argparse.Namespace) -> None:
    _run(["development-status", "--book-id", str(args.book_id), "--start", str(args.start), "--count", str(args.count)])
    _run(["production-readiness", "--book-id", str(args.book_id), "--start", str(args.start), "--count", str(args.count)])
    _run(["agent-plan-utilization", "--book-id", str(args.book_id)])
    _run(["generation-queue-health"])


def prepare(args: argparse.Namespace) -> None:
    _run(["backup-database", "--label", "agent-ops-prepare"])
    cycle = [
        "agent-plan-cycle",
        "--book-id",
        str(args.book_id),
        "--chapter-number",
        str(args.chapter_number),
        "--skip-visuals",
    ]
    if args.live_embedding:
        cycle.append("--live-embedding")
    _run(cycle)
    repair = ["repair-production-scaffold", "--book-id", str(args.book_id), "--chapter-count", str(args.count)]
    if args.apply:
        repair.append("--apply")
    _run(repair)
    _run(["production-readiness", "--book-id", str(args.book_id), "--start", str(args.start), "--count", str(args.count)])


def next_action(args: argparse.Namespace) -> None:
    command = [
        "run-next-action",
        "--book-id",
        str(args.book_id),
        "--chapter-number",
        str(args.chapter_number),
        "--platform",
        args.platform,
    ]
    if not args.execute:
        command.append("--preview-only")
    elif args.queue_generation:
        command.append("--queue-generation")
    _run(command)


def worker(args: argparse.Namespace) -> None:
    if not args.execute:
        _run(["list-generation-queue", "--status", "pending", "--limit", str(args.limit)])
        _run(["generation-queue-health"])
        return
    command = [
        "run-generation-worker",
        "--book-id",
        str(args.book_id),
        "--max-loops",
        str(args.max_loops),
        "--max-tasks-per-loop",
        str(args.max_tasks_per_loop),
        "--sleep-seconds",
        str(args.sleep_seconds),
        "--recover-stale-before-run",
    ]
    if args.token_budget:
        command.extend(["--token-budget", str(args.token_budget)])
    _run(command)


def publish_dry_run(args: argparse.Namespace) -> None:
    _run(["publish-preflight", "--version-id", str(args.version_id)])
    _run(["create-publish-job", "--version-id", str(args.version_id), "--platform", args.platform])
    print("Create step printed the publish_job_id. Run this manually after inspection:")
    print("python -m app.cli publish-job-dry-run --job-id <publish_job_id>")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="agent-ops",
        description="Whitelisted operation entrypoint for external agents such as OpenClaw or Hermes.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("status", help="Read-only system and book diagnostics.")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--start", type=int, default=1)
    p.add_argument("--count", type=int, default=5)
    p.set_defaults(func=status)

    p = sub.add_parser("prepare", help="Back up, refresh Agent Plan context, and preview scaffold repair.")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--chapter-number", type=int, default=1)
    p.add_argument("--start", type=int, default=1)
    p.add_argument("--count", type=int, default=5)
    p.add_argument("--live-embedding", action="store_true")
    p.add_argument("--apply", action="store_true", help="Apply scaffold repair after backup. Omit for preview-only.")
    p.set_defaults(func=prepare)

    p = sub.add_parser("next-action", help="Preview or execute the next workflow action for one chapter.")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--chapter-number", type=int, required=True)
    p.add_argument("--platform", default="manual")
    p.add_argument("--queue-generation", action="store_true")
    p.add_argument("--execute", action="store_true", help="Actually run the workflow action. Omit to preview only.")
    p.set_defaults(func=next_action)

    p = sub.add_parser("worker", help="Inspect or run the generation queue worker.")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--max-loops", type=int, default=1)
    p.add_argument("--max-tasks-per-loop", type=int, default=1)
    p.add_argument("--sleep-seconds", type=float, default=2.0)
    p.add_argument("--token-budget", type=int, default=0)
    p.add_argument("--execute", action="store_true", help="Run queued generation tasks. Omit to inspect only.")
    p.set_defaults(func=worker)

    p = sub.add_parser("publish-dry-run", help="Create a publish job after preflight; dry-run execution remains manual.")
    p.add_argument("--version-id", type=int, required=True)
    p.add_argument("--platform", default="manual")
    p.set_defaults(func=publish_dry_run)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
