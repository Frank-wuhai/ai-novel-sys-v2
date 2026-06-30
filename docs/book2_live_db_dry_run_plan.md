# Book2 live DB 队列清理与 dry-run 验证方案

## 当前现场

- live DB: `data/novel.db`
- Book2: `id=2`，标题《我的武侠游戏存档，正在现实同步加载》
- 当前后台队列健康：
  - `total=100`
  - `completed=98`
  - `canceled=1`
  - `pending=1`
  - `running=0`
  - `stale_running=0`
- 唯一 active blocker：
  - task `788`
  - `task_type=queue_revise_chapter`
  - `status=pending`
  - `book_id=2`
  - `chapter_number=2`
  - `dry_run=false`
  - created_at `2026-06-30 15:34:04.488301`

## 当前规划状态

`plan-chapters --book-id 2 --start 1 --count 8 --no-state-repairs` 显示：

- ch1: `next_action=mark_publish_job`，publish job queued
- ch2: 被 task 788 卡住，`next_action=wait_generation_task`
- ch3: `generate_rebuild_candidates`
- ch4: `revise_chapter`
- ch5: `generate_rebuild_candidates`
- ch6-ch8: `resolve_deferred_backlog`，因为 ch3 未通过，不允许创建下一生产段

## 清理原则

1. 先备份 `data/novel.db`。
2. 只处理 Book2 的 active queue tasks。
3. 当前没有 running task，因此不需要中止正在执行的模型任务。
4. task 788 是 stale pending blocker，清理动作应为 cancel，而不是直接删除。
5. 清理后立刻 preview / dry-run，不启动 live LLM 长任务。

## 执行命令

```bash
# 1. 备份 live DB
cp data/novel.db data/backups/pre-book2-dry-run-<UTC>.db

# 2. 取消 stale pending task
venv/bin/python -m app.cli cancel-generation-task --task-id 788 --reason "stale pending task blocks Book2 dry-run after green regression baseline"

# 3. 确认队列无 active blocker
venv/bin/python -m app.cli generation-queue-health --stale-after-seconds 3600
venv/bin/python -m app.cli list-generation-queue --status pending --limit 20

# 4. Book2 ch2 preview
venv/bin/python -m app.cli production-run-next --book-id 2 --chapter-number 2 --preview-only

# 5. Book2 ch2 dry-run
venv/bin/python -m app.cli production-run-next --book-id 2 --chapter-number 2 --dry-run

# 6. 验证计划矩阵
venv/bin/python -m app.cli plan-chapters --book-id 2 --start 1 --count 8 --no-state-repairs
```

## 预期结果

- task 788 从 `pending` 变成 `canceled`。
- ch2 不再返回 `wait_generation_task`。
- ch2 next action 应进入：
  - `revise_chapter`，或
  - `generate_rebuild_candidates`，或
  - `review_chapter`（仅当 latest draft 状态合规）。
- 不应出现：
  - 自动 `approve_chapter`；
  - 自动 `mark_publish_job`；
  - 跳过 ch2 直接创建 ch6+；
  - 因 stale task 再次 blocked。
