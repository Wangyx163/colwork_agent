# AI 面试版 P0 执行计划与低成本模型路由

状态：P0/P1 ENGINEERING COMPLETE（最终评测除外）；LIVE HITL SIGN-OFF PENDING  
日期：2026-08-07  
依据：ADR-033

## 一句话展示

一个单 Agent 把会议事实抽取成任务，在 PostgreSQL 外置状态上跨步骤推进，经人工 Gate 验收版本化成果，崩溃后幂等恢复，并用 Harness 解释质量、上下文和 Token 成本。

## P0 闭环

| ID | 垂直闭环 | 状态 | 默认模型 | 升级条件 |
|---|---|---|---|---|
| AI-P0-01 | 逐字稿 → grounded JSON 任务 → 原文对齐 → 人工确认 | 已完成；Harness 有抽取 F1/字段/证据指标 | Terra medium | 抽取/修复语义冲突才用 high |
| AI-P0-02 | PostgreSQL 待办 → Agent run-once/loop → 单节点事务 → 停止/恢复继续 | 已完成；Web 与 Agent Worker 独立进程 | Terra high | 仅恢复并发审计用 xhigh |
| AI-P0-03 | task contract + current version → purpose-scoped context → Token budget/裁剪 → LLM/规则结果 | 已完成；Context Manifest v1 | Terra high | 两次跨版本不变量失败才升级 |
| AI-P0-04 | 人工验收 → AcceptedTaskResult → Memory → final lineage → FINAL_RELEASE | 已完成；诊断页显示 Context/Token Trace | Terra medium | lineage 变化用 high |
| AI-P0-05 | 外部已接受 → 本地确认前崩溃 → 原 EffectId 恢复 → 零重复副作用 | 已完成；含 Worker 中断恢复与 Outbox 黄金场景 | Terra high | 最终故障审计一次 xhigh |
| AI-P0-06 | 固定场景 → 抽取/工作流/恢复/Context-Token 四组指标 → 面试报告 | 已完成；日常回归零外部模型调用 | Terra medium | 指标口径冲突才用 high |
| AI-P0-07 | Trace/Token 页面与演示脚本 → 现场解释每个 AI 概念 | 已完成；`run_ai_p0_demo.ps1` | Luna；不可用时 Terra low | 不升级，复杂逻辑回到对应核心任务 |
| AI-P0-08 | 协作者贡献 → purpose-scoped AI 分析 → 任务负责人 Gate → COORDINATOR 验收 | 已完成；不新增实体，复用 ArtifactVersion/audit；待真实工作台点选签收 | Terra high（状态/权限首次设计）；回归/UI 用 low | 仅跨 Gate/历史权限不变量失败才升级 |

## 模型消费纪律

1. 默认 `gpt-5.6-terra/medium`，不默认 high/xhigh。
2. 文档、fixture、UI、已有 oracle 的测试用 Luna；当前环境无 Luna 时使用 Terra low。
3. 只有 Agent Loop、Context Builder、lineage 跨层实现使用 Terra high。
4. xhigh 只允许用于 AI-P0-05 的最终恢复审计一次；不得用于文档、UI、普通测试或业务补全。
5. 同一任务连续两次失败且原因是跨模块不变量，才升级一级；环境/语法/固定断言失败不升级模型。
6. 每个实现闭环先跑目标测试，再跑当前 102 项基线；不靠扩大上下文让模型“再试一次”。
7. 生产运行时模型与编码模型分开：权限、状态、计分、lineage 和恢复只用规则；逐字稿抽取、单任务成果处理、终稿整理与 ballot 语义去重才允许 qwen-plus，temperature=0，契约最多定向修复一次，不使用更强模型做机械工作。

## 当前验收结果

- `python -m unittest discover -s tests -v`：102 项通过、1 项按环境跳过；
- PostgreSQL 适配测试通过；真实会议旧协作者候选已幂等修复为 CONTRIBUTION，任务恢复 TRACKING；
- PostgreSQL 集成用例：通过；
- `python -m collab_agent eval-ai-p0 --fresh`：四组 Gate 全通过；
- Harness 默认 `OFFLINE_REGRESSION`，外部模型调用数为 0；真实百炼契约烟测单独按需执行，避免每次回归消耗 Token。

## 明确停止

- 真实 SystemClock/工作日历；
- 求助全生命周期主动通知；
- 通用 Outbox 死信/运维矩阵；
- L3、飞书、组织级 RBAC；
- 更多附件格式、归档产品化；
- 三套通用协作模板。

## P1 仅保留（工程已完成，待真人 UI 验收）

- 收集问题 → qwen-plus/规则生成带来源草稿 → 人工确认开票 → 锁定投票 → 最终负责人定稿的一条复合链；
- 本人确认/同 topic 替换/拒绝预制 Memory，Agent Context 与当前协作者最小提示；
- purpose-scoped Context/Token 裁剪继续保留，未证明收益的 `agent_result_cache` DDL 已移除；
- 第二 TaskPack 与跨场景 Harness 属于最终评测，本轮不执行。

## P0 签收剩余

- 当前真实贡献只覆盖方案，未覆盖拍摄/剪辑/发布；任务负责人应执行一次 `INCLUDE`，确认任务仍为 TRACKING 且 COORDINATOR 没有待验收卡，不能为了测试把不完整成果强行 `PROMOTE`。
- `PROMOTE → COORDINATOR 验收` 已由自动化场景覆盖；真实业务待出现足以代表整项任务的贡献时再验证，或在隔离 fixture 中演示。当前人工场景通过后，P0 不再新增业务功能，只保留演示数据清理、报告截图与第二份盲测材料。
