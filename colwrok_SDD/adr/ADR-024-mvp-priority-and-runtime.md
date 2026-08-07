# ADR-024｜MVP 业务优先级与单运行时

状态：ACCEPTED  
日期：2026-08-05

## 背景

现有 SDD 已保留单 Episode、mock IM、脚本模拟同事和单事件循环等范围约束，但又将学术假设、消融、敏感性、数据集召回和大规模 seed 评测设为 v1 MUST，导致 10–15 日排期同时承担产品、可靠性内核和研究验证三套目标。LangGraph checkpoint、EventLog 投影重建与领域表也形成重复恢复语义。

## 决策

1. 项目主旨冻结为：一个协调 Agent 同时推动 3–5 名同事的多个行动项，从责任确认一直推进到终稿审批与归档。
2. `12-mvp-priority-profile.md` 定义 P0–P3；P0 是当前唯一交付承诺。
3. v1 运行时采用 PostgreSQL 领域表、单事件循环、VirtualClock 与 Transactional Outbox。
4. 删除 LangGraph 及其 checkpoint；进程恢复从领域表与 Outbox 推导下一步。
5. 领域表是业务事实唯一权威；AuditEvent 仅作 append-only 审计与产品评测轨迹，不承担 Event Sourcing、投影重建或分歧裁决。
6. 保留多同事脚本模拟和最小 Harness；P0 评测仅包含闭环完成、重复外发、旧版本混入和人均打扰四项。
7. 学术假设、消融、AMC-A、敏感性、帕累托、pass^k 与大规模 seed 评测从当前项目删除，不作为 v1 或后续产品验收前置。
8. 多任务评测能力列为 P2；届时通过 TaskPack、AgentAdapter、EnvironmentAdapter 与 Scorer 契约扩展，不提前泛化办公领域实体。

## 影响

- 保留多同事协作、分级催办、版本校验和聚合审批的完整业务主线。
- P0 仍能验证最关键的外发幂等与旧版防护，但不再声称统计稳定性或学术结论。
- 删除第二 checkpoint 与完整 Event Sourcing 后，恢复与一致性实现显著缩小。
- 当前实现保持模块化单体；N01–N14 是逻辑职责，不要求独立部署。

## 被替代的决策

- ADR-003 的 P0 人数与行为数改为 3 名负责人、4 个任务、4 个核心行为；更多行为进入 P1。
- ADR-004 的三类崩溃验证缩为 P0 一个外发幂等恢复场景；其余进入 P1。
- ADR-008 中“朴素全上下文消融基线”为当前项目删除项；关键状态外置仍保留。
- ADR-011 的四元幂等键由持久化 EffectId 取代。
- ADR-014 中模拟器使用小模型与缓存降为 P2；P0 使用冻结模板话术。
- ADR-016 的 ActionItem 状态机由 `07-state-machines.md` 1.1 简化版本取代。
- ADR-017 的 DecisionEpoch 删除；新的业务动作生成 EffectId，重试复用 EffectId。
- ADR-018 的四版本指针收敛为 ActionItem.current_valid_version_id；声明版本标签只展示。
- ADR-019 的五项 checkpoint 权重不再是 P0 验收口径。
- ADR-020 的默认策略参数可作为 fixture，但不做参数扫描结论。
- ADR-021 被本 ADR 修订：保留领域表与不可变 AuditEvent，但 AuditEvent 不再作为投影权威或重放源。

## 推翻条件

若项目目标重新变为论文或基准研究，并明确需要统计假设、数据集泛化与 pass^k 结论，应新建 ADR 恢复相应评测工作，不得直接把这些条款重新写入 P0。
