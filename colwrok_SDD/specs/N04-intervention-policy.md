# N04｜追交付与干预策略

版本：1.6  
变更依据：ADR-030、ADR-032、ADR-033、ADR-034

## P0 信号定义

`last_owner_signal_at` 是兼容字段名，只能由任务负责人或当前协作者的以下业务事件更新；事件本身必须保留真实 actor 与 contributor_role：

- 派发接受或 CommitmentRevision 修订；
- 快捷状态 `ON_TRACK | AT_RISK | BLOCKED | WAITING_INPUT | READY_TO_SUBMIT`；
- AssistanceRequest 创建、补充或解决；
- ArtifactVersion 提交/重新提交；
- 交付退回后执行人的明确回应。

快捷状态必须保存 `valid_until`，到期后不再证明当前状态。页面在线、轮询刷新、停留、已读、未提交草稿和系统消息均不更新该时间。ArtifactVersion、求助解决和验收通过属于“进展证据”；普通快捷回应只属于“活动信号”，二者在 UI 和指标中分开。

- 验证：AUTO-N04-001 至 AUTO-N04-003。

## P0 异常询问决策表

通用异常询问必须同时满足：

1. ActionItem=`TRACKING`；
2. 不存在待验收/已有效的版本；
3. 当前时间已进入个人承诺前 `check_in_lead`，或已经逾期；
4. `now - last_owner_signal_at >= silence_window`；
5. 不存在 OPEN/ACKNOWLEDGED AssistanceRequest；
6. 不存在仍在冷却期内的询问，且当日触达预算未耗尽。

按优先级只选择第一条满足的规则：

| 条件 | 决策 |
|---|---|
| ActionItem=PENDING_CONFIRMATION 且超过 confirmation_timeout | L1 确认提醒 |
| 存在待验收版本或 current_valid_version_id | L0，不追进展 |
| 存在未解决求助 | L0，转为跟踪求助状态 |
| 最新版本校验/验收失败且未回应 | L1 返工询问 |
| 尚未进入 check_in_lead | L0，沉默不构成异常 |
| 进入检查窗口且持续无信号 | L1 私聊结构化询问 |
| 已逾期，L1 后超过 l2_wait 且仍无信号 | L2 在本次会议协作面提醒 |
| 其他 | L0 |

结构化询问优先提供快捷回应，避免要求执行人反复撰写“进展、下一步、阻塞”。策略输入只能来自领域表、VirtualClock 和 ContentPack；LLM 不参与是否询问或询问等级决定。

- 验证：AUTO-N04-004 至 AUTO-N04-008。

## P0 触达预算

- 每名 PARTICIPANT 每模拟日默认最多 2 次 L1–L2 可见触达。
- 预算按当天已创建且未被抑制的唯一 EffectId 计数，包含待投递与已投递动作；重试不重复计数。
- 达到预算后不创建 Intervention/OutboxEntry，只追加 `InterventionSuppressed` 审计；P0 不自动合并消息。
- P0 没有 L3 或预算越权路径。
- 验证：AUTO-N04-009、AUTO-N04-010。

## P0 EffectId

- 外部动作的 EffectId 由规范业务键哈希得到：`episode_id + action_item_id/final_deliverable_id + effect_type + trigger_key`。
- trigger_key 对询问包含 rule_id、触发时间边界、active_commitment_revision_id、last_owner_signal_at 和 current_valid_version_id；对返工使用 version_id；对终稿发布使用 final_deliverable_id。
- 重复评估同一触发必须得到相同 EffectId 并复用既有 Intervention/Approval；新的承诺修订、版本、有效信号或时间边界才可形成新动作。
- 同一动作的发送重试、进程恢复与审批重试必须复用原 EffectId；不使用额外递增决策实体。
- 验证：AUTO-N04-011、AUTO-N04-012。

## P0 求助路由

会议明确协作者与运行期求助是两条不同信息：前者由 N01 从原文写入任务元数据并持续拥有贡献权限；后者是执行中主动发起、具有生命周期的 `AssistanceRequest`，其目标仅在 `OPEN/ACKNOWLEDGED` 期间拥有贡献权限。P0 不把默认协作者自动转成求助，两类协作者均复用原任务而不获得共同 owner 或验收权。

- 求助目标只能从当前 EpisodeParticipant 中选择 COORDINATOR 或 PARTICIPANT；服务端拒绝未参会 actor_id。
- 系统可基于参会名单、当前任务负载和已确认协作偏好给出候选，但发起人必须确认目标。
- AssistanceRequest 独立跟踪 `OPEN -> ACKNOWLEDGED -> RESOLVED/CANCELLED`；求助未解决时不再发送泛化进展询问。
- 邀请、确认接手、解决、取消、协作者快捷状态与协作者交付必须进入原 ActionItem 的统一协作记录；不得派生第二套协作时间线。
- 可选 `blocking_action_item_id` 只表示当前求助受到哪个本会议任务阻塞，不扩展完整依赖图。
- 面试 P0 只要求请求与解决状态可在工作台和 AuditEvent 中观察；不继续建设每次状态变化的主动通知链。
- 验证：AUTO-N04-013 至 AUTO-N04-016。

## P1

- 同一负责人同日多项提醒合并成一条消息。
- 更细的 reason_code 与安静时段规则。
- L3 直属上级升级、经理角色与人工审批；只有显式启用组织权限适配后才可使用。
