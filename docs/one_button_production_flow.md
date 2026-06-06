# One-Button Production Flow

本文件定义 AI Novel System v2 的前台交互原则：前台不是后台控制台，而是作者工作台。每个页面默认只显示诊断统计和一个主动作；分项工具进入高级选项。

## Core Rule

- Default surface: diagnosis summary + one primary action.
- Advanced tools: searchable, foldable, never required for normal use.
- System internals: queue, Agent Plan, embedding, market search, model routing, diagnostics stay behind the main action.
- User decisions: approve, revise, create, repair, continue.
- Unsafe writes: preview first; save/approve only after explicit confirmation.

## End-to-End Flow

1. 新建作品
   - 用户只需要输入一句想法、书名、类型和平台。
   - 主动作：一键补齐并创建。
   - 系统自动补齐读者承诺、核心设定、世界规则、主角动力和长期冲突。

2. 作品设定
   - 显示骨架分、阻断项、提醒项和确认进度。
   - 主动作：一键诊断并修复。
   - 系统自动更新市场证据、语义记忆并生成修复草案预览。
   - 用户确认后保存，才进入后续生产依据。

3. 写作台
   - 主动作：继续写作。
   - 系统自动判断下一步是创建 brief、生成正文、质检、修订、回写连续性、审批或发布准备。
   - 失败时必须在写作台直接显示“卡在哪里”，不能只跳到后台。

4. 修改与审批
   - 显示当前章版本、质检、主编审稿和建议动作。
   - 主动作：一键处理当前章。
   - 没有修改意见时执行审批；有修改意见时提交修订并退回写作台。

5. 章节地图
   - 显示全书进度统计。
   - 主动作：回到写作台继续。
   - 章节明细默认折叠，只用于定位。

6. 作品库
   - 只负责选书或进入新建作品。
   - 主动作：新建作品 / 回到写作台。

7. 系统诊断
   - 只用于排错，不参与日常写作。
   - 队列、失败任务、模型用量、发布配置、数据库备份和知识上下文默认作为高级诊断。

## Current Page Contract

- 新建作品：诊断统计 + 一键补齐并创建。
- 作品设定：诊断统计 + 一键诊断并修复。
- 写作台：主线状态 + 继续写作。
- 修改与审批：审批统计 + 一键处理当前章。
- 章节地图：流程统计 + 回到写作台继续。
- 作品库：新建作品 / 回到写作台。
- 系统诊断：排错专用。

## Development Guardrail

以后新增能力时，先接入后台能力层，再接入主动作编排；不要直接在前台新增并列按钮。只有当一个动作需要作者明确选择不同意图时，才允许出现在默认前台。
