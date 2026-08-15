# N12｜P0 验收 Harness

版本：1.2

## P0

### N12-001｜固定场景运行

- 读取一个冻结 ContentPack，创建单个 Episode，驱动脚本同事、VirtualClock、审批输入和一次故障注入直到归档或失败。
- 失败运行必须保留，不得用重跑成功覆盖。
- ContentPack 或 Harness 在被测系统启动前损坏可标记 INFRA_ERROR；被测系统失败必须判失败。
- 验证：AUTO-N12-001 至 AUTO-N12-003。

### N12-002｜故障注入

- P0 只强制 `TST-REC-001`：IM 接受后、本地确认前进程退出。
- 恢复后比较业务终态、终稿 payload/lineage、EffectId 与外部 message_id，忽略真实时间戳和重试次数。
- 验证：AUTO-N12-004。

### N12-003｜报告

- 一条文档化命令输出：冻结配置摘要、scenario_result、gate_summary、flow/effect/node signals、失败原因和 AuditEvent JSONL 路径。
- GATE 逐项给出 pass/fail 与证据；SIGNAL 给出实际值或 `N/A + reason`。
- 任一约定 SIGNAL 字段缺失时报告为 REPORT_INVALID；SIGNAL 数值偏低本身不改变退出码。
- 任一 P0 通过条件失败时命令返回非零退出码。
- 验证：AUTO-N12-005。

## P1

- 增加审批长挂起、重试耗尽、事务回滚等固定回归场景和失败详情页。

## P2｜多任务演进

- 引入 `TaskPack + AgentAdapter + EnvironmentAdapter + Scorer` 四个协议，使不同办公任务复用运行器。
- P0 ContentPack 可迁移为首个 TaskPack；P0 不提前实现注册中心、插件市场或多 Agent 调度器。
