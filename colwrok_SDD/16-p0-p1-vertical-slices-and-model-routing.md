# P0/P1 垂直任务闭环与模型路由

状态：SUPERSEDED BY ADR-033/ADR-035；第 4、6 节 Issue Plan 仅为历史拆解，不代表当前待办  
日期：2026-08-06  
来源：ADR-030、ADR-031、ADR-032、`12-mvp-priority-profile.md`、`15-p0-closure-audit-and-agent-routing.md`  
输出模式：draft-only，不创建 GitHub Issues

## 1. Readiness

- 目标用户、P0/P1 边界、主流程、人工决定、权限、失败恢复和验收口径已经明确，达到 implementation issue 拆解条件。
- ADR-032 已消除真实时钟、Web/Worker 隔离和求助通知三个 P0 架构未决项。
- P1 已砍为一个“问题收集—Agent 草稿—投票—定稿”白名单链；L3、三个通用模板与跨 Episode Memory 不进入当前 P1。
- 每个任务按可演示的端到端闭环拆分，不建立“前端/后端/测试/文档”横向任务。

## 2. 模型等级

| 路由等级 | 具体模型/推理档 | 适用任务 | 禁止承担 |
|---|---|---|---|
| L0 | Luna 轻量档；当前环境不可直接调用，回退为 `gpt-5.6-terra/low` | 局部、确定、已有 oracle 的垂直小改动 | 权限、并发、时间、恢复、外发副作用和新领域语义 |
| T1 | `gpt-5.6-terra/low` | 小型 API/UI 校验、稳定错误映射、机械回归 | 跨状态机设计和故障恢复裁决 |
| T2 | `gpt-5.6-terra/medium` | 已冻结契约下的常规数据/API/UI 闭环 | 多进程时序、重复外发、权限模型首轮设计 |
| T3 | `gpt-5.6-terra/high` | 跨层状态转换、版本 lineage、Adapter/Outbox 接线 | 关键并发/恢复方案未经压力测试的最终签收 |
| T4 | `gpt-5.6-terra/xhigh` | 时钟/并发/进程恢复、幂等副作用、Live E2E 与复杂失效传播 | 代替人工产品、权限或发布决定 |

模型等级表示实现任务的最低安全路由。最终 P0/P1 Gate 始终由确定性测试和人工业务验收裁决，不由模型自评通过。

## 3. P0 分层与闭环状态

| 层 | 任务闭环 | 当前状态 | 剩余工作 | 模型路由 |
|---|---|---|---|---|
| P0-L0 身份/上下文 | 会话凭证 → Principal → membership/role/owner 校验 → 字段 projection → 审计 | 已闭合 | 只做回归；任何权限语义变更重新走 T3 | 回归 L0；权限变更 T3 |
| P0-L1 会议到责任 | 转写抽取 → 负责人复核/版本化派发 → 逐人回应 → 双工期/冲突可视 | 已闭合 | 只做真实数据回归 | L0/T1 |
| P0-L2 主动推进 | 业务信号/沉默 → SystemClock/Scheduler → L1/L2 决策 → Outbox → IM → 回应更新信号 | 未闭合 | 真实 ClockSource、Worker 调度和可靠投递 | T3/T4 |
| P0-L3 求助 | 执行人选择参会者 → 请求通知 → 对方确认/解决 → 发起人获知 → 恢复推进 | 未闭合 | 生命周期 Outbox 通知及去重 | T3，契约稳定后边界测试 L0 |
| P0-L4 单任务交付 | 正文/链接/附件 → 校验/抽取 → 异步成果处理 → 负责人退回或验收 | 语义闭合，运行部分未闭合 | 慢模型与 HTTP 隔离；服务端附件上限 | Worker T3；上限 L0 |
| P0-L5 结果/Memory/终稿 | AcceptedTaskResult → 报告/私有 Memory → required 全部完成 → 自动终稿 → FINAL_RELEASE → 归档 | 领域闭合 | 迁入独立 Worker 后做整链回归 | T2/T3 |
| P0-L6 可靠性 | 外发计划 → 原子领取 → 失败重试/死信 → 重启回收 → EffectId 去重 | 未闭合 | 通用 Dispatcher 故障路径和启动恢复 | T4 |
| P0-L7 真实验收 | 启动 Web/Worker → 只用 HTTP 操作 → 注入慢模型/IM 故障/重启 → 归档与 Gate | 未闭合 | Live E2E 和用户真实会议验收 | T4 + HITL |

## 4. P0 Issue Plan

### P0-01｜慢模型不阻塞工作台，待处理结果由独立 Worker 完成

- Type：AFK
- Priority / model：P0-A；最低 T3，推荐 `gpt-5.6-terra/high`
- Source：ADR-032 决策 2/3、N09-008、SYS-ARCH-002
- 闭环：提交 ArtifactVersion → Web 立即返回 → Worker 原子领取 → 百炼/本地处理 → READY/RETRY_WAIT/FAILED → 页面查询到结果。
- Acceptance criteria：
  - [ ] Web/API 与 Worker 使用隔离运行循环和数据库连接，启动/停止可定位。
  - [ ] 注入 60 秒慢处理时 `/api/state` 和普通写请求仍响应。
  - [ ] Worker 重启后继续同一 version_id，不生成第二份处理结果。
  - [ ] 不引入 Redis、Celery、Workflow 或 WorkerJob 业务实体。
- Verification：新增 Worker 单元/集成测试及慢处理 runtime smoke。
- Blocked by：无。

### P0-02｜通用 Outbox 在失败和重启后可靠完成或明确死信

- Type：AFK
- Priority / model：P0-A；最低 T3，推荐 `gpt-5.6-terra/xhigh`
- Source：ADR-032 决策 5、N09-003 至 N09-006
- 闭环：Outbox PENDING → CLAIMED → IM 接受/失败 → DELIVERED、RETRY_WAIT 或 DEAD_LETTER → 重启回收 → 原 EffectId 重投/停止。
- Acceptance criteria：
  - [ ] 接受前可重试错误进入 RETRY_WAIT，终态错误或耗尽进入 DEAD_LETTER。
  - [ ] 接受后响应丢失并重启时，mock IM 只保留一个 external_message_id。
  - [ ] 新 Worker 启动主动回收旧 session 的 CLAIMED entry。
  - [ ] Approval/Episode 的依赖失败状态可查询，且所有转移有 AuditEvent。
- Verification：故障注入矩阵＋PostgreSQL 并发/重启集成测试。
- Blocked by：无；与 P0-01 可并行开发，合并后联调。

### P0-03｜真实业务时间触发一次正确的主动询问

- Type：AFK
- Priority / model：P0-A；最低 T3，推荐 `gpt-5.6-terra/high`
- Source：ADR-032 决策 1、N04、N14、API-CLK-002
- 闭环：SystemClock 同步 → Scheduler 推导检查窗口/沉默 → L1/L2 Intervention/Outbox → Dispatcher 送达 → 快捷回应更新有效信号并抑制重复询问。
- Acceptance criteria：
  - [ ] LIVE 时间不回退，EVALUATION 仍完全使用 VirtualClock 并保持原测试可复现。
  - [ ] 尚未进入窗口、有效信号未过期、待验收或存在未解决求助时不催办。
  - [ ] 满足条件时自动送达一次；刷新、重复 Tick 和 Worker 重启不重复发送。
  - [ ] 同刻到达的有效用户输入优先于 Scheduler 催办。
- Verification：受控 SystemClock 集成测试＋真实 Worker runtime smoke。
- Blocked by：P0-01、P0-02。

### P0-04｜求助状态变化主动通知正确当事人

- Type：AFK
- Priority / model：P0-A；最低 T2，推荐 `gpt-5.6-terra/high`
- Source：ADR-032 决策 4、N04 求助路由、API-HELP-001
- 闭环：owner 选择参会者并确认 → target 收到请求 → target ACK/RESOLVE 或 requester CANCEL → 另一方收到状态 → 未解决抑制泛化询问，解决后恢复正常策略。
- Acceptance criteria：
  - [ ] 创建、确认、解决、取消分别使用稳定 EffectId 和正确收件人。
  - [ ] 未参会者、自助、非 owner 发起和非当事人响应均被拒绝且不创建 Outbox。
  - [ ] 重复 API、重试和 Worker 重启不重复通知。
  - [ ] 团队视图可见求助状态，但不泄露不必要的正文或私聊字段。
- Verification：服务契约、权限、Outbox、mock IM 和工作台集成测试。
- Blocked by：P0-01、P0-02。

### P0-05｜超限附件在解码前被拒绝并向用户解释

- Type：AFK
- Priority / model：P0-B；最低 L0，推荐 Luna；不可用时 `gpt-5.6-terra/low`
- Source：P0 闭环审计 P0-F、SYS-ERR-001、API-DELIVERY-001
- 闭环：用户选择/调用上传 → 客户端提示或服务端检查 Content-Length/声明大小 → 超限返回 413/VALIDATION → 表单内容保留 → 数据库和模型调用均不发生。
- Acceptance criteria：
  - [ ] 浏览器和服务端统一以 5MB 原始附件总量为边界，并考虑 base64 请求开销。
  - [ ] 绕过前端的超限请求在附件解码和百炼调用前失败。
  - [ ] 返回稳定错误码和可读提示，领域状态保持不变。
- Verification：HTTP 边界测试、无模型调用断言、人工表单草稿检查。
- Blocked by：无。

### P0-06｜只通过部署入口完成一次可恢复的完整任务闭环

- Type：AFK；结果交由下一票 HITL 签收
- Priority / model：P0-A；最低 T3，推荐 `gpt-5.6-terra/xhigh`
- Source：TST-LIVE-001、P0-L0 至 P0-L7
- 闭环：会议导入 → 复核/派发/逐人回应 → 真实时间询问/求助 → 提交/慢模型处理 → 退回/重交/验收 → Memory/终稿 → Worker/IM 故障恢复 → FINAL_RELEASE/归档。
- Acceptance criteria：
  - [ ] 测试只使用 HTTP、PostgreSQL 观察和进程启停，不调用 service 推进/评估/投递辅助方法。
  - [ ] 慢模型、IM 接受前失败、接受后响应丢失和 Worker 重启均被覆盖。
  - [ ] 5 GATE 通过；重复外发、旧版本、缺 lineage、权限/预算绕过均为 0。
  - [ ] 测试失败能定位到业务步骤、Outbox 状态和 AuditEvent，而不是只返回 `Failed to fetch`。
- Verification：PostgreSQL Live E2E 报告和固定 runtime smoke 命令。
- Blocked by：P0-01 至 P0-05。

### P0-07｜用真实会议完成最终业务验收

- Type：HITL
- Priority / model：P0 sign-off；实现辅助 `gpt-5.6-terra/medium`，最终决定由用户完成
- Source：12-mvp-priority-profile 第 10/11 节、TST-LIVE-001
- 闭环：用户以会议负责人和参会者分别操作真实逐字稿任务 → 检查提醒/求助/交付/退回/重交/终稿 → 给出通过或具体缺陷。
- Acceptance criteria：
  - [ ] 至少一项任务经历求助，一项经历退回重交，一项经历终稿驳回或确认。
  - [ ] 用户能回答谁负责、两类日期、最后信号、求助对象、验收版本、失败位置和终稿来源。
  - [ ] 未发现高频刷新覆盖草稿、越权或无法恢复的卡死状态。
- Verification：用户操作记录＋系统审计/验收报告。
- Blocked by：P0-06。

## 5. P1 分层与任务闭环

| 层 | 任务闭环 | 模型路由 |
|---|---|---|
| P1-L0 结构确认 | 组织人原文 → COORDINATOR 确认上游/最终负责人/投票人/保留数 → 最小依赖和参与输入 | 规则/权限 T3；实现后机械回归 L0 |
| P1-L1 Agent 候选草稿 | 已验收上游结果 → 规则抽取或 qwen-plus 语义去重 → 来源校验 → 最终负责人确认 | qwen-plus 仅语义；契约/lineage 为规则 |
| P1-L2 锁定投票/定稿 | 发布 ballot → 每人一次 1–5 分 → 确定性计分 → 最终负责人提交/人工验收 | 状态/计分不调用模型；回归 L0 |
| P1-L3 上游换版 | accepted version 变化 → ballot/vote/下游有效结果失效 → 重新生成 | T3/T4；实现后回归 L0 |
| P1-L4 Memory | 事实报告 → 预制词条候选 → 本人确认/替换/拒绝 → 当前协作者最小提示 | 规则；Context 装配 T1/T2 |
| P1-L5 真人签收/最终评测 | 隔离工作台主路径 → 用户签收；最后再做效果评测 | HITL；本轮不执行最终评测 |

## 6. P1 Issue Plan

> 以下 P1-01 至 P1-07 是 2026-08-06 的历史 Issue 草案；当前采用 ADR-031 修订后的单一白名单链，不按“三模板/顺序交接/L3”继续实施。

### P1-01｜负责人把会议协作描述确认并展开成基础任务（历史）

- Type：AFK；首次真实样例 UI 评审为 HITL
- Priority / model：P1-0；最低 T3，推荐 `gpt-5.6-terra/high`
- 闭环：会议组织人原文 → SYSTEM 提出白名单模板 → COORDINATOR 修正/确认 → 基础 ActionItem、Dependency/Contribution 关系原子生成 → 参与者看到自己的任务。
- Acceptance criteria：仅接受三个白名单模板；成员必须来自 Episode；缺来源/最终负责人/成环时拒绝；不创建多 owner、Workflow 或 Stage。
- Verification：每种模板一份 fixture＋越权/无来源/环路反例。
- Blocked by：P0-07。

### P1-02｜多人并行收集完成后自动解锁汇总任务（历史）

- Type：AFK
- Priority / model：P1-1；最低 T3，推荐 `gpt-5.6-terra/high`
- 闭环：个人基础任务派发/回应/交付/验收 → ALL_REQUIRED Gate 完成计数 → 汇总 owner 收到解锁通知 → 只读已验收版本 → 汇总任务验收。
- Acceptance criteria：缺一项不解锁；等待 owner 不被错误催办；上游正文权限不因关系扩大；汇总 lineage 完整。
- Verification：3 人收集＋1 人汇总 Live fixture。
- Blocked by：P1-01。

### P1-03｜顺序交接在上游换版后使旧下游结果失效（历史）

- Type：AFK
- Priority / model：P1-2；最低 T3，推荐 `gpt-5.6-terra/xhigh`
- 闭环：上游 accepted_version 解锁下游 → 下游处理/验收 → 上游新版本被验收 → 旧下游 lineage 失效 → 下游明确重开 → 新链再次验收。
- Acceptance criteria：未验收上游不可进入下游上下文；旧下游不静默保留有效；通知对象和失效范围准确；无级联循环。
- Verification：版本替换、重启和重复事件测试。
- Blocked by：P1-01、P1-02 的 Gate/lineage 基础。

### P1-04｜多人提交问题、投票后由最终负责人保留 7–8 个问题（已由当前实现取代）

- Type：AFK；计分/并列规则首次冻结为 HITL
- Priority / model：P1-3；最低 T3，推荐 `gpt-5.6-terra/xhigh`
- 闭环：每人独立问题清单验收 → 汇总候选去重并冻结版本 → 每名参会者一次结构化投票 → 确定性计分/并列展示 → 最终负责人选择 7–8 项并提交正式成果 → 人工验收。
- Acceptance criteria：投票不产生共同 owner；一人一次且可审计；计分规则可复算；最终选择仍由负责人确认；候选/投票/终稿版本 lineage 完整。
- Verification：真实会议片段 fixture、并列/缺席/重复投票反例、最终人工 UI 检查。
- Blocked by：P1-01、P1-02；开始前 HITL 冻结计分和并列规则。

### P1-05｜所有参会者看懂协作结构、等待对象和完成计数

- Type：AFK
- Priority / model：P1-4；最低 L0，推荐 Luna；不可用时 `gpt-5.6-terra/low`
- 闭环：查询已确认结构 → 服务端角色 projection → UI 展示模板、成员、阶段、等待对象、完成计数和过期提示 → 用户跳转自己的基础任务。
- Acceptance criteria：不显示共同 owner；普通参会者不见他人正文/管理规则；等待和失效原因可读；刷新不清空草稿。
- Verification：三模板截图/DOM 检查＋权限测试。
- Blocked by：P1-02 至 P1-04 的查询契约冻结。

### P1-06｜同一负责人多任务提醒合并且遵守安静时段

- Type：AFK
- Priority / model：P1-5；最低 T2，推荐 `gpt-5.6-terra/medium`
- 闭环：多个到期 Intervention → 确定性合并窗口/预算 → 一条含任务清单的 Outbox → 回应分派回各任务信号 → 审计可解释。
- Acceptance criteria：不改变每任务 Intervention 依据；合并不绕过预算；安静时段后只补发仍有效项；EffectId 可复算。
- Verification：跨任务、跨日、部分已回应 fixture。
- Blocked by：P0-03、P0-02。

### P1-07｜失败详情与归档导出让负责人能自行定位问题

- Type：AFK
- Priority / model：P1-5；最低 L0，推荐 Luna；复杂导出回退 `gpt-5.6-terra/low`
- 闭环：处理/Outbox/终稿失败 → 负责人查看阶段、错误码、尝试次数和证据引用 → 触发允许的重试或下载归档包 → 导出内容遵循权限裁剪。
- Acceptance criteria：不暴露附件二进制/私有 Memory；可区分可重试/终态；导出包含版本 lineage 和 AuditEvent 摘要；普通参会者受限。
- Verification：失败状态 UI、下载内容和权限快照测试。
- Blocked by：P0-02、P0-06。

### P1-08｜增加文档格式而不改变成果处理契约

- Type：AFK
- Priority / model：P1-5；最低 T2，推荐 `gpt-5.6-terra/medium`
- 闭环：上传新白名单格式 → Adapter 提取文本/元数据 → 复用 source manifest 和单任务处理 → 失败返回稳定错误 → 人工验收。
- Acceptance criteria：二进制不进入模型；大小/格式限制服务端执行；版本绑定和字段分层不变；无专有格式分支进入领域层。
- Verification：每种格式成功/损坏/超限 fixture。
- Blocked by：P0-05、P0-06。

### P1-09｜直属上级 L3 审批只在组织身份边界明确后启用

- Type：HITL
- Priority / model：P1-later；实现最低 T3，推荐 `gpt-5.6-terra/xhigh`
- 闭环：组织 Adapter 提供 manager relation → policy 形成 L3 候选 → 人工批准 → Outbox 通知 → 可审计完成/拒绝。
- Open questions：真实组织身份来源、谁可见原因、审批超时和收件人边界。
- Blocked by：飞书/组织身份决策；未解决前不得实现。

### P1-10｜跨 Episode Memory 在本人确认后最小共享

- Type：HITL
- Priority / model：P1-later；实现最低 T3，推荐 `gpt-5.6-terra/xhigh`
- 闭环：本人已确认 Memory → 冲突/衰减 → 本人选择共享范围 → 新协作上下文按 purpose 最小读取 → 本人撤回后停止使用并审计。
- Open questions：保留期限、共享对象、撤回影响、敏感标签规则和飞书身份映射。
- Blocked by：隐私/组织范围决策；未解决前不得实现。

## 7. 覆盖矩阵

| SDD 项 | Covered by | 状态 |
|---|---|---|
| ADR-032 Web/Worker 隔离 | P0-01、P0-06 | P0 待实现 |
| N09 通用 Outbox/恢复 | P0-02、P0-06 | P0 待实现 |
| N14 LIVE ClockSource/追交付 | P0-03、P0-06 | P0 待实现 |
| N04/API-HELP 主动求助通知 | P0-04、P0-06 | P0 待实现 |
| 服务端附件边界 | P0-05、P0-06 | P0 待实现 |
| TST-LIVE-001/最终验收 | P0-06、P0-07 | P0 待实现/HITL |
| ADR-031 结构识别与展开 | P1-01 | P1 |
| 多人并行收集 | P1-02 | P1 |
| 顺序交接/版本失效 | P1-03 | P1 |
| 多人参与决策 | P1-04 | P1/HITL 规则 |
| N13 协作结构 UI | P1-05 | P1 |
| 消息合并/安静时段 | P1-06 | P1 |
| 失败详情/导出/更多格式 | P1-07、P1-08 | P1 |
| L3、跨 Episode Memory | P1-09、P1-10 | P1-later/HITL |

## 8. 执行边界

- 当前只执行 P0-01 至 P0-07；P1 票保持 draft，不进入代码。
- Luna 只领取明确标为 L0 的完整小闭环；不能把 T3/T4 任务拆碎后伪装成多个 Luna 任务，以免丢失跨层不变量。
- Terra 任务完成后可让 Luna 补机械回归，但原 Terra 任务仍由同等级模型复核关键状态、并发和恢复证据。
- P0-07 完成人工签收后，才按 P1-01 → P1-02 → P1-03 → P1-04 的顺序推进；P1-05 可在对应查询契约冻结后并行。
