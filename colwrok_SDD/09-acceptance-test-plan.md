# 多同事会议行动项协作 Agent｜验收测试计划

版本：2.0  
变更依据：ADR-030、ADR-031、ADR-032、ADR-033、ADR-035

## 1. P0 测试分层

P0 要求 unit、contract、integration、recovery 与 end-to-end 五层。

### Unit

- Principal、membership/role/owner 授权与字段 projection。
- 双工期、schedule conflict、信号有效期、无信号询问决策表与每日预算。
- AssistanceRequest、ActionItem、Approval、Memory 和 OutboxEntry 状态转换。
- ArtifactVersion/AcceptedTaskResult 版本绑定、EffectId 与重复输入去重。
- 模型/Adapter invocation manifest 的 principal、purpose、字段 allowlist、实体版本、prompt/skill version、input_hash 与成功/失败状态。
- 5 个 GATE 及全部 SIGNAL 的正常、零分母和 N/A fixture。

### Contract

- 虚拟会话 Principal 与 mock IM 身份一致性。
- 抽取/复核/版本化派发/逐人响应、快捷信号、个人改期、参会者求助。
- 分层提交、附件抽取、技术错误码/语义状态、验收辅助包。
- 人工验收、FINAL_RELEASE、Memory 查看/预制词条替换。

## 2. P0 集成测试

### TST-INT-001｜身份、复核与版本化派发

- COORDINATOR 复核任务、设置 team_required_by，并派发给一名主负责人和多名协作者；成员逐人接受或退回重改。
- 通过：请求正文伪造 actor_id 无效；未被派发/未参会 actor 被拒绝；全部当前版本成员接受前 owner、协作者写权限和 ACTIVE CommitmentRevision 不激活。
- 通过：任一成员退回后 ActionItem=NEEDS_REVISION，同轮其他 PENDING/ACCEPTED assignment=SUPERSEDED；修改并重派时 definition_version+1 且所有成员重新回应。
- 通过：协调人被显式列入参会名单时，同一 actor 同时具有 COORDINATOR/PARTICIPANT membership 并可被派发本人任务；未列入时不得因协调人身份自动获得参与者写权限。
- 通过：抽取引文必须存在于声明时间戳；短确认改写触发最多一次证据修复，修复只能重绑原文或删除无证据候选，不能改变任务语义。
- 通过：发言人提出“我觉得可以”时不得自动成为 owner；明确“某人负责”或“我去做”才保留 owner hint；会议内即时操作和纯建议不作为会后任务发布依据。
- 通过：长逐字稿按完整发言轮次分块并保留重叠，块间候选汇总后仍以整份原文校验；网络瞬断只增加 transport attempt，不重复创建逻辑候选。
- 通过：成功片段写哈希作用域检查点；后续片段失败后重跑命中既有片段且零网络调用，输入/Prompt/模型/分块任一变化时不得复用旧检查点。
- 通过：模型返回空负责人/空截止但 `needs_confirmation=false` 时，系统只做一次 fail-closed 结构归一化并记录动作，不增加模型调用；可空字段缺失、空字符串及异常 `uncertainties` 可安全归一化，但标题、交付物、置信度和原文证据缺失仍失败。

### TST-INT-002｜双工期与团队可视

- 执行人分别提交早于、等于和晚于 team_required_by 的 promised_by。
- 通过：个人承诺可修订但不覆盖团队日期；晚于时产生 conflict；COORDINATOR 看团队明细，PARTICIPANT 只看本人任务时间线与会议聚合，不见他人个人承诺、正文和管理规则。

### TST-INT-003｜无信号与异常询问

- 在截止时间很远、进入 check_in_lead、信号过期、存在待验收版本、存在未解决求助等条件推进 VirtualClock。
- 通过：远离截止时不问；刷新/已读不延后询问；仅满足全部条件时发一次 L1/L2；快捷回应更新有效信号；冷却与预算有效。

### TST-INT-004｜参会者求助

- 执行人向参会者发起并解决求助，同时尝试选择未参会者。
- 通过：只允许本 EpisodeParticipant；请求状态完整；未解决期间抑制泛化进展询问；系统推荐必须由发起人确认。

### TST-INT-005｜单任务成果处理

- 使用“标题+正文+PDF/text”提交一个 ActionItem，并制造不支持格式、抽取失败、无效 JSON、语义证据不足。
- 通过：各来源分层且绑定 version_id；不发送附件二进制；技术失败由系统 error_code 定位，语义不足由 Prompt 状态表达；失败可重试且不静默降级。
- 通过：附件解析前先校验 owner；Adapter 审计不含 base64；模型上下文中的执行要求、公开验收标准与管理侧复核规则保持独立，调用 manifest 可由实际输入重算。
- 通过：模拟模型同时遗漏成果标题与摘要时，系统从现有证据确定性归一化且不追加模型调用；模拟错误来源引用时最多修复调用一次，二次不合格固定报 `INVALID_SCHEMA`。

### TST-INT-006｜人工验收与结果冻结

- 校验通过后查看不同角色 projection，执行一次退回/重交，再验收通过。
- 通过：PARTICIPANT 不见 management_review_policy；COORDINATOR 见完整辅助包；通过前 current_valid_version_id 不变；通过后 AcceptedTaskResult 与版本一致。

### TST-INT-007｜协作报告与 Memory

- 任务验收后生成报告/Memory 候选，由本人确认、同 topic 预制词条替换或拒绝，并尝试提交词表外值。
- 通过：报告引用任务事实；Memory 默认私有、本人只能从版本化词表维护；COORDINATOR 只能看事实报告；词表外/评价标签不落库；当前协作者只得到 CONFIRMED 最小提示且不含 evidence/history；Memory 不改变权限或验收。

### TST-INT-008｜聚合与发布

- 全部 required 任务验收后聚合，随后一个任务提交并验收新版本。
- 通过：终稿只引用各任务 current_valid_version_id/AcceptedTaskResult；新版本使旧终稿/Approval 失效；只有 COORDINATOR 可 FINAL_RELEASE。
- 通过：终稿整理的排队、开始、重试/失败与成功事件使用同一 invocation manifest；input_hash 等于实际 binary-free source bundle，失败不遗留伪成功终稿。

### TST-INT-009｜协作贡献与最终候选双 Gate

- 当前协作者提交可读成果；通过：生成同一 ActionItem 下的不可变贡献版本，真实提交人可追溯，ActionItem 保持原状态，Worker purpose 为 `CONTRIBUTION_ANALYSIS`。
- 任务负责人分别验证要求补充和提升送验；通过：负责人决定前 COORDINATOR 无待验收卡，`PROMOTE` 后才进入 PENDING_ACCEPTANCE，最终仍须 COORDINATOR 人工验收。
- 解决 AssistanceRequest 后切换协作者视角；通过：不能再提交或被直接要求返修，但仍能在“我参与过的任务”查看自己的贡献与后续任务状态；重新邀请后才恢复写权限。
- 重启一份旧构建产生的未验收协作者候选；通过：只修复一次为贡献版本并恢复任务原状态，已显式 `PROMOTE` 的版本不得被降级。

### TST-P1-001｜问题收集—Agent 草稿—投票—定稿

- 配置两个上游收集任务、一个最终负责人、指定投票人和保留数量；通过：依赖只允许同 Episode、无环，未验收上游不能进入 ballot Context。
- 上游逐项人工验收后生成草稿；通过：每个候选保留 accepted version 来源，规则抽取可去重；qwen-plus 契约不合格最多修复一次，仍失败时只有规则得到至少两个可靠候选才可降级。
- 最终负责人删选后开票；通过：正式 ballot 锁定，投票人必须给全部候选 1–5 整数分，首次提交后不同 message 也不能改票；投票未齐时服务端阻止定稿提交。
- 全部投票后按总分/平均分/option_id 排序并只解锁最终负责人普通提交；上游换版时旧 ballot、投票和下游有效版本失效并回到 TRACKING。

## 3. P0 恢复测试

### TST-REC-001｜IM 已接受但本地未确认

- 在 mock IM 接受 OutboundCommand 后、OutboxEntry 标记 DELIVERED 前终止进程。
- 重启后以原 EffectId 重投。
- 通过：mock IM 只有一个 external_message_id；Outbox=DELIVERED；业务主链继续。

## 4. P0 End-to-End

### TST-E2E-001｜多参会者完整闭环

- 参与者：1 名 COORDINATOR、3 名 PARTICIPANT；无经理角色。
- 任务：4 个 required ActionItem，每项独立提交和验收。
- 链路：会议抽取 → 复核/团队日期/版本化派发 → 逐人响应/个人承诺 → 工期冲突 → 有效信号与一次无信号询问 → 参会者求助 → PDF/text/正文提交 → 校验/辅助处理 → 退回/重交 → 逐项验收/结果冻结 → 报告/Memory → crash/restart → 聚合 → FINAL_RELEASE → 归档。
- 通过：
  1. GATE-E2E：四个任务、结果、终稿和 Episode 归档；
  2. GATE-DUP：重复外发 0；
  3. GATE-VER：旧版本、缺 lineage、结果版本错配均 0；
  4. GATE-GUARD：权限、字段泄露、未参会求助、发布和预算绕过均 0；
  5. GATE-REC：关键崩溃恢复成功；
  6. FLOW/EFFECT/NODE SIGNAL 均有值或合法 N/A；
  7. 重启不依赖进程内状态、模型上下文或第二套流程记录。

## 5. CI 与人工验收

- 每次提交运行不依赖外部 LLM 的 unit、contract、integration；百炼使用录制契约 fixture。
- 合入 P0 前运行一次真实百炼/PDF smoke test、recovery 与 E2E 并保存报告。
- 人工检查应能回答：“谁在做、团队何时需要、个人承诺何时、最后是什么信号、向谁求助、交了哪个版本、处理失败在哪、为何退回、验收结果在哪、Memory 能否替换/拒绝、终稿引用哪版”。

### TST-AGENT-001｜可恢复单 Agent Harness

- 启动 Web/Agent Worker，使用 PostgreSQL 外置状态和 VirtualClock 驱动完整会议任务链；中途停止 Worker 并从数据库恢复。
- 注入 mock IM 已接受、Outbox 尚未确认时崩溃；通过：恢复使用原 EffectId、重复副作用为 0、工作流继续。
- 输出四组报告：抽取 groundedness、工作流/HITL/lineage、恢复/幂等、上下文/Token。
- 不要求 SystemClock、生产日历、完整通知失败矩阵或上传安全压力测试。

## 6. P1 协作结构验收占位

- 使用“每人准备问题 → 指定成员汇总 → 全员投票 → 指定成员定稿”的真实会议片段。
- 验证结构建议只来自会议组织人原文；“每个人”只按确认后的 EpisodeParticipant 展开，未确认别名阻塞发布。
- 验证上游未完成时下游显示等待对象且不错误催办下游；全部必要输入完成后自动解除 Gate。
- 验证下游只读取上游已验收版本；上游有效版本变化后旧下游 lineage 不再有效。
- 验证投票/反馈参与状态可追踪，但不会生成多 owner 或绕过最终负责人验收。
