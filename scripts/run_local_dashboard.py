from __future__ import annotations

import argparse
import difflib
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
from app.models.entities import (
    Book,
    Chapter,
    ChapterBrief,
    ChapterVersion,
    GenerationTask,
    PublishExecution,
    PublishJob,
    PublishingTarget,
    QualityReport,
)
from app.services.canon import format_canon_context
from app.services.dashboard import build_project_snapshot
from app.services.db_ops import (
    check_database_health,
    check_schema_version,
    create_database_backup,
    list_database_backups,
    restore_database_from_backup,
)
from app.services.evidence import audit_market_evidence, format_market_evidence_context
from app.services.feedback import (
    apply_feedback_adjustment_to_brief,
    create_feedback_adjustment,
    list_feedback_adjustments,
    list_platform_feedback,
    record_platform_feedback,
    summarize_platform_feedback,
)
from app.services.llm_audit import llm_failure_suggestion, list_llm_request_logs, summarize_llm_failures, summarize_llm_usage
from app.services.llm_costs import summarize_llm_cost
from app.services.llm_queue import (
    QUEUE_TYPES,
    build_generation_queue_health,
    cancel_generation_queue_task,
    pause_generation_queue_task,
    resume_generation_queue_task,
    retry_generation_queue_task,
    run_generation_queue,
)
from app.services.planning import AUTO_ACTIONS, run_next_action
from app.services.production import (
    approve_chapter,
    execute_publish_job,
    publish_job_dry_run,
    queue_publish_job,
    retry_publish_job,
    upsert_publishing_target,
)
from app.services.continuity import record_chapter_continuity
from app.services.story import format_story_control_context, get_story_bible


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI 小说生产系统 v2</title>
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
    .diff {
      background: #111827;
      color: #e5e7eb;
      max-height: 360px;
      overflow: auto;
    }
    .diff .add { color: #86efac; }
    .diff .del { color: #fca5a5; }
    .diff .meta { color: #93c5fd; }
    .chips { display: flex; flex-wrap: wrap; gap: 6px; padding: 10px 14px; border-top: 1px solid var(--line); }
    .chip { border: 1px solid var(--line); border-radius: 999px; padding: 4px 8px; font-size: 12px; background: #fff; }
    .actions button.secondary { background: #ffffff; color: var(--ink); border-color: var(--line); }
    @media (max-width: 900px) {
      .toolbar, .summary, .grid, .forms { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <h1>AI 小说生产系统 v2</h1>
    <div id="state" class="muted">加载中</div>
  </header>
  <main>
    <section class="toolbar">
      <label>作品<select id="book"></select></label>
      <label>起始章<input id="start" type="number" min="1" value="1"></label>
      <label>章数<input id="count" type="number" min="1" value="20"></label>
      <label>当前章<input id="chapter" type="number" min="1" value="1"></label>
      <button id="refresh">刷新</button>
    </section>
    <section class="summary" id="summary"></section>
    <section class="panel full">
      <h2>当前章生产向导</h2>
      <div class="forms">
        <label>发布平台<input id="wizardPlatform" value="番茄小说"></label>
        <label>最多推进步数<input id="wizardMaxSteps" type="number" min="1" max="10" value="5"></label>
        <label>模式<select id="wizardDryRun"><option value="false">真实生成</option><option value="true">安全演示</option></select></label>
        <button id="runWizard">推进到下一人工点</button>
        <button id="approveWizard">审批当前章</button>
        <label style="grid-column: 1 / -1;">连续性摘要<textarea id="continuitySummary" placeholder="质检通过后，写一句本章发生了什么；留空则系统自动生成简短摘要。"></textarea></label>
        <button id="recordContinuity">记录连续性</button>
      </div>
    </section>
    <section class="grid">
      <div class="stack">
        <section class="panel">
          <h2>章节列表</h2>
          <div id="chapters"></div>
        </section>
        <section class="panel">
          <h2>人工决策</h2>
          <div id="decisions"></div>
        </section>
      </div>
      <div class="stack">
        <section class="panel">
          <h2>队列健康</h2>
          <div id="queue"></div>
        </section>
        <section class="panel">
          <h2>生产就绪</h2>
          <div id="readiness"></div>
        </section>
        <section class="panel">
          <h2>LLM 用量与成本</h2>
          <div id="llmUsage"></div>
        </section>
        <section class="panel">
          <h2>失败任务处理</h2>
          <div id="failedTasks"></div>
        </section>
        <section class="panel">
          <h2>下一步建议</h2>
          <div id="recommendation" class="empty"></div>
          <div class="actions">
            <button id="runQueue">运行一次队列</button>
            <button id="runNext" class="secondary">执行安全下一步</button>
          </div>
        </section>
      </div>
    </section>
    <section class="panel full">
      <h2>章节详情</h2>
      <div id="chapterDetail"></div>
    </section>
    <section class="panel full">
      <h2>读者反馈</h2>
      <div id="feedback"></div>
      <div class="forms">
        <label>平台<input id="feedbackPlatform" value="manual"></label>
        <label>指标<input id="feedbackMetric" value="comment"></label>
        <label>数值<input id="feedbackValue" value=""></label>
        <label>目标章<input id="feedbackTarget" type="number" min="1" value="1"></label>
        <button id="recordFeedback">记录反馈</button>
        <label style="grid-column: 1 / -1;">反馈原文<textarea id="feedbackRaw"></textarea></label>
        <label>反馈 ID<input id="adjustmentFeedbackIds" placeholder="1,2"></label>
        <label>调整目标章<input id="adjustmentTarget" type="number" min="1" value="1"></label>
        <label style="grid-column: span 2;">调整内容<input id="adjustmentText"></label>
        <button id="createAdjustment">创建调整</button>
      </div>
    </section>
    <section class="panel full">
      <h2>发布配置与任务</h2>
      <div id="publishing"></div>
      <div class="forms">
        <label>平台<input id="publishTargetPlatform" value="manual"></label>
        <label>账号标签<input id="publishTargetAccount" value=""></label>
        <label>作品标识<input id="publishTargetWork" value=""></label>
        <label>自动化模式<input id="publishTargetMode" value="manual"></label>
        <button id="savePublishTarget">保存发布目标</button>
        <label style="grid-column: 1 / -1;">配置 JSON<textarea id="publishTargetConfig">{}</textarea></label>
      </div>
    </section>
    <section class="panel full">
      <h2>数据库安全</h2>
      <div id="databaseOps"></div>
      <div class="forms">
        <label>备份标签<input id="databaseBackupLabel" value="dashboard"></label>
        <button id="createDatabaseBackup">创建备份</button>
        <label style="grid-column: 1 / -1;">恢复备份路径<input id="databaseRestorePath" placeholder="data/backups/example.db"></label>
        <button id="restoreDatabase">恢复数据库</button>
      </div>
    </section>
    <section class="panel full">
      <h2>知识上下文</h2>
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
      else $('state').textContent = '暂无作品';
    }

    async function refresh() {
      const bookId = $('book').value;
      if (!bookId) return;
      $('state').textContent = '刷新中';
      const params = new URLSearchParams({book_id: bookId, start: $('start').value, count: $('count').value});
      const chapterParams = new URLSearchParams({book_id: bookId, chapter_number: $('chapter').value});
      const [snapshot, health] = await Promise.all([
        fetchJson('/api/snapshot?' + params.toString()),
        fetchJson('/api/queue-health')
      ]);
      const [detail, feedback, knowledge, llmUsage, failedTasks, publishing, databaseOps] = await Promise.all([
        fetchJson('/api/chapter-detail?' + chapterParams.toString()),
        fetchJson('/api/feedback?book_id=' + encodeURIComponent(bookId)),
        fetchJson('/api/knowledge?' + chapterParams.toString()),
        fetchJson('/api/llm-usage?book_id=' + encodeURIComponent(bookId)),
        fetchJson('/api/failed-tasks?book_id=' + encodeURIComponent(bookId)),
        fetchJson('/api/publishing?book_id=' + encodeURIComponent(bookId)),
        fetchJson('/api/database')
      ]);
      currentSnapshot = snapshot;
      renderSummary(snapshot, health);
      renderChapters(snapshot.chapters);
      renderDecisions(snapshot.human_decisions.items);
      renderQueue(snapshot, health);
      renderReadiness(snapshot.readiness);
      renderLLMUsage(llmUsage);
      renderFailedTasks(failedTasks);
      renderChapterDetail(detail);
      renderFeedback(feedback);
      renderPublishing(publishing);
      renderDatabaseOps(databaseOps);
      renderKnowledge(knowledge);
      $('recommendation').innerHTML = `<div class="command">${escapeHtml(snapshot.recommendation)}</div>`;
      $('state').textContent = `已更新 ${new Date().toLocaleTimeString()}`;
    }

    async function fetchJson(path) {
      const response = await fetch(path);
      if (!response.ok) throw new Error(await response.text());
      return response.json();
    }

    async function postAction(action, payload = {}) {
      $('state').textContent = `执行中：${actionLabel(action)}`;
      const response = await fetch('/api/action', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({action, ...payload})
      });
      if (!response.ok) throw new Error(await response.text());
      const result = await response.json();
      $('state').textContent = `${actionLabel(action)}：${statusLabel(result.status || 'done')}`;
      await refresh();
    }

    function renderSummary(snapshot, health) {
      const queueCounts = health.counts || {};
      const decisions = snapshot.human_decisions;
      $('summary').innerHTML = [
        metric('就绪状态', snapshot.readiness.passed ? '通过' : '阻塞', snapshot.readiness.passed ? 'ok' : 'bad'),
        metric('待执行', queueCounts.pending || 0, 'warn'),
        metric('失败', queueCounts.failed || 0, (queueCounts.failed || 0) ? 'bad' : 'ok'),
        metric('Token', snapshot.generation_recent.estimated_tokens || 0, ''),
        metric('人工事项', decisions.continuity + decisions.approval + decisions.publish + decisions.inspect, 'warn')
      ].join('');
    }

    function metric(label, value, cls) {
      return `<div class="metric"><span>${label}</span><strong class="${cls}">${value}</strong></div>`;
    }

    function renderChapters(chapters) {
      if (!chapters.length) return empty('chapters');
      $('chapters').innerHTML = table(['章', '版本状态', '质检', '下一步', '原因'], chapters.map((item) => [
        `<button class="secondary" data-select-chapter="${item.number}">${item.number}</button>`,
        escapeHtml(statusLabel(item.version_status || 'missing')),
        escapeHtml(qualityLabel(item.quality_passed)),
        escapeHtml(actionLabel(item.next_action)),
        escapeHtml(item.reason)
      ]), true);
    }

    function renderDecisions(items) {
      if (!items.length) return empty('decisions');
      $('decisions').innerHTML = table(['类型', '章节', '原因', '命令'], items.map((item) => [
        decisionLabel(item.type),
        item.chapter,
        item.reason,
        `<span class="command">${escapeHtml(item.command_hint)}</span>`
      ]), true);
    }

    function renderQueue(snapshot, health) {
      const rows = Object.entries(health.counts || {}).map(([name, count]) => [name, count]);
      const runningRows = (health.running_tasks || []).map((item) => [
        `运行 #${item.task_id}`,
        item.chapter_number || '',
        `${item.running_age_seconds || 0}s`,
        `${item.timeout_seconds || 0}s`,
        item.stale ? '<span class="bad">已超时</span>' : '<span class="ok">运行中</span>',
        item.recoverable ? '是' : '否'
      ]);
      const failureRows = (health.latest_failures || []).map((item) => [
        `失败 #${item.task_id}`,
        item.chapter_number || '',
        errorLabel(item.error_category || ''),
        item.error || ''
      ]);
      const taskRows = (snapshot.generation_queue.tasks || []).map((item) => [
        escapeHtml(`任务 #${item.id}`),
        escapeHtml(item.chapter || ''),
        escapeHtml(statusLabel(item.status)),
        escapeHtml(modelParamLabel(item.llm_parameters || {})),
        escapeHtml(item.status === 'running' ? `${item.running_age_seconds || 0}s / ${item.timeout_seconds || 0}s` : errorLabel(item.error_category || '')),
        queueButtons(item)
      ]);
      $('queue').innerHTML =
        table(['状态', '数量'], rows.map(([name, count]) => [statusLabel(name), count])) +
        table(['运行任务', '章节', '已运行', '超时阈值', '状态', '可恢复'], runningRows, true) +
        table(['最近失败', '章节', '类型', '详情'], failureRows) +
        table(['任务', '章节', '状态', '模型参数', '运行/详情', '操作'], taskRows, true);
    }

    function modelParamLabel(params) {
      if (!params || !Object.keys(params).length) return '';
      return `${params.provider_mode || ''} ${params.requested_model || ''} max=${params.max_tokens || ''} temp=${params.temperature ?? ''}`;
    }

    function queueButtons(item) {
      const buttons = [];
      if (item.status === 'pending') {
        buttons.push(actionButton('暂停', 'pause_queue_task', item.id));
        buttons.push(actionButton('取消', 'cancel_queue_task', item.id));
      } else if (item.status === 'paused') {
        buttons.push(actionButton('恢复', 'resume_queue_task', item.id));
        buttons.push(actionButton('取消', 'cancel_queue_task', item.id));
      } else if (item.status === 'failed') {
        buttons.push(actionButton('重试', 'retry_queue_task', item.id));
        buttons.push(actionButton('取消', 'cancel_queue_task', item.id));
      }
      return `<div class="actions">${buttons.join('')}</div>`;
    }

    function actionButton(label, action, taskId) {
      return `<button class="secondary" data-action="${action}" data-task-id="${taskId}">${label}</button>`;
    }

    function renderReadiness(readiness) {
      $('readiness').innerHTML = table(['检查项', '结果', '详情'], readiness.checks.map((item) => [
        readinessLabel(item.name),
        `<span class="${item.passed ? 'ok' : 'bad'}">${item.passed ? '通过' : '未通过'}</span>`,
        item.detail
      ]), true);
    }

    function renderLLMUsage(payload) {
      const usage = payload.usage;
      const cost = payload.cost;
      $('llmUsage').innerHTML =
        table(['指标', '值'], [
          ['请求数', usage.request_count],
          ['完成', usage.completed_count],
          ['失败', usage.failed_count],
          ['估算 Token', usage.estimated_total_tokens],
          ['实际 Token', usage.actual_total_tokens],
          ['计费 Token', usage.billable_total_tokens],
          ['输入 Token', usage.billable_prompt_tokens],
          ['输出 Token', usage.billable_response_tokens],
          ['估算成本', `${cost.estimated_cost} ${cost.currency}`],
          ['模型', cost.model]
        ]) +
        table(['请求', '类型', '状态', '模型', 'Token', '耗时'], payload.recent_requests.map((item) => [
          item.id,
          taskTypeLabel(item.task_type),
          statusLabel(item.status),
          item.model,
          item.actual_total_tokens || item.estimated_total_tokens,
          `${item.elapsed_ms} ms`
        ]));
    }

    function renderFailedTasks(payload) {
      const counts = Object.entries(payload.by_error_category || {}).map(([name, count]) => [errorLabel(name), count]);
      const llmRows = (payload.llm_failures || []).map((item) => [
        errorLabel(item.error_category),
        item.count,
        item.latest_request_id,
        taskTypeLabel(item.latest_task_type),
        item.latest_model,
        item.suggestion
      ]);
      const rows = payload.items.map((item) => [
        item.id,
        taskTypeLabel(item.task_type),
        item.chapter_number || '',
        errorLabel(item.error_category || ''),
        item.error || '',
        item.is_queue_task ? queueFailureButtons(item) : '<span class="muted">查看任务详情</span>'
      ]);
      $('failedTasks').innerHTML =
        table(['错误类型', '数量'], counts) +
        table(['LLM 错误', '次数', '最近请求', '类型', '模型', '处理建议'], llmRows) +
        table(['任务', '类型', '章节', '错误', '详情', '操作'], rows, true);
    }

    function queueFailureButtons(item) {
      return `<div class="actions">${actionButton('重试', 'retry_queue_task', item.id)}${actionButton('取消', 'cancel_queue_task', item.id)}</div>`;
    }

    function renderChapterDetail(detail) {
      if (!detail.chapter) {
        $('chapterDetail').innerHTML = '<div class="empty">章节不存在</div>';
        return;
      }
      const brief = detail.latest_brief;
      const quality = detail.latest_quality;
      const qualityData = quality?.data || null;
      const llmReview = qualityData?.llm_review || null;
      $('chapterDetail').innerHTML =
        table(['字段', '值'], [
          ['章节 ID', detail.chapter.id],
          ['章节号', detail.chapter.number],
          ['状态', statusLabel(detail.chapter.status)],
          ['最新 Brief', brief ? `#${brief.id} ${statusLabel(brief.status)}` : ''],
          ['最新质检', quality ? `${quality.passed ? '通过' : '未通过'} 分数=${quality.score}` : '']
        ]) +
        (brief ? `<pre>${escapeHtml(['目标：' + brief.goal, '必要节拍：' + brief.required_beats, '硬约束：' + brief.constraints].join('\n'))}</pre>` : '') +
        renderQualityDetail(qualityData) +
        renderLLMReview(llmReview) +
        renderVersionDiff(detail.version_diff) +
        table(['版本', '状态', '来源', '字数', '标题'], detail.versions.map((item) => [
          item.id,
          statusLabel(item.status),
          item.source,
          item.content_chars,
          item.title
        ])) +
        table(['任务', '类型', '状态', '尝试', '错误'], detail.generation_tasks.map((item) => [
          item.id,
          taskTypeLabel(item.type),
          statusLabel(item.status),
          item.attempt || '',
          errorLabel(item.error_category || '')
        ]));
    }

    function renderQualityDetail(data) {
      if (!data) return '<div class="empty">暂无质检报告</div>';
      const dimensions = Object.entries(data.dimensions || {}).sort((a, b) => a[0].localeCompare(b[0]));
      const issues = data.issues || [];
      return '<h2>质检报告</h2>' +
        table(['状态', '分数', '中文字数'], [[qualityStatusLabel(data.status || ''), data.score ?? '', data.chinese_chars ?? '']]) +
        table(['维度', '分数'], dimensions.map(([name, score]) => [
          escapeHtml(dimensionLabel(name)),
          `<span class="${score < 50 ? 'bad' : score < 70 ? 'warn' : 'ok'}">${score}</span>`
        ]), true) +
        chips('问题', issues);
    }

    function renderLLMReview(review) {
      if (!review) return '<div class="empty">暂无 LLM 二审结果</div>';
      return '<h2>LLM 二审</h2>' +
        table(['字段', '值'], [
          ['状态', statusLabel(review.status || '')],
          ['结论', verdictLabel(review.verdict || '')],
          ['分数', review.score ?? ''],
          ['供应商', review.provider || ''],
          ['模型', review.model || ''],
          ['任务', review.generation_task_id || '']
        ]) +
        chips('优点', review.strengths || []) +
        chips('二审问题', review.issues || []) +
        chips('修订建议', review.revision_suggestions || []) +
        chips('风险标记', review.risk_flags || []);
    }

    function renderVersionDiff(diff) {
      if (!diff || !diff.text) return '<div class="empty">暂无版本对比</div>';
      return '<h2>最新版本对比</h2>' +
        table(['旧版本', '新版本'], [[`#${diff.left_version_id}`, `#${diff.right_version_id}`]]) +
        `<pre class="diff">${formatDiff(diff.text)}</pre>`;
    }

    function formatDiff(text) {
      return escapeHtml(text).split('\n').map((line) => {
        if (line.startsWith('+') && !line.startsWith('+++')) return `<span class="add">${line}</span>`;
        if (line.startsWith('-') && !line.startsWith('---')) return `<span class="del">${line}</span>`;
        if (line.startsWith('@@') || line.startsWith('---') || line.startsWith('+++')) return `<span class="meta">${line}</span>`;
        return line;
      }).join('\n');
    }

    function chips(title, items) {
      const values = (items || []).map((item) => `<span class="chip">${escapeHtml(item)}</span>`).join('');
      return `<div class="chips"><strong>${escapeHtml(title)}</strong>${values || '<span class="muted">无</span>'}</div>`;
    }

    function renderFeedback(payload) {
      $('feedbackTarget').value = $('chapter').value;
      $('adjustmentTarget').value = $('chapter').value;
      $('feedback').innerHTML =
        table(['指标', '数量'], Object.entries(payload.summary.by_metric || {})) +
        table(['反馈', '平台', '指标', '数值', '原文'], payload.items.map((item) => [
          `<button class="secondary" data-add-feedback-id="${item.id}">${item.id}</button>`,
          escapeHtml(item.platform),
          escapeHtml(item.metric_name),
          escapeHtml(item.metric_value),
          escapeHtml(item.raw_text)
        ]), true) +
        table(['调整', '目标章', '状态', '反馈', '内容'], payload.adjustments.map((item) => [
          item.id,
          item.target_chapter_number,
          statusLabel(item.status),
          item.feedback_ids,
          item.adjustment_text
        ]));
    }

    function renderPublishing(payload) {
      $('publishing').innerHTML =
        table(['目标', '平台', '账号', '作品', '模式', '状态'], payload.targets.map((item) => [
          item.id,
          item.platform,
          item.account_label,
          item.work_identifier,
          item.automation_mode,
          statusLabel(item.status)
        ])) +
        table(['任务', '版本', '章节', '平台', '状态', '预览', '操作'], payload.jobs.map((item) => [
          item.id,
          item.version_id,
          item.chapter_number || '',
          item.platform,
          statusLabel(item.status),
          `<details><summary>预览</summary><pre>${escapeHtml(item.preview.title + '\n字数：' + item.preview.content_chars + '\n\n' + item.preview.content_excerpt)}</pre><pre>${escapeHtml(item.result_report || '')}</pre></details>`,
          publishButtons(item)
        ]), true) +
        table(['执行', '任务', '平台', '状态', '模式', '报告'], payload.executions.map((item) => [
          item.id,
          item.publish_job_id,
          item.platform,
          statusLabel(item.status),
          item.automation_mode,
          item.report
        ]));
    }

    function publishButtons(item) {
      const buttons = [];
      if (item.status === 'pending') buttons.push(actionButton('发布干跑', 'publish_dry_run', item.id));
      if (item.status === 'dry_run_ready') buttons.push(actionButton('发布入队', 'queue_publish_job', item.id));
      if (item.status === 'queued') {
        buttons.push(actionButton('确认检查', 'execute_publish_job_blocked', item.id));
        buttons.push(actionButton('确认发布', 'execute_publish_job_confirm', item.id));
      }
      if (item.status === 'failed') buttons.push(actionButton('重试发布', 'retry_publish_job', item.id));
      return `<div class="actions">${buttons.join('')}</div>`;
    }

    function renderDatabaseOps(payload) {
      const health = payload.health;
      const schema = payload.schema_version;
      $('databaseOps').innerHTML =
        table(['检查项', '值'], [
          ['数据库地址', health.database_url],
          ['SQLite 文件', health.sqlite_path],
          ['数据表数量', health.table_count],
          ['迁移脚本数量', health.migration_count],
          ['最新迁移', health.latest_migration],
          ['备份数量', health.backup_count]
        ]) +
        table(['Schema', '值'], [
          ['状态', statusLabel(schema.status)],
          ['当前版本', schema.current_versions.join(',')],
          ['代码期望版本', schema.expected_head],
          ['说明', schema.message]
        ]) +
        table(['备份', '状态', '大小', '路径', '报告'], payload.backups.map((item) => [
          `<button class="secondary" data-use-backup-path="${escapeHtml(item.backup_path)}">#${item.id}</button>`,
          statusLabel(item.status),
          formatBytes(item.size_bytes),
          `<span class="command">${escapeHtml(item.backup_path)}</span>`,
          escapeHtml(item.report)
        ]), true);
    }

    function renderKnowledge(payload) {
      $('knowledge').innerHTML =
        table(['故事圣经', '状态'], [[payload.story_bible?.id || '', statusLabel(payload.story_bible?.status || 'missing')]]) +
        `<pre>${escapeHtml('故事上下文\n' + payload.story_context + '\n\nCanon 上下文\n' + payload.canon_context + '\n\n市场证据\n' + payload.evidence_context)}</pre>` +
        table(['信号', '可用', '原因', '来源'], payload.evidence_audit.map((item) => [
          item.signal_id,
          item.usable ? '是' : '否',
          item.reasons.join(',') || '可用',
          item.source
        ]));
    }

    function table(headers, rows, trusted = false) {
      const head = headers.map((item) => `<th>${escapeHtml(String(item))}</th>`).join('');
      const body = rows.map((row) => `<tr>${row.map((item) => `<td>${trusted ? item : escapeHtml(String(item ?? ''))}</td>`).join('')}</tr>`).join('');
      return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
    }

    function empty(id) {
      $(id).innerHTML = '<div class="empty">暂无数据</div>';
    }

    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, (char) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;'}[char]));
    }

    const STATUS_LABELS = {
      active: '启用',
      approved: '已审批',
      applied: '已应用',
      blocked: '阻塞',
      canceled: '已取消',
      created: '已创建',
      completed: '已完成',
      done: '完成',
      draft: '草稿',
      dry_run_ready: '干跑通过',
      executed: '已执行',
      failed: '失败',
      missing: '缺失',
      needs_revision: '需修订',
      ok: '正常',
      no_version: '无版本',
      pass: '通过',
      paused: '已暂停',
      pending: '待执行',
      planned: '已规划',
      planning: '规划中',
      published: '已发布',
      queued: '已入队',
      ready: '就绪',
      recorded: '已记录',
      reviewed_pass: '质检通过',
      restored: '已恢复',
      revision_ready: '修订就绪',
      running: '运行中',
      saved: '已保存',
      ahead_or_diverged: '版本异常',
      current: '当前最新',
      current_with_extra_heads: '当前含额外分支',
      behind: '落后',
      no_migrations: '无迁移',
      unversioned: '未版本化'
    };
    const ACTION_LABELS = {
      approve_chapter: '人工审批章节',
      backup_database: '创建数据库备份',
      cancel_queue_task: '取消队列任务',
      create_chapter_brief: '创建章节 Brief',
      create_feedback_adjustment: '创建反馈调整',
      create_publish_job: '创建发布任务',
      create_revision_brief: '创建修订 Brief',
      draft_chapter: '生成章节草稿',
      mark_publish_job: '确认发布结果',
      pause_queue_task: '暂停队列任务',
      publish_job_dry_run: '发布干跑',
      queue_publish_job: '发布入队',
      record_chapter_continuity: '回写连续性',
      record_feedback: '记录反馈',
      resume_queue_task: '恢复队列任务',
      retry_publish_job: '重试发布任务',
      retry_queue_task: '重试队列任务',
      review_chapter: '质检章节',
      revise_chapter: '修订章节',
      restore_database: '恢复数据库',
      run_next_action: '执行安全下一步',
      run_queue: '运行队列',
      done: '已完成',
      wait_generation_task: '等待生成任务'
    };
    const DECISION_LABELS = {
      continuity_writeback: '连续性回写',
      human_approval: '人工审批',
      final_publish_confirmation: '最终发布确认',
      manual_inspection: '人工检查'
    };
    const READINESS_LABELS = {
      foundation: '故事地基',
      story_bible: '故事圣经',
      evidence: '市场证据',
      canon: 'Canon',
      chapter_queue: '章节队列',
      human_decisions: '人工决策',
      llm: 'LLM 配置'
    };
    const DIMENSION_LABELS = {
      arc_alignment: '剧情段对齐',
      basic_publishability: '基础可发布性',
      brief_coverage: 'Brief 覆盖',
      canon_consistency: 'Canon 一致性',
      choice_and_cost: '选择与代价',
      conflict_pressure: '冲突压力',
      hook_strength: '章末钩子',
      platform_risk: '平台风险',
      prose_density: '文本密度',
      reader_momentum: '读者推动力',
      setting_risk: '设定风险'
    };
    const ERROR_LABELS = {
      auth: '鉴权失败',
      context_length: '上下文过长',
      execution: '执行错误',
      network: '网络错误',
      permission: '权限错误',
      provider: '供应商错误',
      rate_limit: '限流',
      structured_output: '结构化输出错误',
      timeout: '超时',
      validation: '校验错误'
    };
    function statusLabel(value) { return STATUS_LABELS[value] || value || ''; }
    function actionLabel(value) { return ACTION_LABELS[value] || value || ''; }
    function decisionLabel(value) { return DECISION_LABELS[value] || value || ''; }
    function readinessLabel(value) { return READINESS_LABELS[value] || value || ''; }
    function dimensionLabel(value) { return DIMENSION_LABELS[value] || value || ''; }
    function errorLabel(value) { return ERROR_LABELS[value] || value || ''; }
    function verdictLabel(value) { return value === 'pass' ? '通过' : value === 'needs_revision' ? '需修订' : value === 'fail' ? '失败' : value; }
    function qualityStatusLabel(value) { return value === 'PASS' ? '通过' : value === 'FAIL' ? '未通过' : value; }
    function qualityLabel(value) {
      if (value === true || value === 'True' || value === 'true') return '通过';
      if (value === false || value === 'False' || value === 'false') return '未通过';
      if (value === null || value === undefined || value === '') return '';
      return String(value);
    }

    function formatBytes(value) {
      const size = Number(value || 0);
      if (size < 1024) return `${size} B`;
      if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
      return `${(size / 1024 / 1024).toFixed(1)} MB`;
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
      const backupButton = event.target.closest('button[data-use-backup-path]');
      if (backupButton) {
        $('databaseRestorePath').value = backupButton.dataset.useBackupPath;
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
        showError(new Error('当前范围内没有可自动执行的安全动作'));
        return;
      }
      postAction('run_next_action', {book_id: currentSnapshot.book.id, chapter_number: item.number, dry_run: true, platform: $('wizardPlatform').value}).catch(showError);
    });
    $('runWizard').addEventListener('click', () => {
      if (!currentSnapshot?.book?.id) return;
      const dryRun = $('wizardDryRun').value === 'true';
      if (!dryRun && !window.confirm('确认要真实推进当前章吗？这可能会调用真实 LLM。')) return;
      postAction('run_current_until_blocked', {
        book_id: currentSnapshot.book.id,
        chapter_number: Number($('chapter').value),
        platform: $('wizardPlatform').value,
        dry_run: dryRun,
        max_steps: Number($('wizardMaxSteps').value || 5)
      }).catch(showError);
    });
    $('recordContinuity').addEventListener('click', () => {
      if (!currentSnapshot?.book?.id) return;
      postAction('record_continuity_dashboard', {
        book_id: currentSnapshot.book.id,
        chapter_number: Number($('chapter').value),
        summary: $('continuitySummary').value
      }).catch(showError);
    });
    $('approveWizard').addEventListener('click', () => {
      if (!currentSnapshot?.book?.id) return;
      if (!window.confirm('确认审批当前章最新版本吗？审批后即可创建发布任务。')) return;
      postAction('approve_current_chapter', {
        book_id: currentSnapshot.book.id,
        chapter_number: Number($('chapter').value),
        reviewer: 'dashboard'
      }).catch(showError);
    });
    document.addEventListener('click', (event) => {
      const button = event.target.closest('button[data-action]');
      if (!button) return;
      if (button.dataset.action === 'execute_publish_job_confirm') {
        if (!window.confirm('确认要执行最终发布吗？这个动作会把发布任务标记为已发布。')) return;
      }
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
    $('savePublishTarget').addEventListener('click', () => {
      postAction('upsert_publishing_target', {
        platform: $('publishTargetPlatform').value,
        account_label: $('publishTargetAccount').value,
        work_identifier: $('publishTargetWork').value,
        automation_mode: $('publishTargetMode').value,
        config_json: $('publishTargetConfig').value
      }).catch(showError);
    });
    $('createDatabaseBackup').addEventListener('click', () => {
      postAction('backup_database', {
        label: $('databaseBackupLabel').value
      }).catch(showError);
    });
    $('restoreDatabase').addEventListener('click', () => {
      const backupPath = $('databaseRestorePath').value.trim();
      if (!backupPath) {
        showError(new Error('请先填写或选择备份路径'));
        return;
      }
      if (!window.confirm('确认恢复数据库？当前数据库会先自动备份，然后被所选备份覆盖。')) return;
      postAction('restore_database', {
        backup_path: backupPath,
        confirm: true
      }).catch(showError);
    });

    function showError(error) {
      $('state').textContent = '出错';
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
                _llm_usage_payload(session, book_id=books[0].id)
                _failed_tasks_payload(session, book_id=books[0].id)
                _publishing_payload(session, book_id=books[0].id)
                _database_payload(session)
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
                            "running_count": report.running_count,
                            "stale_running_count": report.stale_running_count,
                            "running_tasks": [
                                {
                                    "task_id": item.task_id,
                                    "task_type": item.task_type,
                                    "chapter_number": item.chapter_number,
                                    "attempt": item.attempt,
                                    "max_attempts": item.max_attempts,
                                    "running_age_seconds": item.running_age_seconds,
                                    "timeout_seconds": item.timeout_seconds,
                                    "stale": item.stale,
                                    "recoverable": item.recoverable,
                                }
                                for item in report.running_tasks
                            ],
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
            if parsed.path == "/api/llm-usage":
                query = parse_qs(parsed.query)
                with session_scope() as session:
                    self._send_json(_llm_usage_payload(session, book_id=_int_query(query, "book_id", 0)))
                return
            if parsed.path == "/api/failed-tasks":
                query = parse_qs(parsed.query)
                with session_scope() as session:
                    self._send_json(_failed_tasks_payload(session, book_id=_int_query(query, "book_id", 0)))
                return
            if parsed.path == "/api/publishing":
                query = parse_qs(parsed.query)
                with session_scope() as session:
                    self._send_json(_publishing_payload(session, book_id=_int_query(query, "book_id", 0)))
                return
            if parsed.path == "/api/database":
                with session_scope() as session:
                    self._send_json(_database_payload(session))
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
            if payload.get("action") == "restore_database":
                self._send_json(_perform_restore_action(payload))
                return
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
    if action == "backup_database":
        backup = create_database_backup(session, label=str(payload.get("label") or "dashboard"))
        return {
            "status": backup.status,
            "database_backup_id": backup.id,
            "backup_path": backup.backup_path,
            "size_bytes": backup.size_bytes,
        }
    if action == "publish_dry_run":
        job = publish_job_dry_run(session, job_id=int(payload.get("task_id") or 0))
        return {"status": job.status, "publish_job_id": job.id}
    if action == "queue_publish_job":
        job = queue_publish_job(session, job_id=int(payload.get("task_id") or 0))
        return {"status": job.status, "publish_job_id": job.id}
    if action == "retry_publish_job":
        job = retry_publish_job(session, job_id=int(payload.get("task_id") or 0))
        return {"status": job.status, "publish_job_id": job.id}
    if action == "execute_publish_job_blocked":
        job, execution = execute_publish_job(session, job_id=int(payload.get("task_id") or 0), confirm=False)
        return {"status": execution.status, "publish_job_id": job.id, "publish_execution_id": execution.id}
    if action == "execute_publish_job_confirm":
        job, execution = execute_publish_job(session, job_id=int(payload.get("task_id") or 0), confirm=True)
        return {"status": execution.status, "publish_job_id": job.id, "publish_execution_id": execution.id}
    if action == "upsert_publishing_target":
        target = upsert_publishing_target(
            session,
            platform=str(payload.get("platform") or ""),
            account_label=str(payload.get("account_label") or ""),
            work_identifier=str(payload.get("work_identifier") or ""),
            automation_mode=str(payload.get("automation_mode") or "manual"),
            config_json=str(payload.get("config_json") or "{}"),
        )
        return {"status": "saved", "publishing_target_id": target.id}
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
            platform=str(payload.get("platform") or "manual"),
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
    if action == "run_current_until_blocked":
        book_id = int(payload.get("book_id") or 0)
        chapter_number = int(payload.get("chapter_number") or 0)
        max_steps = int(payload.get("max_steps") or 5)
        if max_steps < 1 or max_steps > 10:
            raise ValueError("max_steps must be between 1 and 10")
        executed = []
        for _ in range(max_steps):
            result = run_next_action(
                session,
                book_id=book_id,
                chapter_number=chapter_number,
                dry_run=bool(payload.get("dry_run", True)),
                platform=str(payload.get("platform") or "manual"),
            )
            if result.action not in AUTO_ACTIONS or result.status != "executed":
                return {"status": "blocked", "blocked_action": result.action, "message": result.message, "executed": executed}
            executed.append(
                {
                    "action": result.action,
                    "status": result.status,
                    "message": result.message,
                    "object_id": result.object_id,
                }
            )
        return {"status": "executed", "executed": executed}
    if action == "record_continuity_dashboard":
        book_id = int(payload.get("book_id") or 0)
        chapter_number = int(payload.get("chapter_number") or 0)
        summary = str(payload.get("summary") or "").strip() or _default_continuity_summary(session, book_id=book_id, chapter_number=chapter_number)
        result = record_chapter_continuity(session, book_id=book_id, chapter_number=chapter_number, summary=summary)
        return {"status": "recorded", "chapter_id": result.chapter_id}
    if action == "approve_current_chapter":
        version = _latest_version_for_chapter(
            session,
            book_id=int(payload.get("book_id") or 0),
            chapter_number=int(payload.get("chapter_number") or 0),
        )
        approved = approve_chapter(session, version_id=version.id, reviewer=str(payload.get("reviewer") or "dashboard"))
        return {"status": approved.status, "version_id": approved.id}
    raise ValueError(f"unsupported action: {action}")


def _perform_restore_action(payload: dict) -> dict:
    result = restore_database_from_backup(
        backup_path=str(payload.get("backup_path") or ""),
        confirm=bool(payload.get("confirm")),
    )
    return {
        "status": "restored",
        "database_path": result.database_path,
        "source_backup_path": result.source_backup_path,
        "pre_restore_backup_path": result.pre_restore_backup_path,
        "restored_size_bytes": result.restored_size_bytes,
    }


def _latest_version_for_chapter(session, *, book_id: int, chapter_number: int) -> ChapterVersion:
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if not chapter:
        raise ValueError("chapter not found")
    version = session.scalar(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter.id).order_by(ChapterVersion.id.desc()))
    if not version:
        raise ValueError("chapter version not found")
    return version


def _default_continuity_summary(session, *, book_id: int, chapter_number: int) -> str:
    version = _latest_version_for_chapter(session, book_id=book_id, chapter_number=chapter_number)
    excerpt = " ".join(version.content.split())[:160]
    return f"第{chapter_number}章已通过质检，最新版本《{version.title}》进入连续性记录。{excerpt}"


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
        "version_diff": _version_diff_payload(versions),
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


def _llm_usage_payload(session, *, book_id: int) -> dict:
    usage = summarize_llm_usage(session, book_id=book_id)
    cost = summarize_llm_cost(session, book_id=book_id)
    recent = list_llm_request_logs(session, book_id=book_id, limit=8)
    return {
        "usage": {
            "book_id": usage.book_id,
            "request_count": usage.request_count,
            "completed_count": usage.completed_count,
            "failed_count": usage.failed_count,
            "estimated_total_tokens": usage.estimated_total_tokens,
            "actual_total_tokens": usage.actual_total_tokens,
            "billable_prompt_tokens": usage.billable_prompt_tokens,
            "billable_response_tokens": usage.billable_response_tokens,
            "billable_total_tokens": usage.billable_total_tokens,
            "elapsed_ms": usage.elapsed_ms,
        },
        "cost": {
            "book_id": cost.book_id,
            "model": cost.model,
            "request_count": cost.request_count,
            "billable_prompt_tokens": cost.billable_prompt_tokens,
            "billable_response_tokens": cost.billable_response_tokens,
            "billable_total_tokens": cost.billable_total_tokens,
            "input_price_per_1m_tokens": cost.input_price_per_1m_tokens,
            "output_price_per_1m_tokens": cost.output_price_per_1m_tokens,
            "estimated_cost": cost.estimated_cost,
            "currency": cost.currency,
        },
        "recent_requests": [
            {
                "id": item.id,
                "generation_task_id": item.generation_task_id,
                "task_type": item.task_type,
                "status": item.status,
                "provider": item.provider,
                "model": item.model,
                "prompt_template": item.prompt_template,
                "estimated_total_tokens": item.estimated_total_tokens,
                "actual_total_tokens": item.actual_total_tokens,
                "elapsed_ms": item.elapsed_ms,
                "error_category": item.error_category,
            }
            for item in recent
        ],
    }


def _failed_tasks_payload(session, *, book_id: int) -> dict:
    tasks = list(
        session.scalars(
            select(GenerationTask)
            .where(GenerationTask.book_id == book_id, GenerationTask.status == "failed")
            .order_by(GenerationTask.id.desc())
            .limit(20)
        )
    )
    counts: dict[str, int] = {}
    advice: dict[str, str] = {}
    rows = []
    for task in tasks:
        input_data = _loads_json(task.input_json)
        output_data = _loads_json(task.output_json)
        error_category = str(output_data.get("error_category") or "")
        counts[error_category] = counts.get(error_category, 0) + 1
        advice[error_category] = llm_failure_suggestion(error_category)
        rows.append(
            {
                "id": task.id,
                "task_type": task.task_type,
                "status": task.status,
                "chapter_number": input_data.get("chapter_number"),
                "attempt": output_data.get("attempt") or input_data.get("attempt"),
                "max_attempts": output_data.get("max_attempts") or input_data.get("max_attempts"),
                "error_category": error_category,
                "error": str(output_data.get("error") or "")[:300],
                "is_queue_task": task.task_type in QUEUE_TYPES,
            }
        )
    return {
        "total": len(rows),
        "by_error_category": dict(sorted(counts.items())),
        "advice_by_error_category": {key: advice[key] for key in sorted(advice)},
        "llm_failures": [
            {
                "error_category": item.error_category,
                "count": item.count,
                "latest_request_id": item.latest_request_id,
                "latest_task_type": item.latest_task_type,
                "latest_provider": item.latest_provider,
                "latest_model": item.latest_model,
                "latest_elapsed_ms": item.latest_elapsed_ms,
                "suggestion": item.suggestion,
            }
            for item in summarize_llm_failures(session, book_id=book_id, limit=20)
        ],
        "items": rows,
    }


def _publishing_payload(session, *, book_id: int) -> dict:
    targets = list(session.scalars(select(PublishingTarget).order_by(PublishingTarget.id)))
    jobs = list(
        session.scalars(
            select(PublishJob)
            .join(ChapterVersion, ChapterVersion.id == PublishJob.chapter_version_id)
            .join(Chapter, Chapter.id == ChapterVersion.chapter_id)
            .where(Chapter.book_id == book_id)
            .order_by(PublishJob.id.desc())
            .limit(20)
        )
    )
    executions = list(
        session.scalars(
            select(PublishExecution)
            .join(PublishJob, PublishJob.id == PublishExecution.publish_job_id)
            .join(ChapterVersion, ChapterVersion.id == PublishJob.chapter_version_id)
            .join(Chapter, Chapter.id == ChapterVersion.chapter_id)
            .where(Chapter.book_id == book_id)
            .order_by(PublishExecution.id.desc())
            .limit(20)
        )
    )
    return {
        "targets": [
            {
                "id": target.id,
                "platform": target.platform,
                "account_label": target.account_label,
                "work_identifier": target.work_identifier,
                "automation_mode": target.automation_mode,
                "status": target.status,
                "config": _loads_json(target.config_json),
            }
            for target in targets
        ],
        "jobs": [_publish_job_payload(session, job) for job in jobs],
        "executions": [
            {
                "id": execution.id,
                "publish_job_id": execution.publish_job_id,
                "platform": execution.platform,
                "status": execution.status,
                "automation_mode": execution.automation_mode,
                "report": execution.report,
                "artifact_path": execution.artifact_path,
            }
            for execution in executions
        ],
    }


def _database_payload(session) -> dict:
    health = check_database_health(session)
    schema = check_schema_version(session)
    backups = list_database_backups(session, limit=20)
    return {
        "health": {
            "database_url": health.database_url,
            "sqlite_path": health.sqlite_path,
            "table_count": health.table_count,
            "tables": health.tables,
            "migration_count": health.migration_count,
            "latest_migration": health.latest_migration,
            "backup_count": health.backup_count,
        },
        "schema_version": {
            "database_url": schema.database_url,
            "current_versions": schema.current_versions,
            "expected_head": schema.expected_head,
            "status": schema.status,
            "migration_count": schema.migration_count,
            "latest_migration": schema.latest_migration,
            "message": schema.message,
        },
        "backups": [
            {
                "id": backup.id,
                "database_url": backup.database_url,
                "backup_path": backup.backup_path,
                "status": backup.status,
                "size_bytes": backup.size_bytes,
                "report": backup.report,
            }
            for backup in backups
        ],
    }


def _publish_job_payload(session, job: PublishJob) -> dict:
    version = session.get(ChapterVersion, job.chapter_version_id)
    chapter = session.get(Chapter, version.chapter_id) if version else None
    content = version.content if version else ""
    return {
        "id": job.id,
        "version_id": job.chapter_version_id,
        "chapter_number": chapter.chapter_number if chapter else None,
        "platform": job.platform,
        "status": job.status,
        "automation_payload": _loads_json(job.automation_payload),
        "result_report": job.result_report,
        "preview": {
            "title": version.title if version else "",
            "content_chars": len(content),
            "content_excerpt": content[:1200],
        },
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
    data = _loads_json(quality.report)
    return {
        "id": quality.id,
        "score": quality.score,
        "passed": quality.passed,
        "report": quality.report,
        "data": data,
    }


def _version_diff_payload(versions: list[ChapterVersion]) -> dict | None:
    if len(versions) < 2:
        return None
    right = versions[0]
    left = versions[1]
    diff = difflib.unified_diff(
        left.content.splitlines(),
        right.content.splitlines(),
        fromfile=f"version#{left.id}",
        tofile=f"version#{right.id}",
        lineterm="",
    )
    text = "\n".join(diff)
    return {
        "left_version_id": left.id,
        "right_version_id": right.id,
        "text": text,
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
