# Sprint 2 P1-4：45 分死锁根因修复 — 阶段总结

**日期**：2026-07-04  
**分支**：`stabilization-priority-20260701`  
**关键 commit**：`482119c`（urban intent alias）+ `e0acebd`（candidate retry）

---

## TL;DR

**修复了什么**：`intent_underfulfilled` 强制 45 分死锁，根源是**评审用抽象元语言（"外部压力/核心能力/章末"），CONCEPT_ALIASES 只有武侠词典，都市书零匹配**——导致再好的正文也命中率 33-66% < 45%，score 被压 45，无限触发 revision。

**效果**：
- **Ch1-15 dry-run**：106 个 45 分历史版本 100% unblock，0 低质泄漏
- **Ch17-20 实跑**：4/4 auto=YES，平均 15 min/章，最快 2 版本一次过关
- **回归**：12 套件全 PASS

---

## 根因分析（从表象到本质）

### 表象
- Ch4/Ch8/Ch12 频繁出现 QualityReport.score = 45、passed = False
- 触发无止境 revision loop
- Ch12 达 38 版本 / Ch8 达 60 版本 / Ch1 达 26 版本
- 单章耗时从 90 min 到 604 min 不等

### 中间假设（错的）
- ❌ "rule-flat 死锁 fallback"—— 查 `production_reviewing.py` 无此路径
- ❌ "score 计算 bug"—— dimensions 平均 74 但 QR.score = 45，看似 bug 实际是**故意压制**
- ❌ "bias.blockers 触发"—— QR 里 bias.blockers=[]，非此路径

### 真正的根因
`quality.py:316-317`：
```python
if intent.blockers:
    score = min(score, max(45, intent.score))
```

`intent_acceptance.py:147`（旧）：
```python
if points and score < 45:
    blockers.append("intent_underfulfilled")   # 覆盖率 < 45% → intent 不达标
```

**覆盖率**由 `_point_covered` 判定，走 `_coverage_units` → `CONCEPT_ALIASES` 字面 + 别名匹配。

**决定性事实**：`CONCEPT_ALIASES` **全部是武侠词汇**（江湖/门派/追兵/守谷…）。都市书 3 的 required_beats 里的评审语言（"外部压力/核心能力/章末/局面变化"）**没有一条对应到都市场景词**（工位/微信/抽屉/催款/笔记本…）。

**Ch1 v10 实测**：3 个 point 只中 1 个（HIT: 33%）→ 强制 45。

**Ch1 v10 正文明明写了**：
- 陈渡站起面对刘芸的责问（**外部压力**）
- 抽屉里出现笔记本（**核心能力显现**）
- 周磊看热闹、埋下下一步（**章末变化**）

但 point 用的是"评审元语言"，正文用的是"具体场景语言"——**字面必然失败**。

**这是分类错误**——`CONCEPT_ALIASES` 是**武侠专用词典**，`_looks_like_urban` 判定 + urban 词典 = 缺失。

---

## 修复方案（方案 A：三阶段）

### Stage-1 — book_profile 加 urban 分档
**文件**：`app/services/book_profile.py`

新增：
- `URBAN_CORE_MARKERS` / `URBAN_AVOID_MARKERS` / `URBAN_DRIFT_REPLACEMENTS`
- `URBAN_GUARD_LINES`（4 条题材护栏 prompt）
- `BookProfile.is_urban` property
- `_looks_like_urban(text)` 双信号检测：
  - 强信号：都市 / 现代都市 / 职场 / 白领 / 都市异能 / …
  - 弱信号：≥3 个都市场景 token（工位 / 电梯 / 微信 / 加班 / 房贷 / …）

### Stage-2 — URBAN_CONCEPT_ALIASES
**文件**：`app/services/intent_acceptance.py`

新增 9 组都市语义别名：
| 评审语言 | 都市场景词 |
|---|---|
| 外部压力/威胁/紧张 | 堵/逼/催/邮件/微信/催款/解雇/领导/客户/债主 |
| 主角/主动/破局/选择 | 站起来/推开/拨通/翻抽屉/掏出/决定/开口 |
| 核心能力/触发/回报 | 笔记本/字迹/预知/浮现/感应/画面/闪回 |
| 代价/承担/后果 | 忘记/记不起/裂痕/疼痛/血/透支/眩晕 |
| 章末/局面/变化 | 下一步/明天/凌晨/拉黑/挂断/追/逃/门外 |
| 命运/身边人/改变 | 同事/朋友/家人/母亲/父亲/常见人名 |
| 冲突/矛盾/张力 | 盯着/沉默/对峙/顶回去/怼/攥紧/皱眉 |
| 追读/钩子/悬念 | 究竟/为什么/陌生号码/未读消息/未接来电 |
| 画面/氛围/细节 | 阳光/键盘/抽屉/屏幕/咖啡/键盘声/呼吸声 |

**关键实现**：
- `_coverage_units(point, profile=None)` 接受传入的 profile，按 `profile.is_urban` 派发到 URBAN 词典
- `_point_covered(content, point, profile=None)` 同样透传 profile
- `evaluate_author_intent` **一次性**从完整 brief context（goal + required_beats + constraints + author_preferences + canon + content）算出 profile，传给 `_point_covered`

**为什么不在 `_coverage_units` 里现算 profile**：`_coverage_units` 只能看到**单个 point 字符串**，都市 point 里根本没有都市词（只有"外部压力"这种评审语言），推断永远走 generic 分支——**必须从上下文继承 profile**。

### Stage-3 — 剔除"剧情基线"
**文件**：`app/services/intent_acceptance.py:_marked_story_points`

**旧**：从 4 种标记抽取 points（本章剧情承诺/剧情基线各两种冒号变体）  
**新**：只保留"本章剧情承诺"

理由：**剧情基线是书级背景/风格指南**（"都市白领意外获得笔记本"），**不是本章要交付的东西**。第 17 章不应该被要求正文里再复述"主角是白领、获得了笔记本"这些设定——它们是**书级前提**，不是**章级承诺**。

历史数据显示此项独立贡献 ~10% 的 45 分误伤。

### Stage-4 — 元描述型 point 降级 —— **跳过**
Stage 1+2+3 dry-run 已 100% 解决误伤，加 Stage-4 反而过度宽松。

---

## A1：candidate retry
**文件**：`app/services/rebuild_candidates.py`

`generate_rebuild_candidates` 内层循环：每个 candidate slot 从"失败即 skip"改为**最多重试 3 次**，temperature 每次 +0.06 escape 确定性空稿状态。`_skip_reasons` 现在记录 `attempts` 字段。

真实场景：LLM 偶发结构化输出解析失败（空 body / JSON schema mismatch）—— 以前直接浪费一个 candidate 名额；现在**同一 strategy retry 出真稿**保留 diversity。

---

## 验证证据

### Dry-run（书 3 Ch1-15）
- **106** 个历史 45 分版本
- **106/106 = 100%** 修复后 `intent_underfulfilled` 消除
- **0** 个 dim_avg < 65 的低质稿被误放行
- Sanity 3 case（武侠内容 vs 都市 brief / 空稿 / 平淡都市）**全部仍被拒**

### 回归（12 套件）
- quality / rebuild_candidates / accept_early_stop_advance / production_optimization
- book2_style_flow / story_dna_workflow / production_router / production_hardening
- editorial_slack / book2_ch3 / chapter_unit_plan / production_decision / quality_gate_tiers
- **全 PASS，无回退**

### 实跑（Ch17-Ch20）
| 章 | 版本数 | 耗时 | 状态 | auto |
|---|---|---|---|---|
| Ch17 | 9 | 7 min | continuity_recorded | ✅ YES |
| Ch18 | 5 | 27 min | continuity_recorded | ✅ YES |
| Ch19 | **2** | 10 min | continuity_recorded | ✅ YES |
| Ch20 | **2** | 17 min | continuity_recorded | ✅ YES |
| **合计** | **18** | **61 min** | 4/4 通过 | 4/4 auto |

**Ch19/20 只用 2 个版本一次过关**——正是"评分器不再误伤"后期望的结果。

---

## 全书状态（Ch1-Ch20）

| 分组 | Ch | passed | auto% |
|---|---|---|---|
| 修复前（Ch1-15） | 15 章 | 12/15 = 80% | 4/6 = 67% |
| 修复后（Ch17-20） | 4 章 | 4/4 = 100% | 4/4 = 100% |
| Ch16（前一轮 kill 时已过） | 1 章 | 1/1 | 1/1 |

- **Ch10/12/15 仍 `passed=False`**：这些是 stage-9 之前的历史遗留（quality.passed 与 chapter.status 不同步问题，已在 P2 blocked 队列）
- **Ch1-9 状态多为 needs_confirmation**：早期 chapter_type_gate 结构差距 3-15 分区间，需要手工兜底

---

## 下一步

1. **Ch21+**：书 3 只规划到 Ch20，若要继续跑需先做 chapter planning
2. **Ch1-15 遗留 `passed=False`**：P2 quality.passed 同步问题的手工修复决策
3. **A2 prompt 优化**：Ch8/9 高版本数（60/26）的 prompt 层瓶颈诊断——现在 45 分 bug 修好后重新评估是否还需要
4. **性能观察点**：Ch17-20 平均 15 min/章，若持续，日更万字 × 3 本可达
5. **候选优化**（不紧急，路径 2/3 从"表层微调"角度）：
   - `drive_chapter.sh` shell 层 25 轮 × 3 个 Python 冷启动 → 合并为单进程 CLI `run-chapter-loop`
   - worker sleep 3 → 0（省 6 min/章）

---

## 关键文件与提交

- **commit `482119c`**：`app/services/book_profile.py`（+urban archetype）+ `app/services/intent_acceptance.py`（+URBAN_CONCEPT_ALIASES + profile 透传 + 剔除剧情基线）+ 4 个 s2_batch 脚本 + `docs/sprint2/p1_optimization_plan.md`
- **commit `e0acebd`**：`app/services/rebuild_candidates.py`（candidate retry 3x）
- **脚本 `scripts/s2_p14_batch_ch17_20.sh`**：本轮批跑驱动
- **日志 `logs/baseline/s2_p14_batch_20260704_124348/`**：Ch17-20 完整日志

---

## 教训与心得

1. **"表层 sleep 微调" ≠ 根源解决**——用户拦住表层方案让我深挖，找到真正的评分器语义匹配 bug
2. **同一份代码，两种题材，一个死锁**——`CONCEPT_ALIASES` 武侠限定是历史演进遗留，扩展到都市/历史/玄幻需要 profile 派发架构
3. **评审元语言 vs 具体内容语言的匹配鸿沟**是所有"字面覆盖率评分"系统的共性问题；本次解法（人工别名词典）是**中小成本高精度**方案，若未来加更多题材，可考虑升级到 embedding-based 语义匹配
4. **回归 + dry-run + 实跑** 三层验证不可省——dry-run 说 100% unblock 时也要担心过度宽松，最终 4/4 auto 才敢下结论
