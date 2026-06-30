# 当前绿色基线代码审查与拆分提交建议

更新时间：2026-06-30

## 审查范围

当前工作区改动量较大，`git diff --stat` 显示 35 个已跟踪文件存在 diff，另有若干新增服务、脚本和 self-repair 运行产物未跟踪。重点审查了这次稳定化主线相关文件：

- `app/services/production_strategy.py`
- `app/services/planning.py`
- `app/services/rebuild_candidates.py`
- `app/services/production_kernel.py`
- `scripts/book2_production_kernel_regression.py`
- `scripts/rebuild_candidates_regression.py`
- `scripts/production_strategy_regression.py`
- `scripts/run_regressions.py`

## 安全快扫

基于当前 diff 的轻量安全扫描未发现新增：

- 明文 API key / secret / password / token 赋值
- `os.system(...)`
- `subprocess(..., shell=True)`
- `eval(...)` / `exec(...)`
- `pickle.load(s)`
- 明显 SQL 字符串格式化执行

## 已确认的绿色验证

已重新跑过关键专项：

```text
production-strategy-regression: PASS
book2-production-kernel-regression: PASS
rebuild-candidates-regression: PASS
```

`run_regressions.py` 的运行记录功能已做中断验证：人为超时 SIGTERM 后生成：

```text
data/regression_runs/20260630T214732Z-57945.json
status=INTERRUPTED
interrupted_signal=SIGTERM
current_check=worker_stability
summary.total_completed=9
```

这证明旧失败/kill 通知会被明确标记为 `INTERRUPTED`，不再和最终 `FAIL/PASS` 混淆。

## 审查结论

当前没有发现必须立即阻断的安全问题；策略层当前已由专项覆盖：

- active rebuild candidate 不再无条件压过 deadlock；
- 多候选择优稿低于历史最佳稿时会重新候选/兜底；
- 窄门禁可修时继续定点修订，不误触多候选；
- Book2 kernel 回归使用隔离快照库，并清理快照内 active queue；
- `run_regressions.py` 能为 RUNNING / INTERRUPTED / PASS / FAIL 输出 artifact。

## 主要风险点

### R1. 工作区混有大量历史改动

当前 `git status --short` 显示很多 `MM/AM/A/??` 文件，不全属于这次 1-5 收口任务。直接做一个大提交会降低可回退性。

处理建议：先不要 `git add -A`，按下面主题拆 commit。

### R2. `production_strategy.py` 优先级越来越复杂

`assess_production_strategy()` 当前把 active recovery、narrow repairable、candidate regression、active candidate、deadlock、restore loop、budget pingpong、plateau、linear exhaustion、contract conflict 等规则集中在一个函数内。专项已覆盖关键回归，但后续再加规则容易产生遮挡。

处理建议：下一阶段把策略规则拆成显式 rule list，并在 regression 中加入 rule priority matrix。

### R3. `book2_production_kernel_regression.py` 当前是测试隔离，不等于 live 队列治理完成

该脚本通过快照库和清理 active tasks 解决回归稳定性；live DB 仍需要正式的队列巡检、人工确认清理和后续 lease/heartbeat。

处理建议：本次只把 regression isolation 作为测试治理提交；live DB 队列治理单独执行 dry-run/备份后清理。


## Subagent 复审后的补充修复（2026-07-01）

异步只读复审指出 4 个高风险点，当前处理状态：

| 问题 | 状态 | 处理 |
| --- | --- | --- |
| dry-run kernel 会执行真实修订并写入多轮 dry-run version | 已修 | `ProductionKernel.step(dry_run=True)` 强制 `preview_only=True`；Book2 live dry-run 现在 `executed_count=1` 且 `status=preview`。 |
| `done` 被当作人工确认阻塞，终态语义不清 | 已修 | `done` 从 manual action 移除，kernel 返回 `status=completed` 并作为 terminal event。 |
| candidate rebuild 可能选择当前失败源稿作为 incumbent | 已修 | `_best_incumbent_draft(..., exclude_version_id=source_version.id)` 排除当前失败源稿，并补回归。 |
| rebuild candidate 生成后选择阶段异常可能遗留 `running` task | 已修 | 生成阶段和选择阶段异常统一 rollback 后重新加载 task，标记 `failed` 并 commit，补 post-generation 异常回归。 |
| persistent revision budget 统计 dry-run queue task | 已修 | `persistent_revision_budget()` 跳过 `input_json.dry_run` / `output_json.dry_run`，补 production hardening 回归。 |

补充专项验证：

```text
production-kernel-regression: PASS
rebuild-candidates-regression: PASS
production-hardening-regression: PASS
```

最终总回归 artifact：

```text
data/regression_runs/20260630T224011Z-64835.json
status=PASS
summary.total_completed=72
summary.counts.PASS=72
```

live DB 复核：

```text
PRAGMA integrity_check; -> ok
production-run-next --book-id 2 --chapter-number 2 --dry-run:
action=generate_rebuild_candidates
status=preview
executed_count=1
queue running_count=0 stale_running_count=0
```

### R4. `run_regressions.py` artifact 当前记录完整 output，可能变大

总回归失败时 JSON 会包含失败脚本完整输出；当前可接受，但未来如果输出巨大，可再做 `output_tail` / `output_path` 分离。


### Commit 1: `test: record regression run artifacts`

建议包含：

- `scripts/run_regressions.py`
- 新增但不提交具体运行产物：`data/regression_runs/*.json` 建议加入 `.gitignore` 或仅作为本地证据，不纳入版本库。

验证：

```bash
venv/bin/python -m compileall scripts/run_regressions.py
# 可用短超时或人工 SIGTERM 验证 artifact status=INTERRUPTED
```

### Commit 2: `test: isolate book2 production kernel regression from live queue state`

建议包含：

- `scripts/book2_production_kernel_regression.py`
- 与该测试直接相关的 queue/import 调整（若有）

验证：

```bash
venv/bin/python scripts/book2_production_kernel_regression.py
```

### Commit 3: `fix: stabilize production strategy priority for rebuild candidates`

建议包含：

- `app/services/production_strategy.py`
- `scripts/production_strategy_regression.py`
- `scripts/rebuild_candidates_regression.py`
- `app/services/rebuild_candidates.py` 中与 candidate floor / protected constraints 相关的改动

验证：

```bash
venv/bin/python scripts/production_strategy_regression.py
venv/bin/python scripts/rebuild_candidates_regression.py
```

### Commit 4: `fix: keep production planning from advancing past unreadable chapters`

建议包含：

- `app/services/planning.py`
- `app/services/production_kernel.py`
- `scripts/book2_production_kernel_regression.py` 中对应断言（如尚未纳入 commit 2）

验证：

```bash
venv/bin/python scripts/book2_production_kernel_regression.py
venv/bin/python scripts/system_baseline_check.py
```

### Commit 5: `fix: preserve production packet and blueprint constraints`

建议包含：

- `app/services/production_packet.py`
- `app/services/production_blueprint.py`
- `scripts/production_blueprint_regression.py`
- 风格 / Story DNA / naming governance 相关专项脚本

验证：

```bash
venv/bin/python scripts/production_blueprint_regression.py
venv/bin/python scripts/story_dna_workflow_regression.py
venv/bin/python scripts/naming_governance_regression.py
```

### Commit 6: `refactor: split dashboard boundary and production control helpers`

建议包含：

- `scripts/run_local_dashboard.py`
- `app/services/dashboard_background.py`
- `app/dashboard_knowledge_payload.py`
- `app/dashboard_skeleton_constants.py`
- `scripts/architecture_boundary_regression.py`

验证：

```bash
venv/bin/python scripts/architecture_boundary_regression.py
```

### Commit 7: `chore: docs and self-repair artifacts`

建议单独处理：

- `docs/*.md`
- `data/self_repair/*.json`
- `data/dashboard-url.txt`

建议：self-repair 运行产物通常不要混入功能提交；确认是否需要归档，否则加入 `.gitignore` 或移入运行产物目录。

## 提交前最终门禁

每组 commit 后至少跑对应专项；全部拆完后跑：

```bash
venv/bin/python scripts/run_regressions.py --skip-smoke
```

并保留 `data/regression_runs/<run_id>.json` 作为最终绿色证据。
