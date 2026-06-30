# 下一阶段生产架构计划：唯一生产总控 + Queue Lease / Heartbeat

更新时间：2026-06-30

## 背景

当前绿色基线已经解决了几类急性问题：

- live DB 中旧 `pending/running` 生成任务污染 Book2 kernel 回归；
- `production_strategy` 中 deadlock、多候选重建、active rebuild candidate、旧 revision contract 之间优先级冲突；
- `run_regressions.py` 缺少 run artifact，导致中途失败/kill 和最终成功容易混淆；
- `production-run-next --dry-run` 曾经会真实推进 dry-run 修订，直到 kernel step limit，现已改为 preview 语义。

下一阶段不应继续补丁式堆判断，而应把生产控制权、队列租约和回归隔离标准化。

## 目标 1：唯一生产总控

### 现状问题

目前有多个模块能间接影响 `next_action` 或执行生产动作：

- `ProductionKernel.step/run_until_terminal`
- `planning.plan_chapters/_plan_one/run_next_action`
- `production_strategy.assess_production_strategy`
- `production_orchestrator.decide_production_route`
- `production_decision.decide_chapter_production`
- queue worker / author runner / CLI

风险是：

1. UI/CLI 看到的 next action 与 kernel 实际执行动作不一致；
2. dry-run、preview、live execution 语义混杂；
3. 修订预算恢复、多候选重建、等待队列、人工确认的优先级被散落在多个位置；
4. 每次修 bug 都可能只修其中一条路径。

### 建议目标结构

```text
ProductionKernel
  ├─ StateSnapshotBuilder        # 只读聚合：chapter/version/quality/brief/queue/publish
  ├─ ProductionPolicy            # 纯函数：snapshot -> decision
  ├─ ActionExecutor              # 执行动作：live only
  ├─ PreviewRenderer             # preview/dry-run：不写 DB
  └─ TerminalClassifier          # queued/blocked/done/auto_paused
```

### 关键约束

- `next_action` 只能由一个 policy 层产出；
- `dry_run=True` 必须等价于 preview，不允许创建 ChapterVersion / QualityReport / Brief / GenerationTask；
- `preview_only=True` 与 `dry_run=True` 都必须走只读路径；
- 所有 heavy generation live 执行必须进入 queue，不能在请求线程内直接调用 LLM；
- 人工动作只能返回 blocked/manual_required，不能自动执行。

### 分阶段落地

#### Phase A：建立只读 Snapshot

新增或收敛为：

```python
@dataclass(frozen=True)
class ProductionSnapshot:
    book_id: int
    chapter_number: int
    chapter_id: int | None
    latest_version_id: int | None
    latest_version_status: str
    latest_quality_passed: bool | None
    active_revision_brief_id: int | None
    active_generation_task_id: int | None
    active_generation_task_status: str
    publish_job_id: int | None
    publish_job_status: str
    revision_budget_reason: str
    rebuild_candidate_state: str
```

验收：`plan_chapters` 与 `ProductionKernel.preview` 使用同一个 snapshot。

#### Phase B：收敛 Policy

把以下优先级写成单一有序表，并用回归锁死：

1. production gate blocked；
2. active generation task -> `wait_generation_task`；
3. missing chapter/brief/version 基础生产动作；
4. passed/reviewed -> publish/approve/continuity；
5. manual confirmation actions；
6. revision budget / trend recovery；
7. active rebuild candidate continuation；
8. deadlock -> generate rebuild candidates；
9. reading assessment / revision contract -> revise；
10. deferred backlog block。

验收：同一个 snapshot 在 CLI、dashboard、kernel、author runner 中得到同一个 decision。

#### Phase C：拆 ActionExecutor

`run_next_action` 现在同时承担 plan、preview、execute。建议拆为：

```python
preview_action(snapshot, decision) -> RunNextActionResult
execute_action(session, snapshot, decision) -> RunNextActionResult
```

验收：`dry_run=True` 不再调用任何会写 DB 的 service。

## 目标 2：Queue Lease / Heartbeat

### 现状问题

这次 Book2 kernel 回归失败的根因之一是旧 task 788 处于 `pending`，使章节被判断为 `wait_generation_task`。当前 queue 状态更像静态 status，缺少租约和 worker ownership。

### 建议字段

在 `generation_tasks` 增加或标准化：

```text
status: pending | leased | running | completed | failed | canceled | expired
lease_owner: str | null
lease_acquired_at: datetime | null
lease_expires_at: datetime | null
heartbeat_at: datetime | null
attempt: int
max_attempts: int
last_error: text | null
```

如果暂不迁移 schema，也可先在 `input_json/result_json` 内兼容存储 lease 信息，但最终应迁移为字段。

### 状态机

```text
pending -> leased -> running -> completed
pending -> canceled
leased/running --heartbeat timeout--> expired
expired --attempt < max_attempts--> pending
expired --attempt >= max_attempts--> failed
failed -> pending    # explicit retry only
```

### Worker 协议

1. worker 取任务时必须原子 claim：`pending where lease_expires_at is null or < now`；
2. claim 后写入 `lease_owner` 和 `lease_expires_at`；
3. 执行中定期更新 `heartbeat_at`；
4. worker 崩溃后，recovery job 根据 lease/heartbeat 释放或失败任务；
5. policy 判断 active task 时，只把 lease 未过期的 `pending/leased/running` 视为 active。

### CLI/API

保留并增强：

```bash
python -m app.cli generation-queue-health --stale-after-seconds 3600
python -m app.cli recover-stale-generation-tasks --stale-after-seconds 3600 --dry-run
python -m app.cli recover-stale-generation-tasks --stale-after-seconds 3600
python -m app.cli list-generation-queue --status pending --limit 50
```

新增建议：

```bash
python -m app.cli lease-generation-task --worker-id <id>
python -m app.cli heartbeat-generation-task --task-id <id> --worker-id <id>
python -m app.cli expire-generation-leases --stale-after-seconds 3600 --dry-run
```

## 目标 3：回归数据库隔离标准化

### 现状

`book2_production_kernel_regression.py` 已经使用隔离快照库并清理 active queue tasks，但这个模式应抽成所有 live-DB 回归共享的 fixture。

### 建议

在 `scripts/regression_db.py` 增加统一能力：

```python
isolated_database(name, *, sanitize_generation_tasks=True, sanitize_publish_jobs=False)
```

并明确：

- 回归不得直接依赖 live `data/novel.db`；
- snapshot 必须写入 `/tmp` 或 `data/regression_runs/isolated/`；
- active queue tasks 默认失效/取消；
- 每个回归输出当前 DB path，方便追查；
- live DB 检查脚本只能 read-only，除非命令名明确是 recover/cancel。

## 目标 4：运行记录与审计

`run_regressions.py` 已新增 `data/regression_runs/<run_id>.json`。下一步可补：

- latest symlink 或 `latest.json`；
- `INTERRUPTED` 与 `FAIL` 在 dashboard 中分开展示；
- 每个 check 的 stdout/stderr 可选写入单独 log 文件；
- 回归结束后打印 artifact path；
- CI 上传 artifact。

## 建议实施顺序

1. 先补 `dry_run_is_read_only` 回归：记录执行前后版本/brief/task/report 数量不变；
2. 拆 `run_next_action` 为 preview/execute；
3. 引入 `ProductionSnapshot`，让 plan/kernel/dashboard 共用；
4. 标准化 queue active 判断，过滤 expired/stale task；
5. 增加 lease/heartbeat schema migration；
6. 抽统一 `isolated_database(...sanitize_generation_tasks=True)`；
7. 把 `production_strategy_regression` 改成 snapshot/policy table-driven。

## 验收矩阵

每个阶段至少跑：

```bash
venv/bin/python scripts/production_kernel_regression.py
venv/bin/python scripts/book2_production_kernel_regression.py
venv/bin/python scripts/production_strategy_regression.py
venv/bin/python scripts/rebuild_candidates_regression.py
venv/bin/python scripts/run_regressions.py --skip-smoke
```

并做一次：

```bash
venv/bin/python -m app.cli production-run-next --book-id 2 --chapter-number 2 --dry-run
```

要求：

- 不新增版本/brief/report/task；
- `executed_count=1`；
- terminal status 为 `auto_paused` 或明确 preview 状态；
- live queue health 不出现过期 active task。
