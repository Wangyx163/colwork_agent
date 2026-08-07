# N10｜身份、权限与 Agent 上下文装配

版本：1.3  
变更依据：ADR-030

## P0

- 所有入口先取得可信 `Principal{actor_id, episode_id, roles, auth_source}`，再按 `purpose` 组装字段 allowlist。HTTP 请求正文、模型文本和页面 localStorage 都不是身份事实。
- 决策模块直接读取 ActionItem、ACTIVE CommitmentRevision、current_valid_version_id、未解决 AssistanceRequest、最后有效信号、触达预算与 VirtualClock。
- 执行人、两类日期、有效版本等关键状态不得从历史消息或摘要重新推断。
- 模型处理单任务时使用分层结构：`task_definition`、`work_requirements`、按权限决定是否包含的 `management_review_policy`、`submission_text`、`link_metadata`、`attachment_extractions[]`、`source_manifest`。
- `management_review_policy` 只能进入 COORDINATOR 查看或 `TASK_RESULT_REVIEW` purpose 的 SYSTEM 上下文；PARTICIPANT、求助目标和通用进展 Prompt 不可读取。
- CollaborationMemory 默认不注入；仅在 `COLLABORATION_HINT` purpose 下读取本人已确认的最小偏好，不得用于授权、验收或升级。
- 每次模型/Skill 调用在既有 AuditEvent payload 中记录 invocation manifest：`capability_type`、可信 SYSTEM principal、purpose、字段 allowlist、实体版本、prompt/skill version、精确输入 `input_hash` 和输出状态；不得为此再建第二套调用流程实体。
- 会议抽取发生在 Episode 创建前时，抽取结果文件先记录 `episode_id=null` 的 SYSTEM invocation；导入后由 `EpisodeActivated.extraction_invocation` 绑定真实 episode_id，全文不复制进 AuditEvent。
- `TASK_RESULT_REVIEW` 必须把 `deliverable / acceptance_criteria / work_requirements / management_review_policy` 保持为独立字段；管理规则只进入 SYSTEM 的验收 purpose，不进入 PARTICIPANT projection。
- PDF/text Adapter 在解码前先校验任务 owner；审计只保留 MIME、大小、内容哈希、抽取状态与调用 manifest，不保存附件二进制/base64。
- 模型失败显式写 error_code、阶段、retryable 与 invocation 输出状态；终稿调用的 input_hash 必须对应实际 source bundle，不得只哈希版本 ID。
- 验证：AUTO-N10-001 至 AUTO-N10-006。

## P1

- 加入有界最近消息窗口、token_count 与更细引用实体版本审计。
- 消息淘汰不得影响结构化当前状态。
- 经本人确认后，按可见性向未来同场协作者提供最小协作提示。

## P2

- 多任务 AgentAdapter 可定义自己的上下文装配器，但必须通过同一领域读取和授权接口访问本任务状态。
- 飞书 PrincipalProvider 替换 P0 签名虚拟会话，不改变领域角色与字段权限。
