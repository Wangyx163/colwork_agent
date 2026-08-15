# 多同事会议行动项协作 Agent｜逻辑接口契约

版本：1.9  
变更依据：ADR-030、ADR-031、ADR-032、ADR-033、ADR-035、ADR-036  
说明：冻结语义，不冻结 HTTP 路径、类名或 ORM。

## API-AUTH-001｜解析可信 Principal

- 输入：受信虚拟会话或平台 Adapter 提供的认证凭据，不读取业务请求正文 actor_id。
- 输出：`Principal{actor_id, episode_id, roles, auth_source, session_id}`。
- P0 使用签名虚拟会话；未来飞书 Adapter 把 external_user_id 映射为 actor_id。
- 失败：`UNAUTHENTICATED | ACTOR_INACTIVE | EPISODE_MISMATCH`。
- 验证：AUTO-API-AUTH-001 至 AUTO-API-AUTH-003。

## API-AUTH-002｜授权与字段投影

- 输入：`{principal, action, resource_type, resource_id, purpose}`。
- 服务端校验 Episode membership、角色、ActionItem owner 与 purpose；返回允许动作及字段 allowlist。
- PARTICIPANT projection 不含 management_review_policy、他人交付正文、他人私聊与私有 Memory。
- 验证：AUTO-API-AUTH-004 至 AUTO-API-AUTH-007。

## API-EPI-001｜创建 Episode

- 输入：`{client_request_id, organization_id, content_pack_id, transcript, participant_refs, run_context}`；COORDINATOR 来自 Principal。
- 前置：不存在 ACTIVE Episode；所有参与者可映射为有效 Actor。
- participant_refs 是权限输入而非抽取结果；真实会议导入不得省略，也不得从 transcript 自动补入未确认说话人。同一会议幂等重载时名单不一致返回冲突。
- 幂等：相同 client_request_id 不创建第二 Episode。
- 验证：AUTO-API-EPI-001。

## API-IM-001｜接收标准消息

- 输入：`InboundMessage{message_id, conversation_id, sender_actor_id, recipient_actor_ids, sim_time, content, attachments}`。
- sender_actor_id 必须与适配器凭证一致；相同 message_id 幂等。
- 输出：`AcceptedReceipt{message_id, accepted_sequence}`。
- 验证：AUTO-API-IM-001。

## API-IM-002｜发送标准消息

- 输入：`OutboundCommand{outbox_id, effect_id, conversation_id, sender_actor_id, recipient_actor_ids, effect_type, content, attachments}`。
- 仅 Dispatcher 可调用；相同 effect_id 始终返回同一 external_message_id。
- 输出：`DeliveryReceipt{external_message_id, deduplicated, accepted_sim_time}`。
- 验证：AUTO-API-IM-002。

## API-CLK-001｜推进虚拟时间

- 输入：`{run_id, target_sim_time?}`；不指定目标时推进到下一事件。
- 输出：`ClockAdvanceResult{from_time, to_time, derived_trigger_count}`。
- 同一目标重复调用不得重复生成询问或 EffectId。
- 验证：AUTO-API-CLK-001。

## 不排期｜同步真实业务时间

- SystemClock、真实业务时间同步与日历调度不属于当前面试项目 P0/P1。

## API-EXTRACT-001｜结构化抽取

- 输入：`{text, adapter_type: TRANSCRIPT|IM_REPLY, schema, run_context}`。
- 输出 `{raw_candidates[], draft_items[], review_hints[], coverage, failures[]}`。候选发现只引用稳定 unit id；系统据此生成 source_span。`draft_items` 包含可形成任务的最小语义与 unresolved_fields；每条 `review_hint` 具有稳定 hint_ref，并保留上下文扩展后仍不充分的语料及原因。
- 候选发现使用一套统一宽召回 Prompt 与确定性规则补网取并集；模型读取 `left_context + emit_zone + right_context` 重叠窗口并只为 emit zone 输出候选。证据整理可从全局逐字稿按充分性自适应扩展，不使用固定 unit 数。单候选失败必须隔离或降级为 hint，不得使同批候选或整场会议清空。
- coverage 必须区分 `SUCCEEDED | DEGRADED | FAILED`，并记录每个 unit 的唯一 emit-zone 责任窗口、context 出现窗口和未覆盖 unit；模型责任窗口失败但规则补网完成时返回 DEGRADED。
- COORDINATOR 复核并选择主负责人/协作者后才形成版本化派发。
- 验证：AUTO-API-EXTRACT-001。

## API-REVIEW-001｜复核并发布行动项

- 逻辑动作：`revise | publish | ignore | merge | create_from_hint | create_manual`；仅 COORDINATOR。`create_from_hint` 输入必须携带 hint_ref 与人工补充的任务字段。
- 发布至少需要 title、work_requirements、deliverable_key 和 team_required_by；management_review_policy 仅管理侧可见。
- source_span 不可修改；从 hint 添加任务时保留 hint/evidence unit lineage，手工添加时显式标记人工来源。合并只追加来源，不建立候选实体。
- 验证：AUTO-API-REVIEW-001。

## API-ASSIGN-001｜派发行动项

- 输入：`{owner_actor_id, collaborator_actor_ids[], assignment_message?, message_id}`；仅 COORDINATOR。
- 前置：ActionItem 为 PENDING_CONFIRMATION 或 NEEDS_REVISION，任务定义完整；所有成员属于本 Episode，且 owner 不出现在 collaborator 列表。
- 首次派发或重派为当前 definition_version 创建逐人 assignment 并进入 PENDING_ASSIGNMENT；从 NEEDS_REVISION 重派前必须先递增 definition_version。
- 幂等：相同 message_id 返回既有派发；同版本同 actor 只能存在一条 assignment。
- 验证：AUTO-API-ASSIGN-001 至 AUTO-API-ASSIGN-004。

## API-ASSIGN-002｜回应派发

- 输入：`{decision: ACCEPT|RETURN_FOR_REVISION, response_message?, message_id}`；actor 取自 Principal。
- 前置：当前 actor 存在当前 definition_version 的 PENDING assignment。
- ACCEPT 更新本人回应；全部接受时原子激活 owner、协作者和首个 CommitmentRevision，并进入 TRACKING。
- RETURN_FOR_REVISION 必须有留言；ActionItem 进入 NEEDS_REVISION，同版本其他 PENDING/ACCEPTED assignment 进入 SUPERSEDED，停止执行类写入。
- 验证：AUTO-API-ASSIGN-005 至 AUTO-API-ASSIGN-009。

## API-SIGNAL-001｜记录快捷业务信号

- 路径示意：`POST /api/action-items/{id}/signals`。
- 输入：`{signal_type, valid_until?, note?, message_id}`；actor 取自 Principal。
- signal_type 为 `ON_TRACK | AT_RISK | BLOCKED | WAITING_INPUT | READY_TO_SUBMIT`；只有任务执行人可写。
- 普通长文本进展为可选 note，不要求填写；刷新/已读不得调用此接口。
- 验证：AUTO-API-SIGNAL-001。

## API-DEADLINE-001｜修订个人承诺

- 输入：`{promised_by, reason?, message_id}`；仅任务执行人。
- 创建新的 CommitmentRevision 并替代旧修订，不创建 DEADLINE_CHANGE Approval。
- promised_by 晚于 team_required_by 时创建 schedule conflict；只有 COORDINATOR 可另行修改 team_required_by。
- 验证：AUTO-API-DEADLINE-001、AUTO-API-DEADLINE-002。

## API-HELP-001｜任务求助

- 创建输入：`{target_actor_id, category, summary, blocking_action_item_id?}`；requester 取自 Principal。
- target_actor_id 必须属于同一 EpisodeParticipant 且不能是 requester 本人；否则 AUTHORIZATION/VALIDATION。
- 响应动作：`ACKNOWLEDGE | RESOLVE | CANCEL`，并校验 requester/target 权限。
- 系统推荐候选不构成发送，必须由 requester 确认。
- 面试 P0 只要求请求/解决状态进入工作台与 AuditEvent，不要求每个状态转换产生外部通知。
- 验证：AUTO-API-HELP-001 至 AUTO-API-HELP-004。

## API-DELIVERY-001｜提交单任务交付

- 输入：`{summary?, body?, links?, attachment_refs?, client_request_id}`；actor 取自 Principal，且必须是任务负责人或当前协作者。
- 每次成功提交在原 ActionItem 下创建不可覆盖 ArtifactVersion；标题/任务定义与各类提交来源分层保存，并冻结 `submitted_by_actor_id + contributor_role`。不得为协作者创建平行交付对象。
- 负责人提交在确定性校验通过后进入 PENDING_ACCEPTANCE，并异步生成验收辅助包；协作者提交返回 `submission_kind=CONTRIBUTION`，保持任务原状态并异步生成贡献分析；两者都不自动 ACCEPTED。
- 验证：AUTO-API-DELIVERY-001 至 AUTO-API-DELIVERY-003。

## API-DELIVERY-004｜任务负责人处理协作贡献

- 路径绑定 `version_id`；输入：`{action: INCLUDE|REQUEST_REVISION|PROMOTE, comment, message_id}`；仅该 ActionItem 的 owner。
- 只允许处理确定性校验已通过、`review_status=NOT_REQUIRED` 且真实提交人不是 owner 的未处理版本；每个版本只接受一个业务决定，重试返回既有结果。
- `INCLUDE/REQUEST_REVISION` 不改变 ActionItem；`REQUEST_REVISION` 必须有反馈且提交人仍是当前协作者；`PROMOTE` 令该版本进入 PENDING review、ActionItem 进入 PENDING_ACCEPTANCE，随后仍由 COORDINATOR 验收。
- 模型的贡献分析不能调用本接口，也不能作为 owner 决定来源。
- 验证：AUTO-API-DELIVERY-008 至 AUTO-API-DELIVERY-011。

## API-DELIVERY-003｜重试验收辅助处理

- 输入：`{message_id}`；路径绑定 `version_id`，仅 COORDINATOR。
- 只允许对确定性校验已通过、人工验收仍待处理的最终候选或等待负责人处理的协作贡献，在模型处理因 `NETWORK_TIMEOUT | RATE_LIMIT | PROVIDER_5XX | INVALID_JSON` 耗尽后重新排队；不要求协作者重新上传，不改变 ArtifactVersion、提交人或人工 Gate 状态。
- 百炼 Adapter 对可恢复网络错误先执行最多 3 次带退避的请求内重试；仍失败才交由持久化 Worker 记录版本级重试/失败，避免短时断连被直接暴露成终态。
- `AUTH_CONFIG | PROVIDER_4XX | INVALID_SCHEMA | INVALID_CITATION` 不自动循环重试；错误阶段和 error_code 由系统确定，Prompt 不诊断基础设施。若 `INVALID_SCHEMA` 的 Prompt/校验契约已部署修复，COORDINATOR 可显式复用原 ArtifactVersion 重新排队。

## API-DELIVERY-002｜人工验收

- 输入：`{version_id, decision: ACCEPT|REJECT, comment, completion_report_edits?}`；仅 COORDINATOR。
- ACCEPT 冻结 AcceptedTaskResult、更新 current_valid_version_id 并进入 ACCEPTED；REJECT 必须提供可执行反馈并回到 TRACKING。
- 重复相同决定幂等，相反决定 CONFLICT。
- 验证：AUTO-API-DELIVERY-004 至 AUTO-API-DELIVERY-007。

## API-TASK-001｜查询团队工期表

- 输入：当前 Episode、cursor/filters；输出按 Principal 裁剪。
- PARTICIPANT 只读取本人负责/协作任务的执行明细、同任务 assignment 角色/回应状态和会议级聚合计数；不读取其他人的 promised_by、进展、阻塞、提交或私有 Memory。
- COORDINATOR 可读取完整团队工期、个体风险与提交/验收摘要；诊断字段不在业务 projection。
- 验证：AUTO-API-TASK-001。

## API-APP-001｜FINAL_RELEASE

- 输入：`{approval_id, decision: APPROVE|REJECT, comment}`；actor 取自 Principal，且必须是 COORDINATOR。
- 重复相同决定幂等；批准前不得创建发布 OutboxEntry。
- 验证：AUTO-API-APP-001。

终稿整理没有人工 `aggregate` HTTP 动作：全部 required 任务验收后由持久化 Loop 自动排队；业务用户只对生成后的 FINAL_RELEASE 做批准或驳回。旧的手动聚合入口不得出现在 P0 Web surface。

## API-MEM-001｜Memory 查看与预制词条决定

- 查询：本人可见 PRIVATE_DRAFT/CONFIRMED；COORDINATOR 只查询本次任务的事实型协作报告。
- 决定：`CONFIRM | REPLACE | REJECT`；只能由 memory.actor_id 对应 Principal 执行。REPLACE 必须选择同 topic 的预制 value，拒绝自由文本。
- 当前有效协作者只能读取 CONFIRMED Memory 的最小 `collaboration_hint`，不得读取 evidence_refs 与历史版本。
- Memory 不得被接口用于权限、验收或升级判定。
- 验证：AUTO-API-MEM-001 至 AUTO-API-MEM-003。

## API-EVAL-001｜运行 P0 验收

- 输入：冻结的 `evaluation_manifest`。
- 输出：`{report_id, scenario_result, gate_summary, flow_signals, effect_signals, node_signals, evidence_refs}`。
- GATE 失败或报告字段不完整时 eval 失败；SIGNAL 数值本身不决定退出码，N/A 必须附 reason。
- 验证：AUTO-API-EVAL-001。

## P1 接口扩展占位

### API-COLLAB-001｜复核并展开协作结构

- 输入：会议组织人原文对应的结构候选、参与者别名、阶段、最终负责人和 source spans；仅 COORDINATOR 可确认。
- 只接受 `PARALLEL_COLLECTION | SEQUENTIAL_HANDOFF | COLLECTIVE_DECISION`，并确定性展开为基础 ActionItem 与同一 Episode 内的最小依赖/参与关系。
- 未确认参与者、关系成环、缺最终负责人或缺来源时拒绝发布；不创建多 owner、Workflow 或 Stage 实体。

### API-COLLAB-002｜查询协作推进状态

- 输出结构、基础任务状态、等待对象、必要输入完成计数和引用的已验收版本；按现有 Principal 规则裁剪字段。
- 上游有效版本变化后，旧下游 lineage 必须标记失效并返回需要重开的基础任务，不能静默沿用。
