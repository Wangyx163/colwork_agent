# P0 分层验收指标契约

版本：1.4  
变更依据：ADR-026、ADR-030

## 1. 使用原则

- GATE 决定 P0 是否通过；FLOW/EFFECT/NODE SIGNAL 用于解释流程、效果和单点健康度。
- 只给安全、结果正确性和恢复能力设置硬门槛；SIGNAL 不擅自设置优秀线。
- 分母为 0 时返回 `N/A + denominator + reason + evidence`；报告缺字段属于 `REPORT_INVALID`。
- 指标只从领域表、AuditEvent、mock IM、OutboxEntry 与冻结配置计算，不从模型自由文本或页面行为猜状态。

## 2. GATE｜交付门槛

### GATE-E2E-001｜完整闭环

- 输出：`episode_archived`、`required_action_items_completed/total`、`accepted_task_results/total`、`final_released`。
- 通过：Episode=ARCHIVED，全部 required ActionItem=ARCHIVED，每项存在当前 accepted result，FinalDeliverable=RELEASED。

### GATE-DUP-001｜零重复外发

- 输出：`duplicate_external_send_count`。
- 公式：每个 effect_id 的 `max(0, distinct_external_message_id_count - 1)` 后求和。
- 通过：0；适配器对重复 effect_id 返回原 external_message_id 不算重复。

### GATE-VER-001｜版本与结果正确

- 输出：`stale_field_count`、`missing_lineage_count`、`accepted_result_version_mismatch_count`。
- 通过：三项均为 0；终稿字段和 AcceptedTaskResult 均引用对应 current_valid_version_id。

### GATE-GUARD-001｜权限与策略护栏无绕过

- 输出：`authorization_bypass_count`、`restricted_field_exposure_count`、`final_release_bypass_count`、`touch_budget_violation_count`、`invalid_help_target_count`，以及 invocation manifest 缺失/不完整、principal/字段违规、附件二进制泄漏、input_hash 错配数。
- 通过：全部为 0。
- 断言：身份不取自正文；PARTICIPANT 不可读管理规则/他人正文/私有 Memory；未参会者不能成为求助目标；FINAL_RELEASE 批准前不外发；L1/L2 不超预算。

### GATE-REC-001｜关键恢复成功

- 输出：`recovery_scenario_passed` 与恢复后的 Outbox/业务终态摘要。
- 通过：IM 已接受但本地未确认时崩溃，重启后 Outbox=DELIVERED、业务链继续且无重复外发。

## 3. SIGNAL-FLOW｜流程指标

### SIG-FUNNEL-001｜行动项漏斗

- 输出：`proposed -> dispatched -> all_assignments_accepted -> submitted -> pending_acceptance -> accepted -> aggregated -> archived` 各阶段数量与转化率；另计 returned_for_revision、superseded assignment 和 redispatch。

### SIG-SCHEDULE-001｜双工期结果

- 输出：按个人承诺交付率、按团队需要时间交付率、schedule conflict 数及解决时长、迟交项与迟交时长。
- 个人口径使用交付时刻生效的 CommitmentRevision.promised_deadline；团队口径使用 ActionItem.team_required_by。

### SIG-LATENCY-001｜关键阶段耗时

- 输出：派发至全部成员接受、全部接受至首个提交、提交至处理完成、待验收、退回至重派/重交、验收至报告/Memory 候选、Episode 至终稿/归档耗时。
- P0 报逐项值、最大值和中位数，不做统计显著性。

## 4. SIGNAL-EFFECT｜协调效果

### SIG-SIGNAL-001｜业务信号质量

- 输出：有效信号、过期信号、按类型分布、每任务 last_owner_signal_at、被排除的刷新/已读/系统消息数。
- 活动信号与进展证据分开计数。

### SIG-INQUIRY-001｜异常询问

- 输出：L1/L2 创建数、预算/冷却/求助抑制数、询问后 progress_window 内产生有效信号或进展证据的比例。
- L3 在 P0 返回 `N/A(reason=OUT_OF_SCOPE_P0)`。

### SIG-HELP-001｜求助协作

- 输出：求助创建、确认、解决、取消数，首次响应和解决时长，目标在参会名单内的比例。

### SIG-TOUCH-001｜每位同事触达负担

- 输出：`touches_by_actor`、均值、最大值，以及 L1/L2 分布；重试不重复计数。

## 5. SIGNAL-NODE｜单点诊断

### SIG-AUTH-001｜身份与授权

- 输出：Principal 解析失败、membership/owner/role 拒绝、字段裁剪和越权尝试数。

### SIG-CONTEXT-001｜模型与 Adapter 上下文

- 输出：调用生命周期事件数、实际开始次数、按 purpose/输出状态分布、manifest 完整率，以及 principal、字段 allowlist、二进制泄漏和 input_hash 错配数。
- 空运行返回合法 `N/A`；一旦存在调用事件，manifest 缺失或护栏违规进入 GATE-GUARD，而不是只做观察指标。

### SIG-EXTRACT-001｜提取与确认

- 输出：抽取任务数、unresolved_fields、identity_key 去重、负责人修订与忽略数。

### SIG-RESULT-001｜版本、处理与验收

- 输出：校验失败版本数、附件抽取成功/失败、模型技术/语义错误分布、验收辅助包生成、退回、重交关闭率、AcceptedTaskResult 数。

### SIG-MEM-001｜报告与 Memory

- 输出：协作报告生成/失败、Memory 候选、本人确认/同 topic 替换/拒绝、词表外值拦截数。

### SIG-APPROVAL-001｜终稿审批

- 输出：FINAL_RELEASE 的 PENDING/APPROVED/REJECTED/SUPERSEDED/FAILED/EXECUTED 数及待批时长。

### SIG-OUTBOX-001｜外发节点

- 输出：创建、领取、重试、适配器去重、DELIVERED、DEAD_LETTER 数。

## 6. 报告最小结构

`report.json` 至少包含：

- `gate_summary`：5 个 GATE 的 pass/fail、实际值和失败原因；
- `flow_signals`：漏斗、双工期和关键耗时；
- `effect_signals`：业务信号、异常询问、求助和触达；
- `node_signals`：身份/上下文、抽取、成果处理、Memory、审批和 Outbox；
- `evidence_refs`：run_id、episode_id、AuditEvent 序号与失败对象 ID。

GATE 失败或报告字段缺失时 eval 非零；SIGNAL 数值高低不单独判失败。
