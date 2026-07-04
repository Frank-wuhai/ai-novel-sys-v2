# P1 优化方案（Ch16-20 批跑完成后一起落地）

## 概览

修 Ch1-15 复盘暴露的两个可优化点：
- **A1：空稿 candidate retry**——rebuild_candidates 单个 candidate 失败后不重试就丢，浪费预算
- **A2：Ch8/Ch9 rebuild prompt 维度瓶颈**——conflict_pressure / choice_and_cost / brief_coverage / chapter_unit_flow 稳定差 3-15 分

预期效果：**auto 率从 71% 提升到 90%+**。

---

## A1：空稿 candidate retry

### 现状（stage-9 之后）

文件：`app/services/rebuild_candidates.py:174-212`

```python
for index, strategy in enumerate(strategies, start=1):
    savepoint = session.begin_nested()
    try:
        candidate = _generate_one_candidate(...)
        rows.append(candidate)
        session.flush()
        savepoint.commit()
    except StructuredOutputError as candidate_exc:
        savepoint.rollback()
        _skip_reasons.append({...})
```

**问题**：`_generate_one_candidate` 抛 `StructuredOutputError`（空稿/解析失败）时，只 skip 记录，**不重试**。生产上一次 rebuild 通常生成 3 个 candidate，若其中一个失败，剩余 2 个 candidate 可能因质量分布问题也一并失败或分数偏低，最终导致 revision 循环得不到有效 selected。

### 修法

在 savepoint 内加 **N 次 retry**（N=2，温度上浮）：

```python
MAX_CANDIDATE_RETRIES = 2  # 单个 candidate 最多重试 2 次（含首次共 3 次）

for index, strategy in enumerate(strategies, start=1):
    attempt = 0
    last_exc: StructuredOutputError | None = None
    while attempt <= MAX_CANDIDATE_RETRIES:
        savepoint = session.begin_nested()
        try:
            candidate = _generate_one_candidate(
                ...,
                # 每次重试温度上浮 0.05，避免陷入同一失败模式
                temperature=min(0.95, temperature + (index - 1) * 0.04 + attempt * 0.05),
                # 传 attempt 让 prompt 知道是第几次重试（可选，日志用）
                candidate_attempt=attempt,
            )
            rows.append(candidate)
            session.flush()
            savepoint.commit()
            last_exc = None
            break
        except StructuredOutputError as candidate_exc:
            savepoint.rollback()
            last_exc = candidate_exc
            attempt += 1
            _skip_reasons.append({
                "candidate_index": index,
                "attempt": attempt - 1,
                "error": str(candidate_exc),
                "retrying": attempt <= MAX_CANDIDATE_RETRIES,
            })
    if last_exc is not None:
        # 所有 retry 都失败：不再抛出，保留 skip 记录，进入下一 candidate
        pass
```

### 收益

- **单次 rebuild 成功率提升**：单 candidate 失败率若为 15%（当前实测），3 candidate 全失败率从 (0.15)³=0.34% 降到 (0.15³)³=4.2e-8
- **每次 retry 温度上浮**：避免落入同一空稿死角
- **task 日志更完整**：`_skip_reasons` 记录每次 retry 及最终结果

### 风险

- **成本**：单个 candidate 最多 3 次 LLM 调用，最坏情况成本 3×——但仅在异常路径，正常路径 1 次
- **延迟**：单章 rebuild 最坏 +2×LLM 延时，约 +2-4 min（可接受）

### 验证

回归项：`rebuild_candidates_regression.py`。新增 case：
- **case A**：首次 StructuredOutputError → 第 2 次成功 → 期望 rows 收到 candidate
- **case B**：连续 3 次失败 → 期望 skip 记录且不抛
- **case C**：所有 candidate 都空 → 期望 task 不 fail，rows=[]，selector 走 fallback

---

## A2：Ch8/Ch9 rebuild prompt 维度瓶颈

### 现状分析

Ch8/Ch9 26 个版本无一过 gate，四个维度稳定拉胯：
- **conflict_pressure**（冲突压强）
- **choice_and_cost**（选择与代价）
- **brief_coverage**（brief 覆盖度）
- **chapter_unit_flow**（章节内部节奏）

需要（等 Ch16-20 跑完后）具体分析：

1. 拿 Ch8 or Ch9 的 rebuild prompt 全文
2. 抽 3-5 个低分 candidate 的 QualityReport 找 reviewer 的批评点
3. 定位 prompt 里对应这 4 个维度的引导语是否**缺失/含糊/过弱**

### 待做步骤

```bash
# 1. 抽 Ch9 一个 rebuild candidate 的 prompt 和 QualityReport
python -c "
from app.db.session import ...
# 打印 Ch9 v638 (candidate #3 存活) 的 prompt payload 和 QR
"

# 2. 定位 rebuild_candidates.py 里的 prompt 模板
grep -n "prompt\|template" app/services/rebuild_candidates.py

# 3. 找到 4 维度的 review criteria
grep -rn "conflict_pressure\|choice_and_cost" app/services/ app/reviews/
```

### 修法（暂定，需数据支持）

1. **在 prompt 里显式列出这 4 个维度的 rubric**
2. **加入示例低分/高分对比**（few-shot）
3. **让 LLM 在生成前自检这 4 个维度**

### 验证

- 拿 Ch8 or Ch9 重跑 rebuild，比较 4 维度评分改进
- 若改进 ≥3 分，回放 Ch8/Ch9 端到端，看能否自动过

---

## 落地时机

**等 Ch16-20 (proc_a4c551f72c22) 批跑完成后**再动代码。届时：
1. Ch16-20 数据可验证 stage-9 修复的普适性
2. 空稿失败率、维度分布可从 Ch11-20 共 10 章数据统计
3. A1 patch + 回归 + Ch16-20 手工重跑对照
4. A2 需具体 prompt 数据支持，届时抽 Ch8/9/spike 数据分析

## 后续 P2/P3（不在本轮）

- **P2**：plateau_stop delta 参数化、rule-flat spike guard、accept idempotent 避免空转
- **P3**：**status 变化审计日志**——预防未来 root cause hunt 反复
- **revision_loop_guard 6 pre-existing FAIL**（未纳入本轮 Sprint）
