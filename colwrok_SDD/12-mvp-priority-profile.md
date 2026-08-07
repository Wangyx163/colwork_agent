# 多同事会议行动项协作 Agent｜MVP 组织、用户与优先级 Profile

版本：2.0  
上游决策：ADR-024、ADR-026、ADR-030、ADR-031、ADR-032、ADR-033、ADR-034、ADR-035  
适用范围：10–15 个工作日的首个可运行版本

## 1. 项目一句话定义

系统把一次会议拆成多项任务，由会议负责人派发给一名主负责人和必要协作者，大家在同一任务里接受、协作和交付，系统主动发现失联或工期风险，逐项验收后再汇总放行。

## 2. P0 组织与角色

| 角色 | 业务责任 | 必要权限 | P0 不承担 |
|---|---|---|---|
| COORDINATOR（会议负责人） | 复核/发布任务、维护团队时间、验收单任务结果、放行终稿 | 本 Episode 管理视图和决定权 | 组织级 IAM、直属上级管理 |
| PARTICIPANT（参会者） | 回应派发，作为负责人或协作者推进同一任务、快捷回应、向参会者求助、提交和返修 | 本人负责/协作任务及同任务成员关系；会议级聚合进度 | 查看无关任务、他人个人承诺/进展/正文、管理侧规则、他人完整 Memory |
| SYSTEM | 抽取、事件推导、异常询问、校验、成果处理、聚合、报告与 Memory 候选 | 按 purpose 的最小上下文 | 代替人接受派发、验收、选择求助人或放行 |
| 工程评测者 | 运行 fixture、故障恢复和分层指标 | 独立诊断页 | 参与业务决定 |

P0 不做普通团队成员、LINE_MANAGER 或组织管理员角色。会议负责人暂时也是任务发起/验收人；ActionItem.owner_actor_id 只表示协作执行人。

## 3. 业务分层

| 层 | 目标 | 完成信号 |
|---|---|---|
| B0 身份与上下文 | 确认谁在以什么目的操作哪些字段 | actor 不来自正文；跨角色/跨 Episode 访问被拒绝 |
| B1 责任落定 | 从会议识别任务，由负责人复核并派发给一名 owner 与 0..N 协作者 | 全部当前版本成员接受后才激活执行；任一退回则整轮失效并重派 |
| B2 团队工期 | 个人承诺不覆盖团队需要时间，个体细节按角色裁剪 | COORDINATOR 读取团队明细；参与者只读本人时间线和会议聚合，冲突可解释 |
| B3 主动协作 | 从业务事件推导进展，无信号时低成本询问，需要时向参会者求助 | 信号、询问、求助均有来源和生命周期 |
| B4 单任务成果 | 负责人/协作者向同一版本链提交；负责人先处理协作贡献，只有最终候选才由会议负责人验收 | contribution/final candidate 分层、真实提交人、accepted version 和 AcceptedTaskResult 可追溯 |
| B5 记忆 | 验收后生成事实协作报告及预制协作习惯候选 | 报告绑定任务/版本；Memory 本人可确认、同 topic 替换或拒绝，当前协作者只读最小提示 |
| B6 汇总放行 | 只聚合各任务已验收版本并人工放行 | lineage 完整、FINAL_RELEASE 通过并归档 |
| B7 可靠性与评测 | 主链可恢复、可重算、能定位流程/效果/单点问题 | 5 GATE 可判定，SIGNAL 齐全 |

## 4. P0 实现顺序与工期

| 顺序 | 工作包 | 主要产物 | 预计开发+测试 |
|---|---|---|---|
| P0-0 | 最小身份、授权、字段 projection | 签名虚拟 Principal；membership/owner/role 校验；服务端裁剪 | 1–1.5 天 |
| P0-1 | 双工期与团队可视 | team_required_by、promised_by、冲突事件、团队工期表 | 1–1.5 天 |
| P0-2 | 事件信号、异常询问、参会者求助 | signal allowlist/expiry、决策表、快捷回应、AssistanceRequest | 1.5–2 天 |
| P0-3 | 单任务成果处理与验收 | 分层输入、PDF/text 抽取、错误码、验收辅助包、AcceptedTaskResult | 2–3 天 |
| P0-4 | 协作报告与 Memory 基线 | 事实报告、PRIVATE_DRAFT、本人确认/预制词替换/拒绝 | 1–1.5 天 |
| P0-5 | 聚合、发布、恢复与回归 | 终稿 lineage、FINAL_RELEASE、Outbox crash recovery、E2E | 2–3 天 |
| P0-6 | AI Agent 展示闭环 | 隔离 Agent Worker、Context/Token Manifest、黄金幂等恢复、精简 Harness | 2–3 天 |
| 缓冲 | 真实文件/模型联调 | 百炼输出、Token 统计、演示脚本与 UI Trace | 1–2 天 |

当前实现已通过 102 项自动化测试（另有 1 项 PostgreSQL 集成测试按环境跳过）。P0 代码已闭合；P1 问题收集—Agent 草稿—锁定投票—定稿与 Memory 最小提示也已完成自动化，剩余签收是隔离工作台真人主路径；最终有效性评测按本轮要求暂不执行。模型成本按 `17-ai-interview-p0-plan-and-model-routing.md` 分级：状态、权限、计分与恢复永远用规则，qwen-plus 只用于结构化语义抽取/去重/成果处理，不默认使用更强档。

## 5. P0 功能清单

### 5.1 多同事业务闭环

- 1 个 ACTIVE Episode，3–5 名参会者，3–5 个 required ActionItem；各任务独立推进，领域事件串行提交。
- 会议转写抽取 ActionItem；COORDINATOR 修改/合并/忽略，配置一名主负责人和 0..N 协作者后派发。全部当前 `definition_version` 成员接受才进入 TRACKING；任一人退回重改时整轮 assignment 失效，修改后 version+1 重派。
- 合作保留“单负责人 + 明确协作者”：协作者复用同一 ActionItem、状态记录和 ArtifactVersion 版本链，可发送快捷信号、求助并提交贡献，但不成为共同 owner、不能修改负责人的个人承诺或执行人工验收。负责人先对贡献执行 `INCLUDE/REQUEST_REVISION/PROMOTE`，只有 PROMOTE 才进入会议负责人验收；系统不得为协作另建任务、子任务或第二套交付流程。
- 当前承诺与不可变修订历史；双工期及团队风险可视。
- 业务信号、L1/L2 异常询问、每日触达预算；P0 没有 L3。
- 求助只选择本次参会者，状态可跟踪；不允许未参会者外包。
- 每项任务独立提交 ArtifactVersion、校验、处理、退回和人工验收；不是把会议全部任务一次性汇总处理。
- 每项验收结果生成事实协作报告和预制 Memory 候选。
- 所有 required 任务验收后，系统主动聚合 current_valid_version_id 与 AcceptedTaskResult；FINAL_RELEASE 后发布归档。

### 5.2 最小权限与上下文

- P0 使用签名虚拟会话；不再用 localStorage 姓名或请求正文 actor_id 作为授权事实。
- 所有写动作在服务端校验 membership、角色与 owner；所有查询按角色 projection。
- work_requirements 对执行人可见；management_review_policy 仅 COORDINATOR/验收 purpose 可见。
- Prompt/Skill 调用有 purpose、字段 allowlist 和 source manifest；模型不能变更关键状态。

### 5.3 可靠性

- PostgreSQL 领域表为当前业务事实唯一权威源；AuditEvent append-only。
- 领域变更、AuditEvent 与对应 OutboxEntry 同事务提交。
- 单 Dispatcher、mock IM 幂等接收、稳定 effect_id。
- 强制恢复场景：IM 已接受消息但本地尚未记录 DELIVERED 时崩溃，恢复后不得重复外发。
- 不使用 LangGraph、第二套工作流状态或完整 Event Sourcing。

### 5.4 分层验收

- GATE：完整闭环、零重复外发、零旧版/缺 lineage、零权限/发布/预算绕过、关键恢复成功。
- FLOW：任务漏斗、按个人承诺和团队时间交付、关键阶段耗时。
- EFFECT：有效/过期信号、异常询问、询问后进展、求助响应、每人触达。
- NODE：身份拒绝、抽取、版本/处理/验收、Memory、Approval、Outbox。
- SIGNAL 不强设优秀线；字段缺失或非法 N/A 才使报告无效。

## 6. P1：本轮工程范围

- 只保留“多人收集问题 → Agent 整理带来源候选 → 指定成员 1–5 分投票 → 一名负责人定稿”一个复合场景，不实现三套通用模板。
- 已实现上游验收 Gate、依赖等待、完成计数、accepted version 上下文、qwen-plus/规则草稿、人工确认开票、投票锁定、确定性排序、最终提交 Gate 和上游换版失效。
- 已实现版本化预制 Memory 词表、本人确认/替换/拒绝、Agent Context Builder 与当前任务协作者最小提示；无自由文本和展示开关。
- P1 明确不做 LINE_MANAGER/L3、消息合并、安静时段、跨 Episode Memory 或缓存表；这些不影响当前闭环。
- **已从 P1 砍掉**：现实时间催办 Scheduler（口径改为"可复现的主动协调策略"，见 18 号文档第 5 节）；成果处理与 P1 整理两项需人工标注的效果评测（保留确定性 A 类评测与抽取对照）。
- **Memory 升格为 P1 重点**：飞书接入后 Memory 会跨会议聚合并对同事可见，性质从"系统适配本人"变成"团队共享的协作说明书"，因此词表设计、授权边界与产品形态必须在接飞书之前定稿。
- **新建会议/上传逐字稿页不单独做**：归入飞书能力。理由是本项目服务启动时绑定单个 `episode_id`，做一个"页面内新建会议并切换"等于提前实现多 Episode 运行时；而飞书群成员本来就是参会名单的权威来源、妙记本来就有逐字稿，从飞书创建 Episode 比做上传页更自然，也顺带解决"名单必须显式提供"这条硬约束。
- 本轮剩余 P1：投票人可见入口（已完成）、Memory 方案落地（已完成）、DOCX/XLSX/PPTX Adapter（已完成）、确定性产品评测（已完成）、抽取对照评测（工具已完成，待标注数据）。

## 7. P2：平台与多任务扩展

- 飞书 PrincipalProvider、妙记逐字稿、群成员名单、群聊/卡片回调、平台 membership 校验。
- 多个 Episode 并发；多任务 TaskPack/AgentAdapter/EnvironmentAdapter/Scorer。
- 组织目录与更完整 RBAC。
- 完整依赖图、跨 Episode Memory 冲突/衰减/撤回、多轮完整重算、产品级评测趋势。

## 8. 当前明确不做

- 学术假设、消融、显著性、pass^k 或研究型大样本测试床。
- 多 Agent 编排框架、LangGraph、通用 Skill Router、插件市场。
- 通过页面在线/刷新/已读监控员工。
- 用 Prompt/temperature 模拟权限。
- **网页抓取 Adapter**：不抓取、不验证链接真实性；链接只保留 URL 与 inspection status。
- **未参会成员外包**：任务参与权只来自显式 EpisodeParticipant，不把未参会者自动加入。
- 评价性人格、能力、可靠性标签。

## 9. 模块优先级映射

| 模块 | P0 | P1/P2 |
|---|---|---|
| N01/N02 | 抽取、复核、版本化派发/接受/退回、承诺与双工期 | 问题收集—投票—定稿的同 Episode 依赖与参与推进 |
| N03 | mock IM、签名虚拟身份、收发幂等 | 飞书 Adapter |
| N04 | 信号、L1/L2、预算、参会者求助 | L3/经理、消息合并 |
| N05 | FINAL_RELEASE | L3 审批 |
| N06 | 单任务接收、PDF/text、辅助包、人工验收、结果冻结 | 更多格式/质量规则 |
| N07/N08 | 已验收版本聚合、lineage、发布归档 | 多终稿模板 |
| N09 | 同事务、effect_id、关键恢复 | 多 Dispatcher/更多故障 |
| N10 | Principal、purpose、字段 allowlist、分层上下文 | 飞书身份、跨任务装配 |
| N11/N12 | 核心 fixture、5 GATE＋轻量 SIGNAL | 固定回归集/多任务评测 |
| N13 | 三角色表面、团队工期、提交/验收/Memory | 飞书卡片、更细视觉 |
| N14 | VirtualClock、到期和信号过期的可复现实验 | 真实日历不排期 |
| N15 | 单任务协作报告、预制 Memory 候选与本人维护 | 当前任务最小提示已完成；跨 Episode 汇总/衰减进入 P2 |

## 10. P0 验收主场景

载入一份含 4 个 ActionItem 的会议转写 → COORDINATOR 复核并设团队时间/主负责人/协作者 → 成员回应派发，其中一轮退回重改后重派并全部接受 → 事件信号推进，其中一人进入检查窗口后无信号并收到快捷询问，另一人向参会者求助 → 各人独立提交 PDF/text/正文 → 协作贡献由主负责人处理 → 校验、辅助处理、退回与重交 → COORDINATOR 逐项验收并冻结 AcceptedTaskResult → 自动生成协作报告与 Memory 候选 → 注入一次 Outbox crash/restart → 聚合终稿 → FINAL_RELEASE → 归档并输出 GATE/SIGNAL。

通过条件：

1. 4 个 required ActionItem 和 Episode 全部归档；
2. 未参会求助、越权提交、PARTICIPANT 读取管理规则均被拒绝；
3. 重复外发数为 0；
4. 旧版本混入和缺 lineage 均为 0；
5. 个人/团队日期、最后信号、询问、求助、处理结果和 Memory 来源均可追溯；
6. 重启后主链连续，不依赖进程内状态或第二套工作流。

## 11. Fixture 数据策略

- P0 可从 AliMeeting4MUG/AMC-A 选择少量中文行动项句并补最小 owner/deliverable/deadline/source_span 标注。
- 外部数据访问、许可或格式处理超过 1 小时即切换项目自建 fixture，不建设训练集或数据清洗管线。
- 用户提供的真实会议逐字稿用于产品回归；必须绑定参会名单和最小权限 fixture，不能当作全组织数据源。
- 真实数据不做大规模训练集：准备 3–5 份即可，1–2 份开发回归、1 份附最小人工预期、至少 1 份保留为未参与 Prompt/规则调整的最终盲测。
- 每份真实记录只要求会议日期、会议负责人和明确参会名单；人工预期只标应有任务、团队时间、交付要求和验收点，不要求逐句或全文标注。
- 最终盲测另准备一份正文/PDF/text 任务成果；可附一版故意不完整的成果，用于验证退回、重交和版本替换。
