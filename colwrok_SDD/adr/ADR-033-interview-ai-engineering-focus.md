# ADR-033｜面试项目聚焦可恢复单 Agent AI 工程主线

状态：ACCEPTED  
日期：2026-08-06  
取代范围：ADR-032 的真实 SystemClock、完整求助通知、通用生产级 Outbox 故障矩阵与 Live 运维验收

## 背景

本项目用于面试展示，不以企业级协作系统上线为目标。继续投入真实日历、完整通知生命周期、组织级权限、通用死信治理和运维页面，会提高工程量但稀释 AI Agent 的核心叙事。面试需要能够现场证明的 AI 工程能力，而不是业务功能数量。

## 项目定位

项目是一个以会议协作为业务载体、使用 PostgreSQL 外置状态、可中断恢复的单 Agent 长程工作流。它重点展示：

- grounded structured extraction；
- LLM 与确定性规则混合架构；
- Human-in-the-loop；
- durable execution / state externalization；
- ArtifactVersion 与 lineage；
- EffectId 幂等恢复；
- purpose-scoped context、Token budget 与可观测 usage；
- evaluation harness 与故障注入。

## P0 决策

1. 保留 ADR-032 的 Web/API 与 Agent Worker 运行隔离，以及从 PostgreSQL 推导下一步的恢复原则。
2. P0 只用 VirtualClock 演示跨时间/沉默规则；真实 SystemClock、工作日历和生产调度退出 P0。
3. 求助保留最小状态与页面可视，不继续实现创建/确认/解决/取消的完整主动通知链。
4. Outbox 只保留一个高含金量黄金恢复场景：外部已接受、进程在本地确认前崩溃、以原 EffectId 恢复且零重复副作用。通用接受前重试、死信矩阵、数据库会话锁和运维告警不作为 P0 Gate。
5. 保留结构化抽取、HITL、单任务成果处理、版本 lineage、终稿整理和私有 Memory 候选；冻结既有双工期、权限和求助功能，不继续生产化扩展。
6. 新增 Context Manifest/Token Budget：每次模型调用记录 purpose、允许字段、实体版本、included/omitted refs、预算、裁剪策略、input hash 和 usage。
7. Evaluation Harness 精简为抽取、工作流/HITL、恢复/幂等、上下文/Token 四组证据；不追求生产运维覆盖率。

## P1 决策

P1 只保留：

1. “多人收集问题 → 投票 → 最终负责人定稿”的一个复合工作流；
2. 本人确认 Memory 后受控进入后续任务上下文；
3. 上下文压缩、缓存与 Token budget 优化；
4. 第二个 TaskPack，证明运行时不是单一会议硬编码。

三个通用协作模板、L3/直属上级、安静时段、更多文件格式、归档产品化、真实飞书、组织级权限与跨组织 Memory 均不排期。

## 模型成本原则

- 默认使用 `gpt-5.6-terra/medium`；已有 oracle 的局部任务使用 Luna，当前环境不可用时回退 `gpt-5.6-terra/low`。
- durable Agent Loop、Context Builder、lineage 变更使用 `gpt-5.6-terra/high`。
- `gpt-5.6-terra/xhigh` 只用于黄金恢复场景的并发/崩溃最终审计，不作为日常默认。
- 连续两次因跨状态不变量失败，才允许从 medium 升 high；只有恢复/并发证据仍不确定时升 xhigh。

## 结果

P0 的业务面保持足够真实，但所有新增工作都必须直接证明一项 AI Agent 工程能力；不能映射到上述能力的工作默认停止。
