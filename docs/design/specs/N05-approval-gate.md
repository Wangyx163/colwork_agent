# N05｜审批门

版本：1.2  
变更依据：ADR-030

## P0 职责

### N05-001｜类型与权限

- P0 的 Approval.approval_type 仅为 `FINAL_RELEASE`；`L3_INTERVENTION` 保留到 P1。
- 只有 Episode 的 COORDINATOR 可批准或拒绝；身份从可信 Principal 取得，不接受正文 actor_id。
- 验证：AUTO-N05-001。

### N05-002｜创建请求

- 所有必需任务已 ACCEPTED/AGGREGATED、存在 AcceptedTaskResult 且终稿 lineage 完整时，创建 PENDING FINAL_RELEASE Approval，Episode 进入 READY_FOR_FINAL_APPROVAL。
- 同一业务前提下重复评估不得创建第二个 PENDING Approval。
- 验证：AUTO-N05-002、AUTO-N05-003。

### N05-003｜批准与拒绝

- 批准时在同一事务将 Approval 设为 APPROVED，并创建唯一 EffectId 的 OutboxEntry。
- FINAL_RELEASE 批准时对应 FinalDeliverable 同步设为 APPROVED；其 Outbox DELIVERED 后设为 RELEASED。
- Outbox DELIVERED 后 Approval=EXECUTED；进入 DEAD_LETTER 后 Approval=FAILED。
- REJECTED 不得产生对应外部副作用。
- FINAL_RELEASE 拒绝必须填写可执行反馈，并明确指出需要修改的任务成果；拒绝反馈保留在审计中，未发布终稿对普通参会者不可见。执行人提交并通过验收的新版本到达后，系统自动废止旧终稿与旧审批并生成下一修订。
- FINAL_RELEASE 批准前必须在同一事务内复核：它仍是 Episode 当前终稿、状态仍为 PENDING_APPROVAL、每个章节的 `version_id + accepted_task_result_id` 仍与当前任务一致，且字段 lineage 完整；任一不一致均拒绝发布。
- 重复相同决定幂等，相反决定返回 CONFLICT。
- 验证：AUTO-N05-004 至 AUTO-N05-007。

### N05-004｜请求失效

- 待批 FINAL_RELEASE 对应终稿被新修订替代时，Approval=SUPERSEDED。
- SUPERSEDED 不得执行；按最新领域状态重新评估。
- 验证：AUTO-N05-008。

### N05-005｜持久恢复

- PENDING/APPROVED 状态均由数据库恢复；重启不得复制 Approval 或改变 EffectId。
- P1 增加专门的长挂起恢复场景。
- P1 启用 L3 时复用同一审批不变量，不改变 P0 FINAL_RELEASE 语义。
