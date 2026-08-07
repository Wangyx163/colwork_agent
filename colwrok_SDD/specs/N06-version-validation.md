# N06｜交付接收、单任务成果处理与版本校验

版本：1.5  
变更依据：ADR-030、ADR-034

## P0 职责

### N06-001｜接收、分层与去重

- 每次提交只处理一个 ActionItem，并创建挂在该任务下的不可变 ArtifactVersion；提交至少含正文、链接元数据或附件引用之一。
- 提交者可以是任务负责人或当前协作者；ArtifactVersion 必须保存真实 `submitted_by_actor_id` 与 `contributor_role`。协作者提交仍进入原 ActionItem 的同一版本序列，不建立协作专用任务或交付实体。
- content_hash 必须包含提交人身份，避免负责人和协作者提交相同正文时错误复用另一身份语义的版本。
- 同一 ActionItem 与 content_hash 重复时复用已有版本，只追加重复接收审计。
- 新版本按 `(received_sim_time, received_sequence)` 排序；declared_version_label 仅展示。
- 标题/任务定义、正文、链接元数据、附件抽取文本必须分层保存并记录 source manifest，不能拼接成无来源文本。
- 附件二进制在生产环境只保存对象引用、MIME、大小与哈希，不直接发送给模型；允许发送经授权的标题、正文、链接元数据和附件抽取文本。
- 验证：AUTO-N06-001 至 AUTO-N06-003。

### N06-002｜确定性校验

- 按 deliverable schema 的字段类型、必填规则、大小、MIME、版本与确定性质量规则校验。
- 结构化必填、旧版判定、字段值和来源完整性不得由 LLM 最终裁决。
- 结果为 PASSED 或 FAILED，并保存可重算字段错误。
- 验证：AUTO-N06-004、REVIEW-N06-005。

### N06-003｜校验状态、协作贡献 Gate 与人工验收

- 负责人提交的最终候选在确定性校验 PASSED 后进入 `PENDING_ACCEPTANCE`，但不更新 current_valid_version_id，不自动把任务标为 ACCEPTED。
- 协作者提交的版本先标记为 `CONTRIBUTION`，`review_status=NOT_REQUIRED`，ActionItem 保持原 `TRACKING/ACCEPTED/AGGREGATED` 状态；不得因协作材料可读就推断整项任务完成。
- 任务负责人对每个待处理贡献版本只作一次决定：`INCLUDE` 纳入后续工作资料；`REQUEST_REVISION` 返回可执行反馈且要求协作关系仍有效；`PROMOTE` 明确该版本代表整项任务的最终候选，并将版本/ActionItem 转入 `PENDING_ACCEPTANCE`。三种决定均保留审计，不能由模型代替。
- AssistanceRequest 已结束时仍允许负责人 `INCLUDE/PROMOTE` 历史贡献，但 `REQUEST_REVISION` 必须重新邀请后执行；历史协作者保留自己贡献与协作记录的只读跟踪。
- 负责人在贡献分析开始前执行 `INCLUDE/REQUEST_REVISION` 时，系统把该版本的待处理作业改为 `NOT_REQUIRED`；已作业务决定的贡献不得继续被 Worker 发送模型。若调用已开始，只允许记录该次辅助结果，不能反向覆盖负责人决定。
- Worker 默认不开启未处理贡献的模型分析；只有部署入口显式启用 `allow_contribution_analysis`，且组织授权范围覆盖贡献正文/链接/附件抽取文本时才可调用。该开关不影响负责人直接处理贡献，也不允许发送附件二进制。
- 校验 FAILED：current_valid_version_id 保持原值；尚无有效版本时 ActionItem 保持 TRACKING 并触发返工决策，已有有效版本时保留原完成状态。
- 只有 COORDINATOR 人工验收通过时，才更新 current_valid_version_id、创建 AcceptedTaskResult 并将 ActionItem 设为 ACCEPTED；退回时回到 TRACKING 并保存结构化原因。
- 每次指针变化追加 `ActionItemCurrentValidVersionUpdated`。
- 验证：AUTO-N06-006 至 AUTO-N06-009。

### N06-004｜附件抽取与失败定位

- P0 MIME 路由：`text/*` 直接解码，`application/pdf` 由 PDF Adapter 抽取；其他格式显式 `UNSUPPORTED`。
- 系统写死的技术错误码：`AUTH_CONFIG | NETWORK_TIMEOUT | RATE_LIMIT | PROVIDER_4XX | PROVIDER_5XX | UNSUPPORTED_ATTACHMENT | EXTRACTION_FAILED | CONTEXT_LIMIT | INVALID_JSON | INVALID_SCHEMA | INVALID_CITATION`。
- Prompt 只允许返回语义状态：`INSUFFICIENT_EVIDENCE | AMBIGUOUS_TASK | SEMANTIC_MISMATCH`；不能让失败的模型诊断自身网络、鉴权或限流问题。
- 处理失败必须显示阶段、error_code、是否可重试和证据，不得静默生成低质量结果。
- 对 `NETWORK_TIMEOUT | RATE_LIMIT | PROVIDER_5XX | INVALID_JSON`，模型 Adapter 先执行有限次数的带退避请求内重试；耗尽后由 Worker 持久化失败。满足可重试条件的待验收版本可由 COORDINATOR 重新排队，复用原 ArtifactVersion 且不要求重新提交。
- 验证：AUTO-N06-010 至 AUTO-N06-013。

### N06-005｜验收辅助包

- 确定性校验通过后可进行一次模型调用：最终候选使用 `TASK_RESULT_REVIEW` 生成验收辅助包，协作贡献使用 `CONTRIBUTION_ANALYSIS` 说明其对整项任务契约的覆盖、缺口和可合并内容；两者均绑定 version_id。
- `TASK_RESULT_REVIEW` 仅处理尚未由 COORDINATOR 验收的负责人最终候选；经组织持续授权后，部署可通过 `COLWORK_RESULT_PROCESSING_MODE=bailian` 持久启用，不再逐版本确认。允许外发字段仅为任务标题、提交正文、链接元数据和附件抽取文本，禁止附件二进制。
- `CONTRIBUTION_ANALYSIS` 的 `PARTIAL/REVISE/ACCEPT` 只表示材料质量及“若直接提升”的建议，不迁移 ActionItem、review_status 或负责人决定。
- 旧构建把协作版本按最终候选 purpose 处理后，迁移必须清除旧辅助结果并幂等重新排队；不得只改 review_status 后沿用目的不一致的 AI 结论。
- 输入按 `task_definition/work_requirements/submission_text/link_metadata/attachment_extractions/source_manifest` 分层；只有 COORDINATOR 视图的调用可包含 `management_review_policy`。
- PARTICIPANT 只看到提交回执、抽取状态和通用可修正问题；COORDINATOR 看到完整辅助包、来源引用和管理侧验收规则。
- 模型 temperature 仅用于提高输出一致性；权限、schema、引用校验、状态变更和人工 Gate 由系统控制。
- 百炼 `json_object` 只视为“输出可解析 JSON”，不视为字段完整性保证；应用必须继续执行本地 schema 与引用校验。
- 标题、摘要、空数组、版本 ID 等能由当前 task contract、已读取证据或已验收来源唯一确定的展示字段，由系统确定性归一化并记录 `normalization_actions`，不得再次调用模型补写。
- 对错误来源引用、状态冲突等语义契约错误，最多发起一次带具体校验错误的模型修复并重新完整校验；二次失败固定为 `INVALID_SCHEMA`，不得逐字段循环修复或放宽规则。
- 证据结论与缺口说明使用不同引用强度：`evidence_digest/normalized_result` 只能引用已读取证据；`gaps` 可引用输入中存在但未读取的链接/附件来说明“未检查或证据不足”。P0 不核验链接真实性，也不能把链接网页内容写成已读取事实。
- `INVALID_SCHEMA` 不自动循环重试；当 Prompt/校验契约已升级时，COORDINATOR 可显式对同一 ArtifactVersion 重新排队，不要求执行人重新提交。
- 验证：AUTO-N06-014 至 AUTO-N06-017。

### N06-006｜AcceptedTaskResult

- 验收通过时冻结：完成内容引用、完成报告、normalized_result、source_manifest、processing_metadata、accepted_by 与 accepted_sim_time。
- 完成报告由系统从当前版本事实生成，COORDINATOR 可在验收前编辑；原始提交不可覆盖。
- 验收辅助包只提供证据和建议，模型不得批准或退回交付。
- 验证：AUTO-N06-018 至 AUTO-N06-021。

### N06-007｜晚到新版本

- ACCEPTED/AGGREGATED 后收到新版本仍须登记、校验和重新人工验收。
- 新版本验收通过后，旧 FinalDeliverable 修订变为 SUPERSEDED，待批 FINAL_RELEASE 同时失效，并由 N07 创建新终稿修订。
- 新版本校验失败或被退回时保留原 current_valid_version_id，不污染终稿。
- 验证：AUTO-N06-022、AUTO-N06-023。
