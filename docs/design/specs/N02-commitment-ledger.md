# N02｜承诺账本

版本：1.4  
变更依据：ADR-030、ADR-031、ADR-035

## P0 职责

### N02-001｜激活承诺

- 输入：当前 definition_version 的全部 assignment 已接受、主负责人、deliverable_key 与初始 promised_deadline_sim_time。
- 必填值完整后创建不可变 `CommitmentRevision(revision_no=1, status=ACTIVE)`。
- ActionItem.active_commitment_revision_id 与状态必须在同一事务更新为该修订和 TRACKING。
- 每个修订保存 source_message_id 与 source_span。
- 验证：AUTO-N02-001 至 AUTO-N02-003。

### N02-002｜承诺修订

- 同一 ActionItem 收到明确的新交付定义或截止时间时，将旧 ACTIVE 修订设为 SUPERSEDED，创建 revision_no+1 的 ACTIVE 修订。
- 历史修订不得覆盖；策略下一次评估立即使用新承诺。
- 个人 `promised_deadline_sim_time` 与团队 `ActionItem.team_required_by_sim_time` 分开保存：个人承诺不晚于团队日期时直接生效；晚于团队日期时同样保留真实承诺，但追加 `ScheduleConflictDetected`，由 COORDINATOR 处理冲突，不自动修改团队日期。
- 验证：AUTO-N02-004。

### N02-003｜消息幂等

- 相同 source_message_id 重复处理必须返回既有结果，不创建新 ActionItem 或 CommitmentRevision。
- identity merge 只在 N02 内完成，其他模块不得从历史消息再次推断当前承诺。
- 验证：AUTO-N02-005、AUTO-N02-006。

### N02-004｜字段缺失

- 确认/修订缺少必需字段时不得激活不完整修订。
- P0 返回结构化 unresolved_fields 并保留 ActionItem=PENDING_CONFIRMATION；派发成员未全部接受时保持 PENDING_ASSIGNMENT，任何人退回时进入 NEEDS_REVISION。
- 验证：AUTO-N02-007。

### N02-005｜唯一读取口径

- N04、N10、N12 与 UI 只从 ActionItem.active_commitment_revision_id 读取当前承诺。
- 验证：AUTO-N02-008。

### N02-006｜分层工期视图

- COORDINATOR 工期视图同时展示全部任务的 `team_required_by_sim_time` 与主负责人 ACTIVE CommitmentRevision 的 `promised_deadline_sim_time`，并明确标记 `ON_TIME | AT_RISK | CONFLICT | OVERDUE`。
- PARTICIPANT 的活跃时间线只展示本人负责或协作的任务；蓝色表示负责、紫色表示协作。会议整体只返回聚合计数；除同任务角色与派发回应状态外，不暴露他人的个人承诺、进展、阻塞或提交。
- 本人历史参与任务退出活跃时间线，在折叠历史区只读展示。
- 验证：AUTO-N02-009、MANUAL-N02-010。

## P1｜问题收集、投票与定稿

- 只保留一个白名单结构：多个上游问题收集 ActionItem + 一个单负责人定稿 ActionItem + 指定投票输入；不建立 Workflow、Stage 或多 owner 实体。
- 定稿任务只读取上游人工验收后的 `AcceptedTaskResult/accepted_version_id`；全部 `ALL_REQUIRED` 依赖满足前不可生成 ballot、不可投票、不可提交定稿。
- ballot 草稿由 Agent 从已验收输入抽取并去重，保留每项 `source_refs(action_item_id, version_id)`，由最终负责人删选/改字并显式发布。正式发布后锁定。
- 每位指定投票人必须给全部候选 1–5 整数分，首次提交后锁定；所有指定投票完成后，按总分、平均分、option_id 的确定性顺序选出前 N 项，才解锁最终负责人普通提交。
- P1 不提供缺席、弃权、提前关票、百分比门槛或改票；名单必须由 COORDINATOR 在首次派发前确认。
- 上游有效版本变化时，下游 ballot、投票和已提交定稿按 lineage 失效并回到 TRACKING；不得静默沿用旧结果。
- 提醒只针对真正未完成的上游负责人/投票人；等待中的最终负责人不因 Gate 未满足收到泛化催办。
