# ADR Index

状态说明：ACCEPTED 为当前有效；SUPERSEDED 或 SUPERSEDED IN PART 的冲突部分以更新 ADR 为准；PROPOSED 不得覆盖已冻结规范。

## 初始 ADR

- ADR-001 ACCEPTED：场景为会后行动项跟进。
- ADR-002 ACCEPTED：交付物为结构化数据表。
- ADR-003 SUPERSEDED IN PART BY ADR-024：P0 固定为 3 名负责人、4 个任务和 4 个核心行为。
- ADR-004 SUPERSEDED IN PART BY ADR-024：P0 仅强制一个外发幂等恢复场景，其余进入 P1。
- ADR-005 ACCEPTED：单 Episode 内 ActionItem 模拟并行、事件循环串行，多 Episode 不并发。
- ADR-006 ACCEPTED：模拟器脚本决定行为，LLM 仅话术。
- ADR-007 ACCEPTED：升级由决策表决定，LLM 仅感知与话术。
- ADR-008 SUPERSEDED IN PART BY ADR-024：保留关键状态外置，删除朴素上下文基线。
- ADR-009 ACCEPTED：无状态轮询 Scheduler，无持久 timer。
- ADR-010 ACCEPTED：抽取一核两 adapter，同一性合并唯一归 CommitmentLedger。
- ADR-011 SUPERSEDED BY ADR-024：外发幂等改用持久化 EffectId，不再使用四元决策键。
- ADR-012 ACCEPTED：保留 IM Adapter，v1 使用 mock。
- ADR-013 ACCEPTED：UI 基础档。
- ADR-014 SUPERSEDED IN PART BY ADR-024：P0 使用固定 fixture 与模板；模型和缓存进入 P2。
- ADR-015 SUPERSEDED BY ADR-025：不再使用原项目名，当前只使用描述性标题。

## 为完成 SDD 明确化的 ADR

### ADR-016 SUPERSEDED BY ADR-024｜ActionItem 状态机

- 决策：采用 CANDIDATE→AWAITING_CONFIRMATION→CONFIRMED→COMMITTED→TRACKING→DELIVERED→REWORK_REQUIRED/ACCEPTED→AGGREGATED→ARCHIVED；逾期为派生谓词。
- 理由：避免将时间状态与业务生命周期混为一体。

### ADR-017 SUPERSEDED BY ADR-024｜DecisionEpoch

- 决策：每次正式提交新的外部动作决策时递增；重试和恢复复用原值。
- 理由：允许同类合法后续动作，同时保证恢复幂等。

### ADR-018 SUPERSEDED BY ADR-024｜版本排序

- 决策：latest_received 与 latest_validated 按接收时间和序号；latest_declared 使用 ContentPack 比较器；latest_approved 按审批时间。
- 理由：不同“最新”含义不可合并为一个指针。

### ADR-019 SUPERSEDED BY ADR-024｜Checkpoint 权重

- 决策：确认、承诺、首触达、升级合法、无旧版各占 20%。
- 理由：v1 不引入缺乏证据的复杂权重。

### ADR-020 SUPERSEDED IN PART BY ADR-024｜通用周会默认策略参数

- 决策：B=2；确认超时 0.5 工作日；L1 提前量 1 工作日；L2 等待 0.5 工作日；L3 为逾期 1 工作日。
- 理由：提供可运行默认值，正式结论依赖参数扫描而非默认值本身。

### ADR-021 SUPERSEDED BY ADR-024｜领域状态与事件日志

- 决策：采用“当前领域投影 + append-only 审计事件”的混合方式；不要求所有运行查询实时全量重放。
- 理由：满足可审计和恢复要求，同时控制 14 天实现成本。


### ADR-022 ACCEPTED｜Outbox 领取恢复

- 决策：v1 使用单活动 Dispatcher 数据库互斥；重启会话立即回收旧会话 CLAIMED entry，不以模拟时间租约判断。
- 理由：模拟时间在崩溃期间可能不推进，不能作为基础设施领取过期依据。

### ADR-023 ACCEPTED｜审批失效

- 决策：PENDING Approval 的业务前提变化时进入 SUPERSEDED；有效交付可使 L3 请求失效，新有效版本可使 FINAL_RELEASE 请求失效。
- 理由：禁止人工批准已经过期的动作或终稿。

### ADR-024 SUPERSEDED IN PART BY ADR-026/ADR-030｜MVP 业务优先级与单运行时

- 决策：保留一个 Agent 协调 3–5 名同事的完整业务主线，以 `12-mvp-priority-profile.md` 的 P0 为当前交付范围；运行时使用领域表、单事件循环、VirtualClock 与 Outbox，删除 LangGraph 和完整 Event Sourcing；学术型评测退出项目。
- 理由：在不改变项目主旨的前提下，将 10–15 日工作量收敛为可运行、可恢复、可验收的产品 MVP。
- 详情：`ADR-024-mvp-priority-and-runtime.md`。

### ADR-025 ACCEPTED｜移除项目名

- 决策：删除原项目名；规范统一使用“多同事会议行动项协作 Agent”作为描述性标题，暂不设置新产品名。
- 理由：产品命名尚未确定，不让临时代号进入正式表达。
- 详情：`ADR-025-remove-project-name.md`。

### ADR-026 ACCEPTED｜分层轻量验收指标

- 决策：以少量 GATE 判定交付，以 FLOW/EFFECT/NODE SIGNAL 解释流程、效果和单点健康度；SIGNAL 不强设通过线。
- 理由：验收既要能下结论，也要能定位卡点，但不恢复论文式重评测。
- 详情：`ADR-026-layered-acceptance-metrics.md`。

### ADR-027 ACCEPTED｜P0 Fixture 数据来源与止损

- 决策：优先复用 AliMeeting4MUG / AMC-A 的少量中文行动项句并补项目最小标注；外部访问超过 1 小时即切换项目自建 fixture，不新增数据集领域实体或训练管线。
- 理由：减少会议文本构造时间，同时避免下载、清洗和学术评测阻塞 10–15 日产品主链。
- 详情：`ADR-027-fixture-data-source.md`。

### ADR-028 SUPERSEDED BY ADR-035/ADR-036｜复用 ActionItem 实现任务认领（历史兼容）

- 保留结论（由 ADR-036 收窄）：语义充分的 draft_items 直接成为 owner/deadline 可空的 PENDING_CONFIRMATION ActionItem；语义不足项进入 extraction artifact 的 review_hints，不建立候选任务或 Claim 领域实体。
- 被取代部分：公开待认领池、竞争认领和认领时创建承诺；现由 ADR-035 的版本化派发/逐人响应/整轮退回替代。
- 详情：`ADR-028-claim-without-candidate-entity.md`。

### ADR-029 SUPERSEDED IN PART BY ADR-030｜角色页面与人工交付验收

- 决策：拆分同事、负责人和诊断页面；真实交付进入 PENDING_ACCEPTANCE，由负责人验收后才完成；未来 IM 入口只替换身份与交互适配器。
- 理由：把业务操作与工程验收分开，并为 B→C 迁移保留稳定领域边界。
- 详情：`ADR-029-role-surfaces-and-human-acceptance.md`。

### ADR-030 ACCEPTED｜最小权限、事件信号、双工期与协作记忆

- 决策：P0 只保留会议负责人、参会者与系统三类 principal；以可信虚拟身份、服务端动作校验和字段裁剪形成可迁移的最小权限层；用业务事件与异常询问替代高成本手写进展；求助对象只限本次会议参与者；团队需要时间与个人承诺时间分开；验收后生成单任务协作报告与 Memory。公开认领和自由文本 Memory 已由 ADR-035 修订；直属上级和 L3 延后。
- 理由：维持多同事协作主旨，同时控制飞书接入成本、接口暴露与 P0 实现复杂度。
- 详情：`ADR-030-minimal-access-signals-schedule-memory.md`。

### ADR-031 ACCEPTED｜先闭环基础任务，协作结构模板进入 P1

- 决策：P0 只闭环相互独立的单负责人基础 ActionItem；多人并行收集、顺序交接和多人参与决策作为可组合协作结构模板进入 P1，并补充在现有 N01/N02/N13，不使用小数模块编号。默认协作者的贡献权限由 ADR-034 修订。

### ADR-034 ACCEPTED｜协作者复用任务与版本链

- 决策：会议明确协作者与有效求助目标复用原 ActionItem、协作记录和 ArtifactVersion 版本链，可记录状态和提交交付；仍保持单 owner、负责人独有承诺、会议负责人独有验收，不新增协作任务或第二套流程实体。
- 理由：让真实协作者获得与任务执行一致的工作空间，同时保留单一责任与 lineage，避免重复造轮子。
- 理由：保留真实会议中特殊推进结构的产品方向，同时避免在基础任务闭环尚未稳定时引入依赖、多人参与和第二套流程状态。
- 详情：`ADR-031-base-task-first-collaboration-patterns-p1.md`。

### ADR-032 SUPERSEDED IN PART BY ADR-033｜真实业务时钟、独立运行循环与主动通知进入 P0

- 决策：P0 同时支持评测 VirtualClock 与真实工作台 SystemClock；Web/API 和持久化 Worker 隔离运行；求助生命周期进入统一 Outbox 通知；通用投递补齐重试、死信和启动恢复，并以 Live E2E 验收。
- 理由：领域测试通过不能替代生产运行闭环；这些能力复用既有 Scheduler、AssistanceRequest 与 OutboxEntry，不需要新增工作流或通知实体。
- 详情：`ADR-032-live-runtime-worker-outbox-closure.md`。

### ADR-033 ACCEPTED｜面试项目聚焦可恢复单 Agent AI 工程主线

- 决策：保留独立 Agent Worker、状态外置、HITL、版本 lineage 和一个幂等恢复黄金场景；砍掉 P0 真实 SystemClock、完整通知与生产故障矩阵，新增 purpose-scoped Context/Token 管理和精简 Evaluation Harness。
- 理由：面试项目应以可现场证明的 AI 工程能力为主，办公业务只提供真实载体，不追求企业级产品完整性。
- 详情：`ADR-033-interview-ai-engineering-focus.md`。

### ADR-035 ACCEPTED｜版本化多人派发与个人执行视图

- 决策：以“一个主负责人＋零到多个协作者”的版本化派发替代公开认领；成员逐人接受或退回，任一退回使整轮失效并回到负责人修改；个人端使用闹铃、个人时间线和最小协作投影，Memory 使用预制词条。
- 理由：支持真实多人协作和任务定义返修，同时保持单 ActionItem、单 owner、单 ArtifactVersion lineage 与既有两级人工 Gate。
- 详情：`ADR-035-versioned-dispatch-and-personal-execution-view.md`。

### ADR-036 ACCEPTED｜召回优先的抽取与复核提示

- 决策：内部区分候选发现与结构整理，但产品只保留一个负责人复核表面；使用一套统一宽召回 Prompt 与确定性规则补网取并集，按上下文充分性自适应扩展。语义不足项进入 pre-Episode extraction artifact 的 `review_hints`，由负责人添加任务或忽略，不建立 ActionItemCandidate 领域实体。
- 理由：会议 Agent 的漏召成本高于错召；字段不全、跨句证据或单候选失败不得把已发现语料静默删除。固定 3–5 units 与双 Prompt 均无现有证据支持，应先建立可观测、可消融的单 Prompt 基线。
- 详情：`ADR-036-recall-first-extraction-and-review-hints.md`。
