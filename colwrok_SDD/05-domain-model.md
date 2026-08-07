# 多同事会议行动项协作 Agent｜领域模型

版本：1.6  
变更依据：ADR-024、ADR-030、ADR-031、ADR-034、ADR-035

## 建模边界

- 业务实体：Organization、Actor、Episode、ActionItem、CommitmentRevision、ArtifactVersion、AssistanceRequest、Intervention、Approval、FinalDeliverable、CollaborationMemory。
- 关系/派生记录：EpisodeParticipant、ActionItemAssignment、AcceptedTaskResult、FinalFieldLineage；它们不建立第二套业务流程。
- 基础设施表：OutboxEntry、AuditEvent；它们不参与业务概念扩张。
- 只有拥有独立身份、生命周期或不变量的对象才建表；显示模型、流程节点和抽取中间结果不建实体。

## P0 表结构

### DATA-ORG-001｜Organization

- 必需字段：organization_id、name、status、created_at。
- 约束：P0 只允许一个 `status=ACTIVE` 的 Organization。
- 验证：AUTO-DATA-ORG-001。

### DATA-ACTOR-001｜Actor

- 必需字段：actor_id、organization_id、display_name、actor_type、status。
- 枚举：actor_type 为 `USER | SIMULATED_USER`。
- 约束：Actor 必须属于 Episode 所属 Organization。
- 验证：AUTO-DATA-ACTOR-001。

### DATA-EP-001｜Episode

- 必需字段：episode_id、organization_id、run_id、content_pack_id、owner_actor_id、status、created_sim_time、evaluation_cutoff_sim_time、version。
- 可空字段：current_final_deliverable_id；首个终稿生成后写入。
- 状态：`DRAFT | ACTIVE | READY_FOR_FINAL_APPROVAL | APPROVED | ARCHIVED | BLOCKED | CANCELLED`。
- 约束：episode_id 唯一；P0 同时最多一个 ACTIVE Episode。
- 验证：AUTO-DATA-EP-001、AUTO-DATA-EP-002。

### DATA-ROLE-001｜EpisodeParticipant

- 必需字段：episode_id、actor_id、role。
- P0 枚举：role 为 `COORDINATOR | PARTICIPANT`。现有数据库可临时映射 `AGGREGATOR -> COORDINATOR`、`ACTION_OWNER -> PARTICIPANT`；MANAGER 为 P1 保留值。
- 约束：`(episode_id, actor_id, role)` 唯一；每个 Episode 恰有一个 COORDINATOR，至少一个 PARTICIPANT。ActionItem.owner_actor_id 必须是本 Episode 的 PARTICIPANT。
- 验证：AUTO-DATA-ROLE-001。

### DATA-AI-001｜ActionItem

- 必需字段：action_item_id、episode_id、identity_key、title、deliverable_key、required、status、sla_id、source_message_id、source_span、proposal_metadata、created_sim_time、definition_version、version。`published_sim_time`、`owner_actor_id` 与 `team_required_by_sim_time` 在待复核阶段可空。
- 可空字段：active_commitment_revision_id、current_valid_version_id、confirmed_sim_time、last_owner_signal_at、last_owner_signal_type、last_owner_signal_valid_until；信号字段是从 allowlist AuditEvent 同事务维护的可查询摘要。
- 状态：`PENDING_CONFIRMATION | PENDING_ASSIGNMENT | NEEDS_REVISION | TRACKING | PENDING_ACCEPTANCE | ACCEPTED | AGGREGATED | ARCHIVED | REJECTED | BLOCKED`。REJECTED 仅用于 COORDINATOR 在首次派发前忽略误提取项，不再表示被派发人拒接。
- 约束：派发时必须有 `team_required_by_sim_time`、一个 OWNER assignment 与零到多个 COLLABORATOR assignment；进入 TRACKING 前必须是当前 definition_version 的全部 assignment 已接受，同时拥有 owner_actor_id 与一个 ACTIVE CommitmentRevision；identity_key 在 Episode 内唯一。
- 字段分层：`work_requirements` 对执行人可见；`management_review_policy` 仅 COORDINATOR 和验收 purpose 的 SYSTEM 可见，不得放入 PARTICIPANT projection。
- 说明：抽取结果直接成为 `PENDING_CONFIRMATION` ActionItem；`PENDING_ASSIGNMENT` 表示当前版本已经派发但仍有人未回应，`NEEDS_REVISION` 表示整轮已退回负责人修改，不建立 ActionItemCandidate 或公开 Claim 表。
- 验证：AUTO-DATA-AI-001 至 AUTO-DATA-AI-004。

### DATA-ASG-001｜ActionItemAssignment

- 必需字段：assignment_id、action_item_id、definition_version、actor_id、assignment_role、response_status、assigned_sim_time、source_message_id。
- 可空字段：response_message、responded_sim_time。
- 枚举：assignment_role 为 `OWNER | COLLABORATOR`；response_status 为 `PENDING | ACCEPTED | RETURNED | SUPERSEDED`。
- 约束：`(action_item_id, definition_version, actor_id)` 唯一；每个 definition_version 恰有一个 OWNER；actor 必须是本 Episode 的显式 PARTICIPANT；任一 RETURNED 使同版本其他非终态 assignment 进入 SUPERSEDED。
- 说明：该记录只表达版本化派发关系与逐人回应；任务结果、协作贡献、验收和推进仍归原 ActionItem。
- 验证：AUTO-DATA-ASG-001 至 AUTO-DATA-ASG-005。

### DATA-COM-001｜CommitmentRevision

- 必需字段：commitment_revision_id、action_item_id、revision_no、owner_actor_id、deliverable_key、promised_deadline_sim_time、source_message_id、source_span、status、created_sim_time、supersedes_revision_id。
- 状态：`ACTIVE | SUPERSEDED`。
- 约束：同一 ActionItem 的 revision_no 从 1 严格递增，同时最多一个 ACTIVE 修订。
- 工期规则：promised_deadline_sim_time 是执行人的个人承诺；若晚于 ActionItem.team_required_by_sim_time，修订仍可保存，但必须产生 `ScheduleConflictDetected`，不得悄悄改写团队日期。
- 验证：AUTO-DATA-COM-001 至 AUTO-DATA-COM-003。

### DATA-HELP-001｜AssistanceRequest

- 必需字段：assistance_request_id、episode_id、action_item_id、requester_actor_id、target_actor_id、category、summary、status、created_sim_time、updated_sim_time。
- 可空字段：blocking_action_item_id、resolved_sim_time、resolution_summary。
- 状态：`OPEN | ACKNOWLEDGED | RESOLVED | CANCELLED`。
- 约束：requester 与 target 均必须是同一 Episode 的 PARTICIPANT/COORDINATOR，target 不能是 SYSTEM 且不能等于 requester；同一任务存在未解决求助时，通用异常询问被抑制。
- 权限派生：target 在 `OPEN/ACKNOWLEDGED` 期间是该 ActionItem 的 `REQUESTED_COLLABORATOR`；关系终结后自动失去贡献权限，不建立新的权限实体或任务实体。
- 验证：AUTO-DATA-HELP-001 至 AUTO-DATA-HELP-004。

### DATA-VER-001｜ArtifactVersion

- 必需字段：version_id、action_item_id、submitted_by_actor_id、received_sim_time、received_sequence、payload、validation_status、review_status、source_message_id、content_hash。
- 可空字段：declared_version_label、supersedes_version_id、reviewed_by、reviewed_sim_time、review_comment。
- 状态：validation_status 为 `PENDING | PASSED | FAILED`；review_status 为 `NOT_REQUIRED | PENDING | ACCEPTED | REJECTED`。
- 不可变：payload、submitted_by_actor_id、declared_version_label、received_sim_time、source_message_id 与 content_hash 创建后不得更新；`contributor_role` 保存在 source manifest 中并与提交时授权结果一致。
- 去重：同一 ActionItem 收到相同 content_hash 时复用已有版本，只追加重复接收审计，不创建新版本。
- 指针：ActionItem.current_valid_version_id 仅指向按 `(received_sim_time, received_sequence)` 排序的最新 `validation_status=PASSED AND review_status=ACCEPTED` 版本。
- 协作贡献：真实提交人不是 owner 且尚未被提升的版本保持 `review_status=NOT_REQUIRED`；其 `AWAITING_OWNER/INCLUDED/REVISION_REQUESTED/PROMOTED` 由版本字段与 AuditEvent 派生，不建立 Contribution 主表。只有 owner 的 `PROMOTE` 会把该 ArtifactVersion 变成 PENDING 最终候选。
- 验证：AUTO-DATA-VER-001 至 AUTO-DATA-VER-004。

### DATA-INT-001｜Intervention

- 必需字段：intervention_id、episode_id、action_item_id、target_actor_id、level、action_type、reason_code、social_cost、status、effect_id、correlation_id、created_sim_time。
- 可空字段：outbox_id；P0 的 L1/L2 创建时必须非空。
- P0 等级：`L1 | L2`；L0 不建立 Intervention，L3 为 P1 保留值。
- 状态：`PENDING_APPROVAL | PLANNED | DELIVERED | FAILED | SUPERSEDED`。
- 约束：同一 effect_id 只对应一个可外发 Intervention。
- 验证：AUTO-DATA-INT-001。

### DATA-APP-001｜Approval

- 必需字段：approval_id、episode_id、approval_type、effect_id、status、requested_action、requested_by、requested_sim_time、correlation_id、version。
- 可空字段：action_item_id、decided_by、decided_sim_time、execution_outbox_id；FINAL_RELEASE 的 action_item_id 为空，决定前其余决定字段为空。
- P0 类型：`FINAL_RELEASE`；`L3_INTERVENTION` 为 P1 保留值。
- 状态：`PENDING | APPROVED | REJECTED | SUPERSEDED | EXECUTED | FAILED`。
- 约束：被拒绝、被替代或已执行的审批不得再次执行。
- 验证：AUTO-DATA-APP-001。

### DATA-OUT-001｜OutboxEntry

- 必需字段：outbox_id、run_id、episode_id、effect_type、effect_id、payload、status、attempt_count、available_at_sim_time、correlation_id、created_sim_time。
- 可空字段：action_item_id、claimed_by_session、claimed_at_real、external_receipt_id、last_error_code、causation_id、delivered_sim_time。
- 状态：`PENDING | CLAIMED | RETRY_WAIT | DELIVERED | DEAD_LETTER`。
- 约束：effect_id 全局唯一；业务状态、Intervention/Approval 与 OutboxEntry 必须在同一事务写入。
- 验证：AUTO-DATA-OUT-001、AUTO-DATA-OUT-002。

### DATA-EVT-001｜AuditEvent

- 必需字段：event_id、run_id、sequence_no、aggregate_type、aggregate_id、event_type、event_version、sim_time、payload、correlation_id、recorded_at_real。
- 可空字段：causation_id；根输入没有上游原因时为空。
- 约束：`(run_id, sequence_no)` 唯一且只追加；AuditEvent 不作为业务状态重放源。
- 验证：AUTO-DATA-EVT-001。

### DATA-FINAL-001｜FinalDeliverable

- 必需字段：final_deliverable_id、episode_id、revision_no、status、generated_sim_time、payload、schema_version。
- 可空字段：approved_sim_time、supersedes_final_deliverable_id。
- 状态：`PENDING_APPROVAL | APPROVED | SUPERSEDED | RELEASED`。
- 约束：同一 Episode 的 revision_no 从 1 严格递增；修改终稿必须新建修订，不能覆盖已审批版本。
- 验证：AUTO-DATA-FINAL-001。

### DATA-FINAL-002｜FinalFieldLineage

- 必需字段：final_deliverable_id、field_path、action_item_id、version_id、source_field_path、value_hash。
- 约束：`(final_deliverable_id, field_path)` 唯一；version_id 必须是生成时对应 ActionItem.current_valid_version_id。
- 验证：AUTO-DATA-FINAL-002。

### DATA-RESULT-001｜AcceptedTaskResult

- 必需字段：action_item_id、accepted_version_id、completed_content_refs、completion_report、normalized_result、source_manifest、processing_metadata、accepted_by、accepted_sim_time。
- 约束：与 accepted_version_id 一一绑定；内容和来源创建后不可覆盖，新的验收版本产生新记录并使旧记录仅保留历史用途。
- 说明：这是 ArtifactVersion 的验收派生记录，不拥有独立状态机，也不替代 ActionItem.current_valid_version_id。
- 验证：AUTO-DATA-RESULT-001 至 AUTO-DATA-RESULT-003。

### DATA-MEM-001｜CollaborationMemory

- 必需字段：memory_id、actor_id、topic、value、visibility、status、evidence_refs、created_sim_time、version。
- 可空字段：confirmed_by、confirmed_sim_time、supersedes_memory_id。
- 状态：`PRIVATE_DRAFT | CONFIRMED | REJECTED | SUPERSEDED`。
- 约束：本人可确认、从同 topic 预制词表替换或拒绝；当前有效协作者只读 CONFIRMED 最小提示且不含证据/历史；禁止自由文本与评价性人格/能力标签；Memory 不得用于授权、验收或升级决定。
- 验证：AUTO-DATA-MEM-001 至 AUTO-DATA-MEM-004。

## 非领域持久化

- IM Message 由 mock IM/真实 IM 适配器保存，领域层只保存 source_message_id 与 external_receipt_id。
- VirtualClock、ContentPack 和 RunContext 属于运行配置，不扩展为业务实体表。
- 快捷状态、schedule conflict、验收辅助包与任务协作报告是事件或派生视图，不建主业务实体。
- P0 不建立 Artifact、ActionItemCandidate、Progress、完整依赖图、第二套流程位置表或反事实真值表。

## P1 协作结构关系

- `ActionItemDependency` 是 ActionItem 间的同 Episode 无环关系，至少包含 upstream_action_item_id、downstream_action_item_id、gate_type 与 source_span；它没有独立业务状态机，不属于新的任务实体。
- `ActionItemContribution` 是某 ActionItem 对指定 EpisodeParticipant 的轻量输入要求，至少包含 action_item_id、actor_id、contribution_type、status、payload_ref 与 source_span；它不能代替 ArtifactVersion 承载正式工作成果。
- 协作结构模板保存在来源/展开元数据中，不建 `Workflow`、`Stage` 或 LangGraph 状态表；运行状态仍从 ActionItem、关系、版本和参与记录推导。
