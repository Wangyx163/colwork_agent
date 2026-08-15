# N09｜PostgreSQL 持久化与 Outbox

版本：1.4  
变更依据：ADR-032、ADR-033

## P0 职责

### N09-001｜原子提交

- 领域表修改、AuditEvent 与 OutboxEntry 由一个 PostgreSQL 事务提交。
- 任一写入失败时全部回滚；P0 不维护可回放投影或第二套流程位置。
- 验证：AUTO-N09-001。

### N09-002｜EffectId 唯一

- OutboxEntry.effect_id 全局唯一。
- 新业务动作生成一次 EffectId；相同动作的所有重试和恢复复用它。
- 重复插入同 EffectId 返回既有 OutboxEntry 或稳定唯一冲突，不产生第二条外发计划。
- 验证：AUTO-N09-002、AUTO-N09-003。

### N09-003｜Dispatcher

- P0 只运行一个活动 Dispatcher；数据库会话锁和原子条件更新共同保护领取。
- 领取时写 claimed_by_session；新会话启动后把旧失活会话持有的未终态 entry 重置为 PENDING。
- 回收不依赖推进 VirtualClock 或等待墙上超时。
- 验证：AUTO-N09-004、AUTO-N09-004A。

### N09-004｜接受后响应丢失

- mock IM 已接受而本地未保存回执时，恢复后用原 EffectId 重投。
- mock IM 返回同一 external_message_id，OutboxEntry 最终进入 DELIVERED。
- 验证：AUTO-N09-005、TST-REC-001。

### N09-005｜失败与阻塞

- 面试 P0 不实现完整通用失败矩阵；只要求 FINAL_ORGANIZATION 已有受限重试，以及 N09-004 的接受后响应丢失黄金恢复场景。
- 其他失败保留稳定错误与 AuditEvent，不建设完整运维告警/死信处置产品。
- 验证：AUTO-N09-005、TST-REC-001。

### N09-006｜启动恢复

- Worker 启动后先回收旧会话 CLAIMED entry，再只从 ACTIVE Episode、领域表、当前 ClockSource、到期规则和未终态 OutboxEntry 推导下一步。
- 清空进程内存后结果不得改变。
- 验证：AUTO-N09-008。

### N09-007｜工作台单实例

- P0 同一 host/port 只允许一个工作台 HTTP Server 绑定；Windows 使用独占端口绑定，第二个实例必须立即启动失败并给出明确端口占用提示。
- 不允许两个服务进程通过地址复用轮流接收请求；重启流程必须先停止旧实例，再启动并探活新实例。
- 验证：AUTO-N09-009。

### N09-008｜Web 与 Worker 运行隔离

- Web/API 和 Worker 属于同一模块化单体、共享 PostgreSQL 事实源，但使用隔离的运行循环和数据库连接。
- 百炼调用和终稿组织在 Agent Worker 执行，不得占用 HTTP Server 的请求循环；VirtualClock 规则由 Harness/Agent CLI 显式推进。
- 验证：AUTO-N09-010、HARNESS-N09-011。
