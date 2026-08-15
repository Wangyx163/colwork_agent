# 多同事会议行动项协作 Agent｜AuditEvent 目录

版本：1.7  
变更依据：ADR-030、ADR-031、ADR-034、ADR-035  
说明：这些记录用于审计与指标，不是 Event Sourcing 事件，也不驱动业务投影。

## EVT-ENV-001｜审计信封

每条 AuditEvent 使用统一信封：

`{event_id, run_id, sequence_no, aggregate_type, aggregate_id, event_type, event_version, sim_time, payload, correlation_id, causation_id, recorded_at_real}`

- `(run_id, sequence_no)` 唯一且严格递增。
- payload 只记录解释动作所需的数据，避免复制完整消息正文或敏感载荷。
- 非兼容变更必须增加 event_version。
- 验证：AUTO-EVT-ENV-001 至 AUTO-EVT-ENV-003。

## P0 必需事件

### Episode 与身份

- `EpisodeCreated | EpisodeActivated | EpisodeReadyForFinalApproval | EpisodeApproved | EpisodeArchived | EpisodeBlocked`
- `PrincipalResolved | AuthorizationDenied | ProjectionFieldsFiltered`

### ActionItem、承诺与工期

- P0 主链：`ActionItemProposed | ActionItemProposalRevised | ActionItemDispatchCreated | ActionItemAssignmentAccepted | ActionItemAssignmentReturned | ActionItemDispatchSuperseded | ActionItemDispatchedAgain | ActionItemTrackingActivated`；TRACKING 状态与首个 `CommitmentRevisionActivated` 在同一事务成立。
- 旧认领兼容：`ActionItemPublishedForClaim | ActionItemClaimed | ActionItemConfirmationRequested | ActionItemConfirmed | ActionItemRevisedByOwner | ActionItemRejectedByOwner`；不得由新业务表面继续产生。
- `CommitmentRevisionCreated | CommitmentRevisionActivated | CommitmentRevisionSuperseded`
- `ScheduleConflictDetected | ScheduleConflictResolved`

### 信号、询问与求助

- `ProgressSignalRecorded | ProgressSignalExpired`
- `ProgressInquiryPlanned | ProgressInquirySuppressed`
- `AssistanceRequested | AssistanceAcknowledged | AssistanceResolved | AssistanceCancelled`

### 交付、处理与结果

- `ArtifactVersionReceived | ArtifactVersionDuplicateReceived | ArtifactVersionValidationPassed | ArtifactVersionValidationFailed`
- `ArtifactContributionAwaitingOwner | ArtifactContributionIncluded | ArtifactContributionRevisionRequested | ArtifactContributionPromotedToFinalCandidate | ArtifactContributionReclassified | ArtifactContributionAnalysisRequeued | ArtifactContributionContextManifestNormalized`
- `AttachmentExtractionCompleted`
- `TaskResultProcessingStarted | TaskResultProcessingSucceeded | TaskResultProcessingFailed | TaskResultProcessingRetryRequested`
- `AcceptancePacketGenerated | ArtifactReviewRejected | ActionItemCurrentValidVersionUpdated | ActionItemAccepted | ActionItemAggregated`
- `AcceptedTaskResultFrozen | TaskCollaborationReportGenerated | TaskCollaborationReportFailed`

### Memory

- `CollaborationMemoryProposed | CollaborationMemoryConfirmed | CollaborationMemoryCorrected | CollaborationMemoryRejected | CollaborationMemorySuperseded`

### 干预与审批

- `InterventionDecisionCreated | InterventionSuppressed`
- `ApprovalRequested | ApprovalApproved | ApprovalRejected | ApprovalSuperseded | ApprovalExecuted | ApprovalExecutionFailed`

### Outbox、时间与恢复

- `OutboxEntryCreated | OutboxEntryClaimed | OutboxDeliverySucceeded | OutboxDeliveryDeduplicated`
- `OutboxDeliveryRetryScheduled | OutboxEntryDeadLettered | ClockAdvanced | DailyBudgetReset | ProcessRecovered`

### 终稿与评测

- `FinalOrganizationQueued | FinalOrganizationStarted | FinalOrganizationSkipped | FinalOrganizationRetryScheduled | FinalOrganizationDeadLettered | FinalOrganizationSucceeded`
- `FinalDeliverableGenerated | FinalDeliverableSuperseded | FinalDeliverableReleased`
- `MetricCalculated | EvaluationRunCompleted`

## P1 可选事件

- `L3ApprovalRequested | InterventionsMerged | ClarificationRequested | ArchiveExported | FailureDetailRecorded`
- `CollaborationMemoryShared | CollaborationMemoryExported | CollaborationMemoryDeleted`
- `CollaborationPatternProposed | CollaborationPatternConfirmed | ActionItemDependencyCreated | ActionItemDependencySatisfied | ActionItemContributionRecorded | DownstreamResultInvalidated`

## EVT-CAT-001｜写入规则

- AuditEvent 与导致它的领域状态修改在同一事务写入。
- 重复输入若未产生新业务变化，可记录去重事件，但不得重做业务副作用。
- AuditEvent 写入失败时业务事务必须失败。
- 事件 payload 必须遵循可见性和最小化原则；完整提交正文、管理侧规则与私有 Memory 不复制到通用时间线。
- 模型/Adapter 事件只嵌入 invocation manifest、来源版本与状态；`field_allowlist` 说明允许读取的路径，不复制这些路径的实际正文或附件二进制。
- 验证：AUTO-EVT-CAT-001、AUTO-EVT-CAT-002。

## EVT-CAT-002｜读取规则

- 当前状态查询只读领域表。
- 时间线和产品指标可读 AuditEvent，但不得把它当作恢复位置。
- 业务时间线必须按 Principal 过滤事件类型与 payload 字段；诊断页使用独立工程权限。
- 验证：AUTO-EVT-CAT-003、AUTO-EVT-CAT-004。
