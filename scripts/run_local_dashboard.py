from __future__ import annotations

import argparse
import json
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.session import configure_database, session_scope
from app.models.entities import Book
from app.services.dashboard import build_project_snapshot
from app.services.llm_queue import build_generation_queue_health, run_generation_queue
from app.services.planning import AUTO_ACTIONS, run_next_action


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI Novel System v2</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --ink: #1f2933;
      --muted: #627282;
      --line: #d8dee6;
      --accent: #0f766e;
      --warn: #b45309;
      --bad: #b91c1c;
      --ok: #166534;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--ink);
    }
    header {
      padding: 16px 24px;
      background: #20262e;
      color: #fff;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }
    h1 { font-size: 18px; margin: 0; font-weight: 650; letter-spacing: 0; }
    main { max-width: 1280px; margin: 0 auto; padding: 20px; }
    .toolbar {
      display: grid;
      grid-template-columns: 1fr 110px 110px auto;
      gap: 10px;
      align-items: end;
      margin-bottom: 16px;
    }
    label { display: grid; gap: 6px; font-size: 12px; color: var(--muted); }
    select, input, button {
      height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
      font: inherit;
      padding: 0 10px;
    }
    button { background: var(--accent); color: #fff; border-color: var(--accent); cursor: pointer; }
    .summary {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 10px;
      margin-bottom: 16px;
    }
    .metric, .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    .metric { padding: 12px; min-height: 74px; }
    .metric span { display: block; color: var(--muted); font-size: 12px; margin-bottom: 6px; }
    .metric strong { font-size: 20px; }
    .grid { display: grid; grid-template-columns: 1.2fr .8fr; gap: 16px; align-items: start; }
    .panel { overflow: hidden; }
    .panel h2 {
      margin: 0;
      padding: 12px 14px;
      font-size: 14px;
      border-bottom: 1px solid var(--line);
      background: #fbfcfd;
    }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { padding: 9px 10px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
    th { color: var(--muted); font-weight: 600; background: #fbfcfd; }
    .status { font-weight: 650; }
    .ok { color: var(--ok); }
    .warn { color: var(--warn); }
    .bad { color: var(--bad); }
    .muted { color: var(--muted); }
    .command { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; overflow-wrap: anywhere; }
    .stack { display: grid; gap: 16px; }
    .empty { padding: 14px; color: var(--muted); }
    .actions { display: flex; flex-wrap: wrap; gap: 8px; padding: 12px 14px; border-top: 1px solid var(--line); }
    .actions button { height: 32px; font-size: 13px; }
    .actions button.secondary { background: #ffffff; color: var(--ink); border-color: var(--line); }
    @media (max-width: 900px) {
      .toolbar, .summary, .grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <h1>AI Novel System v2</h1>
    <div id="state" class="muted">Loading</div>
  </header>
  <main>
    <section class="toolbar">
      <label>Book<select id="book"></select></label>
      <label>Start<input id="start" type="number" min="1" value="1"></label>
      <label>Count<input id="count" type="number" min="1" value="20"></label>
      <button id="refresh">Refresh</button>
    </section>
    <section class="summary" id="summary"></section>
    <section class="grid">
      <div class="stack">
        <section class="panel">
          <h2>Chapters</h2>
          <div id="chapters"></div>
        </section>
        <section class="panel">
          <h2>Human Decisions</h2>
          <div id="decisions"></div>
        </section>
      </div>
      <div class="stack">
        <section class="panel">
          <h2>Queue Health</h2>
          <div id="queue"></div>
        </section>
        <section class="panel">
          <h2>Readiness</h2>
          <div id="readiness"></div>
        </section>
        <section class="panel">
          <h2>Recommendation</h2>
          <div id="recommendation" class="empty"></div>
          <div class="actions">
            <button id="runQueue">Run Queue Once</button>
            <button id="runNext" class="secondary">Run Safe Next Action</button>
          </div>
        </section>
      </div>
    </section>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    let currentSnapshot = null;

    async function loadBooks() {
      const books = await fetchJson('/api/books');
      $('book').innerHTML = books.map((book) =>
        `<option value="${book.id}">${escapeHtml(book.title)} #${book.id}</option>`
      ).join('');
      if (books.length) await refresh();
      else $('state').textContent = 'No books';
    }

    async function refresh() {
      const bookId = $('book').value;
      if (!bookId) return;
      $('state').textContent = 'Refreshing';
      const params = new URLSearchParams({book_id: bookId, start: $('start').value, count: $('count').value});
      const [snapshot, health] = await Promise.all([
        fetchJson('/api/snapshot?' + params.toString()),
        fetchJson('/api/queue-health')
      ]);
      currentSnapshot = snapshot;
      renderSummary(snapshot, health);
      renderChapters(snapshot.chapters);
      renderDecisions(snapshot.human_decisions.items);
      renderQueue(snapshot, health);
      renderReadiness(snapshot.readiness);
      $('recommendation').innerHTML = `<div class="command">${escapeHtml(snapshot.recommendation)}</div>`;
      $('state').textContent = `Updated ${new Date().toLocaleTimeString()}`;
    }

    async function fetchJson(path) {
      const response = await fetch(path);
      if (!response.ok) throw new Error(await response.text());
      return response.json();
    }

    async function postAction(action, payload = {}) {
      $('state').textContent = `Running ${action}`;
      const response = await fetch('/api/action', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({action, ...payload})
      });
      if (!response.ok) throw new Error(await response.text());
      const result = await response.json();
      $('state').textContent = `${action}: ${result.status || 'done'}`;
      await refresh();
    }

    function renderSummary(snapshot, health) {
      const queueCounts = health.counts || {};
      const decisions = snapshot.human_decisions;
      $('summary').innerHTML = [
        metric('Readiness', snapshot.readiness.passed ? 'PASS' : 'BLOCKED', snapshot.readiness.passed ? 'ok' : 'bad'),
        metric('Pending', queueCounts.pending || 0, 'warn'),
        metric('Failed', queueCounts.failed || 0, (queueCounts.failed || 0) ? 'bad' : 'ok'),
        metric('Tokens', snapshot.generation_recent.estimated_tokens || 0, ''),
        metric('Decisions', decisions.continuity + decisions.approval + decisions.publish + decisions.inspect, 'warn')
      ].join('');
    }

    function metric(label, value, cls) {
      return `<div class="metric"><span>${label}</span><strong class="${cls}">${value}</strong></div>`;
    }

    function renderChapters(chapters) {
      if (!chapters.length) return empty('chapters');
      $('chapters').innerHTML = table(['#', 'Version', 'Quality', 'Next', 'Reason'], chapters.map((item) => [
        item.number,
        item.version_status || 'missing',
        item.quality_passed,
        item.next_action,
        item.reason
      ]));
    }

    function renderDecisions(items) {
      if (!items.length) return empty('decisions');
      $('decisions').innerHTML = table(['Type', 'Chapter', 'Reason', 'Command'], items.map((item) => [
        item.type,
        item.chapter,
        item.reason,
        `<span class="command">${escapeHtml(item.command_hint)}</span>`
      ]), true);
    }

    function renderQueue(snapshot, health) {
      const rows = Object.entries(health.counts || {}).map(([name, count]) => [name, count]);
      const failureRows = (health.latest_failures || []).map((item) => [
        `failure #${item.task_id}`,
        item.chapter_number || '',
        item.error_category || '',
        item.error || ''
      ]);
      const taskRows = (snapshot.generation_queue.tasks || []).map((item) => [
        `task #${item.id}`,
        item.chapter || '',
        item.status,
        item.error_category || ''
      ]);
      $('queue').innerHTML =
        table(['Status', 'Count'], rows) +
        table(['Recent', 'Chapter', 'Status', 'Detail'], failureRows.concat(taskRows));
    }

    function renderReadiness(readiness) {
      $('readiness').innerHTML = table(['Check', 'Passed', 'Detail'], readiness.checks.map((item) => [
        item.name,
        `<span class="${item.passed ? 'ok' : 'bad'}">${item.passed}</span>`,
        item.detail
      ]), true);
    }

    function table(headers, rows, trusted = false) {
      const head = headers.map((item) => `<th>${escapeHtml(String(item))}</th>`).join('');
      const body = rows.map((row) => `<tr>${row.map((item) => `<td>${trusted ? item : escapeHtml(String(item ?? ''))}</td>`).join('')}</tr>`).join('');
      return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
    }

    function empty(id) {
      $(id).innerHTML = '<div class="empty">No items</div>';
    }

    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, (char) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;'}[char]));
    }

    $('refresh').addEventListener('click', refresh);
    $('runQueue').addEventListener('click', () => {
      postAction('run_queue', {max_tasks: 1}).catch(showError);
    });
    $('runNext').addEventListener('click', () => {
      const item = (currentSnapshot?.chapters || []).find((chapter) =>
        ['create_chapter_brief', 'draft_chapter', 'review_chapter', 'create_revision_brief', 'revise_chapter', 'create_publish_job', 'publish_job_dry_run', 'queue_publish_job', 'retry_publish_job'].includes(chapter.next_action)
      );
      if (!item) {
        showError(new Error('No safe next action in selected range'));
        return;
      }
      postAction('run_next_action', {book_id: currentSnapshot.book.id, chapter_number: item.number, dry_run: true}).catch(showError);
    });

    function showError(error) {
      $('state').textContent = 'Error';
      document.querySelector('main').insertAdjacentHTML('afterbegin', `<div class="panel empty">${escapeHtml(error.message)}</div>`);
    }

    loadBooks().catch((error) => {
      showError(error);
    });
  </script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(prog="run_local_dashboard.py")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--database-url", default="")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.database_url:
        configure_database(args.database_url)
    if args.self_test:
        with session_scope() as session:
            books = list(session.scalars(select(Book).order_by(Book.id)))
            queue = build_generation_queue_health(session)
            if books:
                build_project_snapshot(session, book_id=books[0].id, start=1, count=1)
            action_result = _perform_action(session, {"action": "queue_health"})
            print("dashboard_self_test=PASS")
            print(f"book_count={len(books)}")
            print(f"queue_total={queue.total}")
            print(f"action_status={action_result['status']}")
        return 0
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    host, port = server.server_address
    print(f"dashboard_url=http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                self._send_html(HTML)
                return
            if parsed.path == "/api/books":
                with session_scope() as session:
                    books = list(session.scalars(select(Book).order_by(Book.id)))
                    self._send_json(
                        [
                            {
                                "id": book.id,
                                "title": book.title,
                                "genre": book.genre,
                                "platform": book.target_platform,
                                "status": book.status,
                            }
                            for book in books
                        ]
                    )
                return
            if parsed.path == "/api/snapshot":
                query = parse_qs(parsed.query)
                book_id = _int_query(query, "book_id", 0)
                if not book_id:
                    raise ValueError("book_id is required")
                with session_scope() as session:
                    self._send_json(
                        build_project_snapshot(
                            session,
                            book_id=book_id,
                            start=_int_query(query, "start", 1),
                            count=_int_query(query, "count", 20),
                        )
                    )
                return
            if parsed.path == "/api/queue-health":
                with session_scope() as session:
                    report = build_generation_queue_health(session)
                    self._send_json(
                        {
                            "total": report.total,
                            "counts": report.counts,
                            "oldest_pending_id": report.oldest_pending_id,
                            "oldest_pending_chapter": report.oldest_pending_chapter,
                            "latest_failures": [
                                {
                                    "task_id": item.task_id,
                                    "task_type": item.task_type,
                                    "chapter_number": item.chapter_number,
                                    "attempt": item.attempt,
                                    "max_attempts": item.max_attempts,
                                    "error_category": item.error_category,
                                    "error": item.error,
                                    "retryable": item.retryable,
                                }
                                for item in report.latest_failures
                            ],
                        }
                    )
                return
            self._send_text("not found", status=HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._send_text(f"ERROR: {exc}", status=HTTPStatus.BAD_REQUEST)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path != "/api/action":
                self._send_text("not found", status=HTTPStatus.NOT_FOUND)
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            payload = json.loads(raw or "{}")
            if not isinstance(payload, dict):
                raise ValueError("JSON object is required")
            with session_scope() as session:
                self._send_json(_perform_action(session, payload))
        except Exception as exc:
            self._send_text(f"ERROR: {exc}", status=HTTPStatus.BAD_REQUEST)

    def log_message(self, format: str, *args) -> None:
        return

    def _send_json(self, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, value: str) -> None:
        body = value.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, value: str, *, status: HTTPStatus) -> None:
        body = value.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _int_query(query: dict[str, list[str]], name: str, default: int) -> int:
    values = query.get(name)
    if not values:
        return default
    return int(values[0])


def _perform_action(session, payload: dict) -> dict:
    action = str(payload.get("action") or "")
    if action == "queue_health":
        report = build_generation_queue_health(session)
        return {"status": "ok", "total": report.total, "counts": report.counts}
    if action == "run_queue":
        max_tasks = int(payload.get("max_tasks") or 1)
        if max_tasks < 1 or max_tasks > 3:
            raise ValueError("max_tasks must be between 1 and 3")
        batch = run_generation_queue(session, max_tasks=max_tasks)
        return {
            "status": "executed",
            "executed_count": len(batch.results),
            "tasks": [
                {
                    "generation_task_id": result.task.id,
                    "status": result.task.status,
                    "version_id": result.version_id,
                    "child_generation_task_id": result.child_generation_task_id,
                }
                for result in batch.results
            ],
        }
    if action == "run_next_action":
        book_id = int(payload.get("book_id") or 0)
        chapter_number = int(payload.get("chapter_number") or 0)
        if not book_id or not chapter_number:
            raise ValueError("book_id and chapter_number are required")
        result = run_next_action(
            session,
            book_id=book_id,
            chapter_number=chapter_number,
            dry_run=bool(payload.get("dry_run", True)),
        )
        if result.action not in AUTO_ACTIONS or result.status != "executed":
            raise ValueError(f"action is not safe or executable: {result.action} {result.status}")
        return {
            "status": result.status,
            "action": result.action,
            "chapter_number": result.chapter_number,
            "message": result.message,
            "object_id": result.object_id,
        }
    raise ValueError(f"unsupported action: {action}")


if __name__ == "__main__":
    raise SystemExit(main())
