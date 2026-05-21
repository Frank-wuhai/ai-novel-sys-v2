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
from app.models.entities import Book, Chapter, ChapterBrief, ChapterVersion, GenerationTask, QualityReport
from app.services.canon import format_canon_context
from app.services.dashboard import build_project_snapshot
from app.services.evidence import audit_market_evidence, format_market_evidence_context
from app.services.feedback import (
    apply_feedback_adjustment_to_brief,
    create_feedback_adjustment,
    list_feedback_adjustments,
    list_platform_feedback,
    record_platform_feedback,
    summarize_platform_feedback,
)
from app.services.llm_queue import (
    build_generation_queue_health,
    cancel_generation_queue_task,
    pause_generation_queue_task,
    resume_generation_queue_task,
    retry_generation_queue_task,
    run_generation_queue,
)
from app.services.planning import AUTO_ACTIONS, run_next_action
from app.services.story import format_story_control_context, get_story_bible


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
      grid-template-columns: 1fr 90px 90px 90px auto;
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
    .full { margin-top: 16px; }
    .forms { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 8px; padding: 12px 14px; border-top: 1px solid var(--line); }
    textarea {
      min-height: 72px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px 10px;
      font: inherit;
      resize: vertical;
    }
    pre {
      margin: 0;
      padding: 12px 14px;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      font: 12px ui-monospace, SFMono-Regular, Menlo, monospace;
      color: var(--ink);
    }
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
      .toolbar, .summary, .grid, .forms { grid-template-columns: 1fr; }
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
      <label>Chapter<input id="chapter" type="number" min="1" value="1"></label>
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
    <section class="panel full">
      <h2>Chapter Detail</h2>
      <div id="chapterDetail"></div>
    </section>
    <section class="panel full">
      <h2>Feedback</h2>
      <div id="feedback"></div>
      <div class="forms">
        <label>Platform<input id="feedbackPlatform" value="manual"></label>
        <label>Metric<input id="feedbackMetric" value="comment"></label>
        <label>Value<input id="feedbackValue" value=""></label>
        <label>Target<input id="feedbackTarget" type="number" min="1" value="1"></label>
        <button id="recordFeedback">Record Feedback</button>
        <label style="grid-column: 1 / -1;">Raw Text<textarea id="feedbackRaw"></textarea></label>
        <label>Feedback IDs<input id="adjustmentFeedbackIds" placeholder="1,2"></label>
        <label>Adjustment Target<input id="adjustmentTarget" type="number" min="1" value="1"></label>
        <label style="grid-column: span 2;">Adjustment Text<input id="adjustmentText"></label>
        <button id="createAdjustment">Create Adjustment</button>
      </div>
    </section>
    <section class="panel full">
      <h2>Knowledge Context</h2>
      <div id="knowledge"></div>
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
      const chapterParams = new URLSearchParams({book_id: bookId, chapter_number: $('chapter').value});
      const [snapshot, health] = await Promise.all([
        fetchJson('/api/snapshot?' + params.toString()),
        fetchJson('/api/queue-health')
      ]);
      const [detail, feedback, knowledge] = await Promise.all([
        fetchJson('/api/chapter-detail?' + chapterParams.toString()),
        fetchJson('/api/feedback?book_id=' + encodeURIComponent(bookId)),
        fetchJson('/api/knowledge?' + chapterParams.toString())
      ]);
      currentSnapshot = snapshot;
      renderSummary(snapshot, health);
      renderChapters(snapshot.chapters);
      renderDecisions(snapshot.human_decisions.items);
      renderQueue(snapshot, health);
      renderReadiness(snapshot.readiness);
      renderChapterDetail(detail);
      renderFeedback(feedback);
      renderKnowledge(knowledge);
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
        `<button class="secondary" data-select-chapter="${item.number}">${item.number}</button>`,
        escapeHtml(item.version_status || 'missing'),
        escapeHtml(item.quality_passed),
        escapeHtml(item.next_action),
        escapeHtml(item.reason)
      ]), true);
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
        escapeHtml(`task #${item.id}`),
        escapeHtml(item.chapter || ''),
        escapeHtml(item.status),
        escapeHtml(item.error_category || ''),
        queueButtons(item)
      ]);
      $('queue').innerHTML =
        table(['Status', 'Count'], rows) +
        table(['Recent Failure', 'Chapter', 'Status', 'Detail'], failureRows) +
        table(['Task', 'Chapter', 'Status', 'Detail', 'Actions'], taskRows, true);
    }

    function queueButtons(item) {
      const buttons = [];
      if (item.status === 'pending') {
        buttons.push(actionButton('Pause', 'pause_queue_task', item.id));
        buttons.push(actionButton('Cancel', 'cancel_queue_task', item.id));
      } else if (item.status === 'paused') {
        buttons.push(actionButton('Resume', 'resume_queue_task', item.id));
        buttons.push(actionButton('Cancel', 'cancel_queue_task', item.id));
      } else if (item.status === 'failed') {
        buttons.push(actionButton('Retry', 'retry_queue_task', item.id));
        buttons.push(actionButton('Cancel', 'cancel_queue_task', item.id));
      }
      return `<div class="actions">${buttons.join('')}</div>`;
    }

    function actionButton(label, action, taskId) {
      return `<button class="secondary" data-action="${action}" data-task-id="${taskId}">${label}</button>`;
    }

    function renderReadiness(readiness) {
      $('readiness').innerHTML = table(['Check', 'Passed', 'Detail'], readiness.checks.map((item) => [
        item.name,
        `<span class="${item.passed ? 'ok' : 'bad'}">${item.passed}</span>`,
        item.detail
      ]), true);
    }

    function renderChapterDetail(detail) {
      if (!detail.chapter) {
        $('chapterDetail').innerHTML = '<div class="empty">Chapter is missing</div>';
        return;
      }
      const brief = detail.latest_brief;
      const quality = detail.latest_quality;
      $('chapterDetail').innerHTML =
        table(['Field', 'Value'], [
          ['chapter_id', detail.chapter.id],
          ['number', detail.chapter.number],
          ['status', detail.chapter.status],
          ['latest brief', brief ? `#${brief.id} ${brief.status}` : ''],
          ['latest quality', quality ? `${quality.passed} score=${quality.score}` : '']
        ]) +
        (brief ? `<pre>${escapeHtml(['Goal: ' + brief.goal, 'Beats: ' + brief.required_beats, 'Constraints: ' + brief.constraints].join('\n'))}</pre>` : '') +
        table(['Version', 'Status', 'Source', 'Chars', 'Title'], detail.versions.map((item) => [
          item.id,
          item.status,
          item.source,
          item.content_chars,
          item.title
        ])) +
        table(['Task', 'Type', 'Status', 'Attempt', 'Error'], detail.generation_tasks.map((item) => [
          item.id,
          item.type,
          item.status,
          item.attempt || '',
          item.error_category || ''
        ]));
    }

    function renderFeedback(payload) {
      $('feedbackTarget').value = $('chapter').value;
      $('adjustmentTarget').value = $('chapter').value;
      $('feedback').innerHTML =
        table(['Metric', 'Count'], Object.entries(payload.summary.by_metric || {})) +
        table(['Feedback', 'Platform', 'Metric', 'Value', 'Raw'], payload.items.map((item) => [
          `<button class="secondary" data-add-feedback-id="${item.id}">${item.id}</button>`,
          escapeHtml(item.platform),
          escapeHtml(item.metric_name),
          escapeHtml(item.metric_value),
          escapeHtml(item.raw_text)
        ]), true) +
        table(['Adjustment', 'Target', 'Status', 'Feedback', 'Text'], payload.adjustments.map((item) => [
          item.id,
          item.target_chapter_number,
          item.status,
          item.feedback_ids,
          item.adjustment_text
        ]));
    }

    function renderKnowledge(payload) {
      $('knowledge').innerHTML =
        table(['Story Bible', 'Status'], [[payload.story_bible?.id || '', payload.story_bible?.status || 'missing']]) +
        `<pre>${escapeHtml('Story Context\n' + payload.story_context + '\n\nCanon Context\n' + payload.canon_context + '\n\nMarket Evidence\n' + payload.evidence_context)}</pre>` +
        table(['Signal', 'Usable', 'Reason', 'Source'], payload.evidence_audit.map((item) => [
          item.signal_id,
          item.usable,
          item.reasons.join(',') || 'usable',
          item.source
        ]));
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
    document.addEventListener('click', (event) => {
      const chapterButton = event.target.closest('button[data-select-chapter]');
      if (chapterButton) {
        $('chapter').value = chapterButton.dataset.selectChapter;
        refresh().catch(showError);
      }
      const feedbackButton = event.target.closest('button[data-add-feedback-id]');
      if (feedbackButton) {
        const current = $('adjustmentFeedbackIds').value.trim();
        const ids = current ? current.split(',').map((item) => item.trim()).filter(Boolean) : [];
        if (!ids.includes(feedbackButton.dataset.addFeedbackId)) {
          ids.push(feedbackButton.dataset.addFeedbackId);
        }
        $('adjustmentFeedbackIds').value = ids.join(',');
      }
    });
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
    document.addEventListener('click', (event) => {
      const button = event.target.closest('button[data-action]');
      if (!button) return;
      postAction(button.dataset.action, {task_id: Number(button.dataset.taskId)}).catch(showError);
    });
    $('recordFeedback').addEventListener('click', () => {
      postAction('record_feedback', {
        book_id: Number($('book').value),
        chapter_number: Number($('feedbackTarget').value),
        platform: $('feedbackPlatform').value,
        metric_name: $('feedbackMetric').value,
        metric_value: $('feedbackValue').value,
        raw_text: $('feedbackRaw').value
      }).catch(showError);
    });
    $('createAdjustment').addEventListener('click', () => {
      postAction('create_feedback_adjustment', {
        book_id: Number($('book').value),
        target_chapter_number: Number($('adjustmentTarget').value),
        feedback_ids: $('adjustmentFeedbackIds').value,
        adjustment_text: $('adjustmentText').value,
        apply_to_brief: true
      }).catch(showError);
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
                _chapter_detail(session, book_id=books[0].id, chapter_number=1)
                _feedback_payload(session, book_id=books[0].id)
                _knowledge_payload(session, book_id=books[0].id, chapter_number=1)
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
            if parsed.path == "/api/chapter-detail":
                query = parse_qs(parsed.query)
                with session_scope() as session:
                    self._send_json(
                        _chapter_detail(
                            session,
                            book_id=_int_query(query, "book_id", 0),
                            chapter_number=_int_query(query, "chapter_number", 1),
                        )
                    )
                return
            if parsed.path == "/api/feedback":
                query = parse_qs(parsed.query)
                with session_scope() as session:
                    self._send_json(_feedback_payload(session, book_id=_int_query(query, "book_id", 0)))
                return
            if parsed.path == "/api/knowledge":
                query = parse_qs(parsed.query)
                with session_scope() as session:
                    self._send_json(
                        _knowledge_payload(
                            session,
                            book_id=_int_query(query, "book_id", 0),
                            chapter_number=_int_query(query, "chapter_number", 1),
                        )
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
    if action == "pause_queue_task":
        task = pause_generation_queue_task(session, task_id=int(payload.get("task_id") or 0), reason="dashboard")
        return {"status": task.status, "generation_task_id": task.id}
    if action == "resume_queue_task":
        task = resume_generation_queue_task(session, task_id=int(payload.get("task_id") or 0))
        return {"status": task.status, "generation_task_id": task.id}
    if action == "cancel_queue_task":
        task = cancel_generation_queue_task(session, task_id=int(payload.get("task_id") or 0), reason="dashboard")
        return {"status": task.status, "generation_task_id": task.id}
    if action == "retry_queue_task":
        task = retry_generation_queue_task(session, task_id=int(payload.get("task_id") or 0))
        return {"status": task.status, "generation_task_id": task.id}
    if action == "record_feedback":
        feedback = record_platform_feedback(
            session,
            book_id=int(payload.get("book_id") or 0),
            chapter_number=int(payload.get("chapter_number") or 0) or None,
            platform=str(payload.get("platform") or "manual"),
            metric_name=str(payload.get("metric_name") or "comment"),
            metric_value=str(payload.get("metric_value") or ""),
            raw_text=str(payload.get("raw_text") or ""),
        )
        return {"status": "recorded", "feedback_id": feedback.id}
    if action == "create_feedback_adjustment":
        adjustment = create_feedback_adjustment(
            session,
            book_id=int(payload.get("book_id") or 0),
            target_chapter_number=int(payload.get("target_chapter_number") or 0),
            feedback_ids=_parse_feedback_ids(payload.get("feedback_ids")),
            adjustment_text=str(payload.get("adjustment_text") or ""),
        )
        brief_id = None
        if bool(payload.get("apply_to_brief", True)):
            brief = apply_feedback_adjustment_to_brief(session, adjustment_id=adjustment.id)
            brief_id = brief.id
        return {"status": "created", "feedback_adjustment_id": adjustment.id, "brief_id": brief_id}
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


def _chapter_detail(session, *, book_id: int, chapter_number: int) -> dict:
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if not chapter:
        return {"chapter": None, "latest_brief": None, "latest_quality": None, "versions": [], "generation_tasks": []}
    latest_brief = session.scalar(select(ChapterBrief).where(ChapterBrief.chapter_id == chapter.id).order_by(ChapterBrief.id.desc()))
    versions = list(
        session.scalars(
            select(ChapterVersion).where(ChapterVersion.chapter_id == chapter.id).order_by(ChapterVersion.id.desc()).limit(8)
        )
    )
    latest_version = versions[0] if versions else None
    latest_quality = (
        session.scalar(
            select(QualityReport)
            .where(QualityReport.chapter_version_id == latest_version.id)
            .order_by(QualityReport.id.desc())
        )
        if latest_version
        else None
    )
    tasks = _generation_tasks_for_chapter(session, book_id=book_id, chapter_number=chapter_number, limit=8)
    return {
        "chapter": {
            "id": chapter.id,
            "number": chapter.chapter_number,
            "title": chapter.title,
            "status": chapter.status,
            "summary": chapter.summary,
        },
        "latest_brief": _brief_payload(latest_brief),
        "latest_quality": _quality_payload(latest_quality),
        "versions": [
            {
                "id": version.id,
                "version_number": version.version_number,
                "title": version.title,
                "status": version.status,
                "source": version.source,
                "content_chars": len(version.content),
            }
            for version in versions
        ],
        "generation_tasks": tasks,
    }


def _feedback_payload(session, *, book_id: int) -> dict:
    summary = summarize_platform_feedback(session, book_id=book_id)
    feedback_items = list_platform_feedback(session, book_id=book_id, limit=20)
    adjustments = list_feedback_adjustments(session, book_id=book_id, limit=20)
    return {
        "summary": {
            "total": summary.total,
            "by_metric": summary.by_metric,
            "by_platform": summary.by_platform,
        },
        "items": [
            {
                "id": item.id,
                "chapter_id": item.chapter_id,
                "platform": item.platform,
                "metric_name": item.metric_name,
                "metric_value": item.metric_value,
                "raw_text": item.raw_text,
            }
            for item in feedback_items
        ],
        "adjustments": [
            {
                "id": item.id,
                "target_chapter_number": item.target_chapter_number,
                "feedback_ids": item.feedback_ids,
                "status": item.status,
                "adjustment_text": item.adjustment_text,
            }
            for item in adjustments
        ],
    }


def _knowledge_payload(session, *, book_id: int, chapter_number: int) -> dict:
    book = session.get(Book, book_id)
    if not book:
        raise ValueError(f"book not found: {book_id}")
    story_context, story_refs = format_story_control_context(session, book_id=book_id, chapter_number=chapter_number)
    canon_context, canon_refs = format_canon_context(session, book_id=book_id, chapter_number=chapter_number)
    evidence_context, signal_ids = format_market_evidence_context(session, genre=book.genre)
    bible = get_story_bible(session, book_id=book_id)
    return {
        "story_bible": {"id": bible.id, "status": bible.status} if bible else None,
        "story_refs": story_refs,
        "canon_refs": canon_refs,
        "story_context": story_context,
        "canon_context": canon_context,
        "evidence_context": evidence_context,
        "market_signal_ids": signal_ids,
        "evidence_audit": [
            {
                "signal_id": item.signal_id,
                "usable": item.usable,
                "reasons": item.reasons,
                "source": item.source_key,
                "signal": item.signal_text,
            }
            for item in audit_market_evidence(session, genre=book.genre)
        ],
    }


def _generation_tasks_for_chapter(session, *, book_id: int, chapter_number: int, limit: int) -> list[dict]:
    rows: list[dict] = []
    tasks = session.scalars(select(GenerationTask).where(GenerationTask.book_id == book_id).order_by(GenerationTask.id.desc()))
    for task in tasks:
        input_data = _loads_json(task.input_json)
        if input_data.get("chapter_number") != chapter_number:
            continue
        output_data = _loads_json(task.output_json)
        rows.append(
            {
                "id": task.id,
                "type": task.task_type,
                "status": task.status,
                "attempt": input_data.get("attempt") or output_data.get("attempt"),
                "error_category": output_data.get("error_category", ""),
            }
        )
        if len(rows) >= limit:
            break
    return rows


def _brief_payload(brief: ChapterBrief | None) -> dict | None:
    if not brief:
        return None
    return {
        "id": brief.id,
        "goal": brief.goal,
        "required_beats": brief.required_beats,
        "constraints": brief.constraints,
        "status": brief.status,
    }


def _quality_payload(quality: QualityReport | None) -> dict | None:
    if not quality:
        return None
    return {
        "id": quality.id,
        "score": quality.score,
        "passed": quality.passed,
        "report": quality.report,
    }


def _parse_feedback_ids(value) -> list[int]:
    if isinstance(value, list):
        return [int(item) for item in value]
    return [int(item.strip()) for item in str(value or "").split(",") if item.strip()]


def _loads_json(value: str) -> dict:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {"raw": value}
    return data if isinstance(data, dict) else {"value": data}


if __name__ == "__main__":
    raise SystemExit(main())
