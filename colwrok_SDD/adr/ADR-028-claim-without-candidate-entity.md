# ADR-028｜复用 ActionItem 实现任务认领

状态：SUPERSEDED BY ADR-035；仅保留为历史兼容决策  
日期：2026-08-05

## 决策

> 以下公开认领规则不再用于新业务表面。ADR-035 保留“抽取结果直接复用同一 ActionItem、不建立 Candidate/Claim 实体”，但以版本化逐人派发和响应替代公开待认领池。

百炼抽取出的行动项直接写入 `action_items`，状态为 `PENDING_CONFIRMATION`，
在负责人复核、发布和同事认领前允许 `published_sim_time`、`owner_actor_id`
与 `team_required_by_sim_time` 为空。负责人发布后必须已有 team_required_by，
`owner_actor_id IS NULL` 才作为
“待认领”派生视图，不建立
`ActionItemCandidate`、`Claim` 或第二套任务表。

认领人的身份来自可信 Principal，认领请求只提供个人 promised_by。系统通过带
`owner_actor_id IS NULL` 条件的原子更新决定胜者，并在同一事务创建首个
ACTIVE `CommitmentRevision`，随后将原 ActionItem 置为 `TRACKING`。

## 理由

- 抽取、认领、追踪和交付始终引用同一个 action_item_id，避免对象复制与状态同步。
- 允许会议没有明确负责人或截止时间，同时不让模型编造关键信息。
- 条件更新、message_id 幂等回执和承诺版本共同覆盖并发认领、网络重试和后续改期。

## 结果

- ActionItem 的 published/owner/team_required_by 在待复核阶段可空，进入待认领前必须发布并补齐团队时间，进入 TRACKING 前必须补齐 owner 和 ACTIVE CommitmentRevision。
- 工作台可展示会议原文、置信度与不确定项，并直接完成认领。
- 后续接入企业微信、飞书或 Teams 时只需增加 IM 交互适配器，不改变领域实体。
