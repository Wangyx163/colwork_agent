# 办公协作 Agent｜AI 能力资产化与运行路由

版本：1.6  
适用范围：P0 生产化主链与后续多任务扩展

## 1. Assetization Diagnosis

- Current shape：多个确定性业务节点、外部模型调用、文件工具、定时协调 Loop 与人工审批共同组成的产品系统。
- Recommended layer：`System`。
- Confidence：High。
- 理由：单个 Prompt 无法承担身份、状态、重试、成本、审计和审批；单个 Skill 也不应成为第二套工作流引擎。
- 最小实现：确定性领域系统 + 版本化 Prompt + 格式 Skill/Adapter + 复用现有 Outbox 的可恢复 Loop。
- 复用信号：同一输入版本可定位使用的 Prompt/Skill 版本，失败可重试，输出可验收且不污染原始交付。
- 停止信号：出现第二套任务状态、万能 Agent Router、未声明外发、模型直接改变责任/审批/版本指针。

## 2. 真实业务链路路由

| 场景 | 主要资产层 | 运行方式 | P0 决策 | 验收信号 | 优先级 |
|---|---|---|---|---|---|
| 会议行动项抽取 | Prompt | 固定 system prompt + JSON schema + source evidence 对齐 | 模型只提候选；引文失败最多做一次只改证据字段/删除无证据候选的修复 | schema 合法、原文证据存在、未决字段显式、修复不改变任务语义 | P0 |
| 责任确认、版本化派发/响应、改期 | System | 权限、状态机、事务、幂等 | 不调用模型或 Skill | 只有本人回应；任一退回整轮失效；历史不可覆盖 | P0 |
| 事件信号与 L1/L2 异常询问 | Harness + Rule | VirtualClock + 信号 allowlist + 决策表 + Outbox | 不让 LLM 判断是否失联；刷新/已读不算信号；不建设真实日历 | 固定场景可复现、EffectId 不重复 | P0 演示 |
| PDF/文本附件读取 | Skill/Adapter | MIME 路由到受限解析器 | PDF/text 进入；二进制不直接进入 Prompt | 抽取状态、字符数、截断和错误可见 | P0 |
| DOCX/XLSX/网页读取 | Skill/Adapter | 文档、表格、浏览器/抓取适配器 | 不在当前 P0 假装支持；按格式逐个接入 | 真实文件回归样例与解析质量 | P1/P2 |
| 交付格式校验 | System | 必填、类型、大小、版本与去重规则 | 确定性阻塞 Gate | 错误可重算、失败版本保留 | P0 |
| 单任务验收辅助包 | Prompt | 使用分层任务定义、管理侧规则、正文及附件抽取文本输出结构化建议 | 一次调用；只辅助 COORDINATOR，不自动验收 | 结论附 version/source 引用；错误可定位 | P0 |
| 任务协作报告/Memory 候选 | System | 从已验收事实生成报告和版本化预制词条候选 | 候选默认私有，须本人确认/同 topic 替换/拒绝；禁止自由文本与评价标签 | 来源完整、可替换、不参与权限/验收 | P0/P1 |
| 多结果终稿整理 | Prompt + Loop | 固定输出 schema；Outbox 持久化触发、重试和恢复 | 条件满足后系统主动调用；不要求用户点按钮 | Prompt 版本可查、来源版本完整、失败不静默降级 | P0 |
| 问题收集—投票结构 | Prompt + System | Prompt 只提出带原文的结构建议；负责人确认上游/最终负责人/投票人/保留数，规则建立最小关系 | 只做一个白名单场景，不建设通用工作流 DSL | 成员明确、无环、下游只读已验收版本、等待原因可解释 | P1 |
| Ballot 候选草稿 | Prompt + Rule Fallback | qwen-plus/temperature=0 对已验收上游问题做语义抽取和去重；最终负责人确认 | 契约修复最多一次；模型不决定来源、开票、投票或入选 Gate | 每项 accepted version 来源合法；失败可确定性降级或明确停止 | P1 |
| 外部调研以补做任务 | Skill/Agent | 浏览器、搜索、证据采集、引用与独立验收 | 不属于协调 Agent P0；不得拿模型常识补造同事成果 | 来源可核验、与原行动项分开验收 | P2 |
| 终稿审批与发布 | System + Human Gate | 审批、Outbox、归档 | 模型不得批准自己生成的终稿 | 无审批绕过、发布幂等 | P0 |
| 指标与回归 | System | 从领域表和 AuditEvent 重算 | 不用 LLM 决定 Gate | 同轨迹同结果、失败可定位 | P0 |

## 3. Prompt、Skill 与确定性代码的边界

### 3.1 标准化成 Prompt

满足以下条件时使用 Prompt：输入已转为文本/结构化数据，核心工作是语义判断或表达转换，不需要操作外部工具。

P0 Prompt 必须具备：

- 稳定名称与 `prompt_version`；
- 固定输入字段和 JSON 输出 schema；
- 禁止编造、来源引用和失败条件；
- 模型、token usage、精确输入 input_hash 与输出一同记录；运行事件还必须携带 N10 定义的 invocation manifest；
- Provider 的 JSON mode 只保证可解析 JSON，不等于 schema 字段完整；应用层必须校验。
- 可由权威输入唯一确定的展示字段先做确定性归一化并记录动作，不消耗第二次模型调用；错误引用、状态冲突等语义错误最多定向修复一次，仍失败则进入 `INVALID_SCHEMA`/重试状态，不逐字段循环追问。

### 3.2 标准化成 Skill/Adapter

满足以下条件时使用 Skill/Adapter：需要调用专门工具、处理特定文件格式、访问网页或执行多步机械操作。

P0 不建设通用 Skill 注册中心，只做显式 MIME 路由：

- `application/pdf` → PDF 文本提取 Adapter；
- `text/*` → 文本解码 Adapter；
- 其他格式 → `UNSUPPORTED`，不能假装已处理。

Skill 输出先标准化为文本、表格或证据对象，再交给 Prompt；Skill 不直接改变 ActionItem、Approval 或 FinalDeliverable 状态。

### 3.3 必须留在确定性系统

- actor 身份与权限；
- ActionItem、CommitmentRevision、ArtifactVersion 状态；
- 双工期、信号 allowlist/过期、预算和 L0–L2 决策表；
- Principal、membership、owner、字段 projection 与 purpose allowlist；
- 幂等、事务、版本指针、lineage；
- 审批、发布、归档和 GATE 指标。

## 4. 自动终稿整理 Loop

触发条件：所有 `required=true` 的 ActionItem 均为 `ACCEPTED/AGGREGATED`，且存在匹配的 `current_valid_version_id + AcceptedTaskResult`。

1. 系统按当前有效版本集合生成稳定 `input_hash`。
2. 复用 Outbox 创建唯一 `FINAL_ORGANIZATION` effect；不新增第二套任务实体。
3. Worker 读取文本正文并调用格式 Adapter 抽取附件内容。
4. 按部署策略调用 `bailian` 或本地确定性处理器。
5. Prompt 输出必须通过章节完整性和版本引用校验。
6. 成功后创建 FinalDeliverable 新修订和 FINAL_RELEASE Approval。
7. 失败进入 `RETRY_WAIT`，保留错误、尝试次数和 AuditEvent；达到上限进入 `DEAD_LETTER`，不得静默生成低质量终稿。
8. 输入版本变化时旧 effect 跳过，新版本集合产生新 effect。

其中版本 fingerprint 只负责 EffectId 与过期检查；审计中的 `input_hash` 必须哈希实际送入整理器的 binary-free source bundle，二者不可混用。

部署级策略：

- `bailian`：允许把确定性校验通过、尚待会议负责人验收的最终候选之任务标题、提交正文、链接元数据和附件抽取文本发送给百炼；不发送附件二进制，模型只生成验收辅助包。
- `local`：只使用确定性模板，适合离线演示和测试。
- `disabled`：关闭自动整理。

策略由运维启动参数配置一次，业务用户不需要逐次确认或充当系统中间环节。

## 5. 当前不做

- 不把 Codex 本地 Skills 当作生产运行时依赖；生产能力必须有项目内 Adapter、契约和测试。
- 不建设万能 Skill Router、插件市场或 LangGraph。
- 不让终稿模型替同事上网补做原任务。
- 不用模型输出直接覆盖原始 ArtifactVersion。

## 6. Context 与 Token 管理

每次 MODEL invocation 在既有 manifest 上增加 `included_refs`、`omitted_refs`、`token_budget`、`estimated_input_tokens`、`truncation_strategy` 和实际 `usage`。上下文按 purpose 装配，禁止默认拼接全量聊天/会议历史；原始事实仍保留在 PostgreSQL/ArtifactVersion，不因上下文裁剪丢失。

## 7. 研发 Agent 模型路由

生产系统中的业务模型路由与“由哪个编码 Agent 实现功能”是两套概念，不得混用。研发任务按 `17-ai-interview-p0-plan-and-model-routing.md` 执行：

- Luna 只承担局部、确定、有现成测试 oracle 的完整小闭环；当前环境无直接 Luna 档位时回退 `gpt-5.6-terra/low`。
- `gpt-5.6-terra/medium` 承担已冻结契约下的常规数据/API/UI 状态闭环。
- `gpt-5.6-terra/high` 承担跨层状态、lineage、Adapter/Outbox 接线。
- `gpt-5.6-terra/xhigh` 只用于黄金幂等恢复的最终审计；日常实现不默认使用。
- 不得把一个 T3/T4 高风险任务横向拆碎成多个 Luna 任务来规避整体推理；关键不变量由原等级模型复核，Gate 由测试和人工决定。
