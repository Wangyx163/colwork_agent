# 多同事会议行动项协作 Agent｜状态机

版本：1.5  
变更依据：ADR-030、ADR-034、ADR-035

## 1. Episode

`DRAFT -> ACTIVE -> READY_FOR_FINAL_APPROVAL -> APPROVED -> ARCHIVED`

任意非终态在不变量破坏时可进入 `BLOCKED`；DRAFT/ACTIVE 可由 COORDINATOR 进入 `CANCELLED`。

- `DRAFT -> ACTIVE`：参会者、转写与 ContentPack 校验通过。
- `ACTIVE -> READY_FOR_FINAL_APPROVAL`：所有必需 ActionItem 已 ACCEPTED/AGGREGATED、存在 AcceptedTaskResult 且终稿已生成。
- `READY_FOR_FINAL_APPROVAL -> APPROVED`：FINAL_RELEASE 获 COORDINATOR 批准。
- `APPROVED -> ARCHIVED`：发布 Outbox 已 DELIVERED 且归档完成。
- 验证：AUTO-SM-EP-001 至 AUTO-SM-EP-004。

## 2. ActionItem

主路径：

`PENDING_CONFIRMATION -> PENDING_ASSIGNMENT -> TRACKING -> PENDING_ACCEPTANCE -> ACCEPTED -> AGGREGATED -> ARCHIVED`

分支：

- `PENDING_CONFIRMATION -> REJECTED`：COORDINATOR 在首次派发前忽略误提取项。
- `PENDING_ASSIGNMENT -> NEEDS_REVISION`：任一当前版本被派发人退回重改；整轮回应失效并暂停执行。
- `NEEDS_REVISION -> PENDING_ASSIGNMENT`：COORDINATOR 修改任务定义、递增 definition_version 并重新派发。
- `PENDING_ASSIGNMENT -> TRACKING`：当前版本一名主负责人和全部协作者均已接受；原子激活 owner、协作者和首个承诺。
- `TRACKING -> TRACKING`：收到无效版本，保存错误并继续等待修订版。
- `TRACKING/ACCEPTED/AGGREGATED -> 原状态`：协作者提交通过校验的贡献版本，等待任务负责人处理；AI 贡献分析不改变任务状态。
- `TRACKING/ACCEPTED/AGGREGATED -> PENDING_ACCEPTANCE`：任务负责人提交最终候选，或将一个未处理的协作贡献 `PROMOTE` 为整项任务候选。
- `PENDING_ACCEPTANCE -> TRACKING`：COORDINATOR 退回交付版本并说明原因。
- `PENDING_ACCEPTANCE -> ACCEPTED`：COORDINATOR 验收最新待验收版本，更新 current_valid_version_id 并冻结 AcceptedTaskResult。
- `ACCEPTED/AGGREGATED -> PENDING_ACCEPTANCE`：晚到新版本确定性校验通过，等待重新人工验收；原 current_valid_version_id 在新版本通过前保持。
- 任意非终态 `-> BLOCKED`：数据不变量或无法恢复的业务错误。

规则：

- 只有 COORDINATOR 可复核/发布任务、修改 team_required_by 和执行交付验收。
- 只有 COORDINATOR 可选择当前 Episode PARTICIPANT 形成版本化派发；被派发人只能回应当前版本。只有 owner_actor_id 对应负责人可修订 promised_by。任务负责人、当前版本已接受的协作者，以及 `OPEN/ACKNOWLEDGED` AssistanceRequest 的目标可在同一 ActionItem 上回应状态和提交；协作者不获得人工验收权。
- 派发必须有 team_required_by；所有 assignment 接受后才条件更新 owner、创建首个 ACTIVE CommitmentRevision 并进入 TRACKING。
- `promised_by > team_required_by` 不阻止保存真实承诺，但产生 schedule conflict；两类日期均不互相覆盖。
- 负责人最终候选格式校验通过只进入 PENDING_ACCEPTANCE；协作贡献格式校验通过保持任务原状态。模型辅助信息不能让贡献进入 PENDING_ACCEPTANCE，也不能让任务进入 ACCEPTED。
- 协作贡献处理状态由 ArtifactVersion 与审计事件派生为 `AWAITING_OWNER | INCLUDED | REVISION_REQUESTED | PROMOTED`，不是新的领域实体或 ActionItem 状态机；每个版本只允许一个负责人决定。
- AGGREGATED 要求终稿 lineage 已引用 current_valid_version_id。
- `is_overdue = clock.now > active_commitment.promised_deadline AND status IN {TRACKING, PENDING_ACCEPTANCE}`；待回应与退回修改阶段不催执行进度。
- 验证：AUTO-SM-AI-001 至 AUTO-SM-AI-011。

## 3. AssistanceRequest

`OPEN -> ACKNOWLEDGED -> RESOLVED`

- `OPEN/ACKNOWLEDGED -> CANCELLED`：请求人取消或任务已完成导致请求失效。
- requester 与 target 必须属于同一 EpisodeParticipant；target 不得为 SYSTEM 或未参会 Actor。
- target 在 `OPEN/ACKNOWLEDGED` 期间获得该 ActionItem 的贡献权限，`RESOLVED/CANCELLED` 后权限撤销；该权限从关系状态推导，不创建第二个 ActionItem。
- 未解决时抑制该任务的通用进展询问，但不抑制与求助本身有关的提醒。
- 验证：AUTO-SM-HELP-001 至 AUTO-SM-HELP-004。

## 4. Approval

状态：`PENDING | APPROVED | REJECTED | SUPERSEDED | EXECUTED | FAILED`

- P0 仅 FINAL_RELEASE；只有 COORDINATOR 可决定。
- `PENDING -> APPROVED`：在同一事务创建执行 OutboxEntry。
- `PENDING -> REJECTED`：不得产生外部副作用。
- `PENDING -> SUPERSEDED`：终稿生成新修订。
- `APPROVED -> EXECUTED/FAILED`：OutboxEntry DELIVERED/DEAD_LETTER。
- 重试复用原 EffectId；终态不可再次执行。
- 验证：AUTO-SM-APP-001 至 AUTO-SM-APP-006。

## 5. CollaborationMemory

`PRIVATE_DRAFT -> CONFIRMED | REJECTED`

- 新确认值使同 actor/topic 的旧 CONFIRMED 值进入 SUPERSEDED。
- 只有本人可确认、从同 topic 预制词表替换或拒绝；替换创建新版本并保留 evidence_refs，词表外自由文本失败。
- Memory 不驱动 ActionItem、Approval 或访问权限状态迁移。
- 验证：AUTO-SM-MEM-001 至 AUTO-SM-MEM-004。

## 6. OutboxEntry

状态：`PENDING | CLAIMED | RETRY_WAIT | DELIVERED | DEAD_LETTER`

- `PENDING -> CLAIMED`：Dispatcher 原子领取。
- `CLAIMED -> DELIVERED`：适配器返回新回执或同 EffectId 的既有回执。
- `CLAIMED -> RETRY_WAIT -> PENDING`：可重试错误到期后重新领取。
- `CLAIMED -> PENDING`：确认领取会话已失活；重启后可重领。
- `CLAIMED/RETRY_WAIT -> DEAD_LETTER`：达到重试上限或终止错误。
- DELIVERED 不可逆；所有重试复用原 EffectId。
- 验证：AUTO-SM-OUT-001 至 AUTO-SM-OUT-006。

## 7. FinalDeliverable

`PENDING_APPROVAL -> APPROVED -> RELEASED`

- 任一未发布修订在产生新修订时进入 `SUPERSEDED`。
- PENDING_APPROVAL 必须拥有完整 lineage；FINAL_RELEASE 被拒后保持该修订但不得发布。
- 验证：AUTO-SM-FINAL-001 至 AUTO-SM-FINAL-003。
