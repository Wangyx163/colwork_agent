# 多同事会议行动项协作 Agent｜系统级 SDD

版本：1.5  
变更依据：ADR-024、ADR-026、ADR-030、ADR-032、ADR-033

## 1. 系统边界

### SYS-SCOPE-001｜输入

- 会议转写、参会名单、ContentPack、RunContext、同事业务信号/求助/交付、会议负责人验收与终稿决定。
- 未声明输入必须返回稳定错误码，且不得改变业务状态。
- 验证：AUTO-SYS-SCOPE-001。

### SYS-SCOPE-002｜输出

- 标准 IM 消息、团队工期视图、单任务结果/协作报告/Memory 候选、结构化终稿、AuditEvent、分层验收报告与归档包。
- 每个输出必须可追溯至 run_id、episode_id 与 correlation_id。
- 验证：AUTO-SYS-SCOPE-002。

### SYS-SCOPE-003｜P0 排除

- 不接真实企微、钉钉或飞书；不做会议转写、RAG、多模态、多 Agent、并行 Episode、多任务评测、直属上级 L3 或未参会者外包。
- mock IM、脚本同事与单 ACTIVE Episode 是 P0 的明确边界，不是临时缺陷。
- 验证：REVIEW-SYS-SCOPE-003。

## 2. 运行架构

### SYS-ARCH-001｜逻辑模块

- N01–N15 是同一应用内的逻辑职责，不要求拆成服务或部署单元。
- 模块只能通过契约调用或领域事务协作，不得直接维护另一套流程状态。
- 验证：REVIEW-SYS-ARCH-001。

### SYS-ARCH-002｜单串行事件循环

- 同一 Tick 的输入按 `(sim_time, input_priority, accepted_sequence)` 排序。
- input_priority 冻结为：已到达的 IM/审批输入优先于同刻 SLA 调度触发；交付校验在入站事务中同步完成。这样恰好在截止时到达的有效交付不会先触发催办。
- Orchestrator 每次只提交一个领域事务；相同输入与配置必须得到语义一致的业务状态、AuditEvent 顺序与外发效果。
- “单串行”约束领域决策顺序，不要求 HTTP 与慢模型在同一线程；LIVE 模式由隔离 Worker 串行执行模型、Scheduler 与 Dispatcher，Web/API 只接收命令和查询状态。
- 验证：AUTO-SYS-ARCH-002。

### SYS-ARCH-003｜事务边界

- 一次业务决策所修改的领域表、对应 AuditEvent 与 OutboxEntry 必须在同一数据库事务写入。
- 任一步失败则整笔事务回滚，不允许出现“状态已变但消息永远不会发”或“消息已计划但无业务依据”。
- 验证：AUTO-SYS-ARCH-003。

### SYS-ARCH-004｜恢复入口

- 进程启动后读取 ACTIVE Episode、当前领域状态、到期调度与未终态 OutboxEntry，推导下一可执行步骤。
- 进程内队列和 LLM 上下文都不得成为恢复所需真相源。
- Agent Worker 启动从 PostgreSQL 恢复待处理版本与终稿 effect；VirtualClock/Scheduler 由 Harness 明确推进，不依赖墙上时间。
- 验证：AUTO-SYS-ARCH-004、HARNESS-SYS-ARCH-006。

### SYS-ARCH-005｜ContentPack 隔离

- 人员、会议文本、交付 schema、同事行为、SLA 与策略阈值均位于 ContentPack。
- 核心代码不得写入某一业务场景的专有分支。
- 验证：AUTO-SYS-ARCH-005。

## 3. 数据与错误

### SYS-DATA-001｜唯一状态源

- PostgreSQL 领域表是当前业务状态唯一读写源。
- AuditEvent 只追加、只用于审计和指标；不得依靠回放 AuditEvent 重建 P0 业务状态。
- 验证：AUTO-SYS-DATA-001。

### SYS-DATA-002｜不可变历史

- CommitmentRevision、ArtifactVersion、FinalDeliverable 修订与 AuditEvent 不得被覆盖或软删除。
- 更正通过新修订和 supersedes 引用表达。
- 验证：AUTO-SYS-DATA-002。

### SYS-DATA-003｜不变量失败

- 检测到 `INVARIANT_VIOLATION` 时，当前事务回滚，Episode 转为 BLOCKED，并生成可见错误摘要。
- 系统不得自动猜测或覆盖冲突数据。
- 验证：AUTO-SYS-DATA-003。

### SYS-ERR-001｜稳定错误分类

- 错误类型：`VALIDATION | NOT_FOUND | CONFLICT | RETRYABLE_INFRA | TERMINAL_INFRA | AUTHORIZATION | INVARIANT_VIOLATION`。
- 未完成动作必须体现为 API 错误、Outbox 状态或 AuditEvent，不能只写文本日志。
- 验证：AUTO-SYS-ERR-001。

## 4. 安全与可观测性

### SYS-SEC-001｜身份

- mock IM 根据调用凭证确定 sender_actor_id，消息体不得伪造发送者。
- 所有业务请求先由 PrincipalProvider 确定 actor；正文 actor_id 不可信。服务端校验 Episode membership、COORDINATOR/PARTICIPANT 角色、任务 owner 与字段 projection。
- 只有 Episode 的 COORDINATOR 可以验收单任务与决定 FINAL_RELEASE；PARTICIPANT 不得读取管理侧验收规则、他人正文或私有 Memory。
- 验证：AUTO-SYS-SEC-001 至 AUTO-SYS-SEC-004。

### SYS-OBS-001｜链路追踪

- AuditEvent、OutboxEntry、外部回执与 Approval 必须保留 correlation_id；由另一动作触发时保留 causation_id。
- 任一外发消息都必须能追溯到原始输入、策略原因与当前业务对象。
- 验证：AUTO-SYS-OBS-001。

### SYS-OBS-002｜审计导出

- 系统提供按 run_id 导出稳定排序 AuditEvent 的 JSONL 或等价格式。
- 导出用于审计，不承诺作为状态恢复输入。
- 验证：AUTO-SYS-OBS-002。

## 5. 非功能边界

### SYS-NFR-001｜可复现

- 仓库必须提供一条文档化命令运行 P0 固定场景，输出 GATE 与流程/效果/单点 SIGNAL。
- 正式运行冻结 ContentPack 版本、策略参数、时钟配置与可选模型标识。
- 验证：AUTO-SYS-NFR-001。

### SYS-NFR-002｜确定性

- 相同输入、ContentPack、时钟与配置下，结构化业务结果和 EffectId 序列必须一致。
- LLM 只可用于抽取或措辞；P0 可完全使用固定 fixture 与模板运行。
- 验证：AUTO-SYS-NFR-002。

### SYS-NFR-003｜故障降级

- 话术模型不可用时使用冻结模板；抽取不可用时进入人工确认或明确失败，不能伪造承诺。
- 验证：AUTO-SYS-NFR-003。

### SYS-NFR-004｜性能与 UI

- 单 Episode 的无 LLM Tick 在本地开发环境 p95 应小于 1 秒。
- P0 UI 只在首次进入、成功动作后或手动操作时刷新；不得用高频轮询覆盖表单草稿，也不得把刷新当业务信号。
- 验证：AUTO-SYS-NFR-004、MANUAL-SYS-NFR-005。

## 6. P0 推荐工程形态

### SYS-IMPL-001｜模块化单体

- 一个代码库、一个 PostgreSQL；API/UI、worker 与 eval CLI 共用领域和应用层。
- 推荐目录职责：`domain/`（实体与规则）、`application/`（用例与事件循环）、`adapters/`（mock IM/clock）、`persistence/`（事务与 Outbox）、`ui/`、`eval/`。
- worker 负责串行事件循环与单 Dispatcher；故障测试以独立进程启动/终止 worker。
- P0 不增加 Redis、Celery、消息中间件或独立工作流服务。
- 验证：REVIEW-SYS-IMPL-001。

### SYS-IMPL-002｜默认技术栈

- 在没有既有工程约束时，默认使用 Python 3.12、FastAPI、SQLAlchemy 2、Alembic、PostgreSQL、pytest 与服务端渲染的最小页面。
- 技术栈不是业务契约；若团队已有等价栈，可保持领域表、事务、EffectId、接口和测试语义不变地替换。
- 最少提供 `dev`、`test`、`eval` 三个入口；具体用 Make、PowerShell 或项目脚本均可。
- 验证：REVIEW-SYS-IMPL-002。
