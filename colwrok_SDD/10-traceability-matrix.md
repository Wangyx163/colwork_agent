# 多同事会议行动项协作 Agent｜P0 追踪矩阵

| 业务目标 | 核心实体/契约 | 主要模块 | 指标 | 验收 |
|---|---|---|---|---|
| 身份与最小权限 | Principal、EpisodeParticipant、API-AUTH-001/002 | N03、N10、N13 | GATE-GUARD、SIG-AUTH | TST-INT-001 |
| 模型/工具最小上下文 | invocation manifest、purpose、字段 allowlist、实体版本 | N06、N07、N10 | SIG-AUTH、SIG-RESULT、SIG-OUTBOX | TST-INT-005/008 |
| 从会议落定任务责任 | ActionItem、ActionItemAssignment、CommitmentRevision、API-EXTRACT/DISPATCH/ASSIGNMENT-RESPONSE | N01、N02、N03 | SIG-FUNNEL、SIG-EXTRACT、SIG-LATENCY | TST-INT-001 |
| 双工期可视 | team_required_by、promised_by、ScheduleConflict | N02、N13、N14 | SIG-SCHEDULE | TST-INT-002 |
| 按信号主动追交付 | ProgressSignal、Intervention、EffectId | N04、N14 | GATE-GUARD、SIG-SIGNAL/INQUIRY/TOUCH | TST-INT-003 |
| 参会者求助 | AssistanceRequest、API-HELP-001 | N04、N13 | GATE-GUARD、SIG-HELP | TST-INT-004 |
| 外发不重复 | OutboxEntry、API-IM-002 | N03、N09 | GATE-DUP、SIG-OUTBOX | TST-REC-001 |
| 接收与验收单任务结果 | ArtifactVersion、AcceptedTaskResult、API-DELIVERY | N06 | GATE-VER、SIG-RESULT | TST-INT-005/006 |
| 协作报告与 Memory | CollaborationMemory、API-MEM-001 | N15 | SIG-MEM | TST-INT-007 |
| 汇总和发布 | FinalDeliverable、FinalFieldLineage、FINAL_RELEASE | N05、N07、N08 | GATE-E2E/VER、SIG-APPROVAL/LATENCY | TST-INT-008 |
| 多同事完整协作 | EpisodeParticipant、ContentPack 行为脚本 | N01–N15 | 5 GATE＋FLOW/EFFECT/NODE SIGNAL | TST-E2E-001 |
| 可解释与可恢复 | 领域表、AuditEvent、OutboxEntry | N09、N12、N13 | GATE-REC/DUP＋全部 NODE SIGNAL | TST-REC-001/E2E |
| 可恢复单 Agent 长程闭环 | Agent Worker、PostgreSQL 状态、VirtualClock、EffectId、Context Manifest | N06、N07、N09、N10、N12、N14 | groundedness、HITL/lineage、恢复/幂等、Context/Token | TST-AGENT-001 |

## TRACE-001｜范围治理

- P0 实现项必须能映射到上表至少一个业务目标。
- 无法映射且不属于安全、数据完整性或开发基础设施的工作，默认降至 P1/P2，而不是新增 P0 实体或框架。
