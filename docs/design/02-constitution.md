# 多同事会议行动项协作 Agent｜Constitution

版本：1.3  
冻结级别：最高规范层
变更依据：ADR-024、ADR-026、ADR-030

## CON-001｜目标优先级

- 类型：优先级约束
- 约束：发生范围、工期或实现冲突时，系统 MUST 按“多同事业务闭环 ＞ 高风险副作用与数据正确性 ＞ 产品级可重算评测 ＞ UI 完整度 ＞ 可选扩展”排序。
- 验证：REVIEW-CON-001；所有 ADR 必须声明是否改变该优先级。

## CON-002｜外部副作用唯一通道

- 类型：不变量
- 约束：Agent 产生的外部可见动作 MUST 经 Outbox 提交，任何模块 MUST NOT 直接调用 IM Adapter 发送业务消息。
- 适用范围：N01、N02、N04、N05、N06、N07、N08。
- 验证：AUTO-CON-002；静态依赖检查与集成测试验证仅 Dispatcher 持有发送权限。

## CON-003｜事务原子性

- 类型：不变量
- 约束：领域状态变更、对应 AuditEvent 与 OutboxEntry MUST 在同一数据库事务中提交或回滚。
- 适用范围：所有产生外部动作的领域决策。
- 验证：AUTO-CON-003；在事务内三个故障点注入异常，数据库不得出现部分提交。

## CON-004｜审计事件不可变

- 类型：不变量
- 约束：已提交的 AuditEvent 记录 MUST NOT 被更新或删除。AuditEvent 用于审计与指标重算，不作为领域状态重放源。
- 适用范围：全系统。
- 验证：AUTO-CON-004；数据库权限与测试均拒绝 UPDATE/DELETE 已提交事件。

## CON-005｜领域状态权威

- 类型：不变量
- 约束：正常运行时，领域表 MUST 是当前规范状态的唯一读源。
- 适用范围：Agent、Scheduler、UI、Policy。
- 验证：AUTO-CON-005；业务查询不得从 AuditEvent、LLM 上下文或进程内状态反推当前领域状态。

## CON-006｜审计分歧处理

- 类型：不变量
- 约束：领域表与 AuditEvent 不一致时，系统 MUST 以领域表作为当前业务事实并将审计一致性检查标记为失败；MUST NOT 通过 AuditEvent 自动覆盖领域表。
- 适用范围：Harness、恢复工具。
- 验证：AUTO-CON-006；缺失或矛盾的 AuditEvent 可被识别，Episode 进入 BLOCKED 或报告失败。

## CON-007｜恢复位置可推导

- 类型：恢复约束
- 约束：系统 MUST NOT 依赖第二套流程位置记录；进程重启后 MUST 从领域表、Outbox 与当前 VirtualClock 推导下一可执行步骤。
- 验证：AUTO-CON-007；清空进程内状态后可继续运行，业务事实与分层指标可重算结果不变。

## CON-008｜统一时间源

- 类型：不变量
- 约束：所有领域逻辑 MUST 从 RunContext 获取 VirtualClock；领域模块 MUST NOT 读取系统墙上时钟。
- 适用范围：N01–N15。
- 验证：AUTO-CON-008；测试环境替换系统时间不影响领域事件序列。

## CON-009｜统一随机源

- 类型：不变量
- 约束：所有影响领域行为或评测结果的随机选择 MUST 使用 RunContext 提供的种子化随机源。
- 验证：AUTO-CON-009；同内容包、同 seed、同缓存快照产生相同语义事件序列。

## CON-010｜业务与评测隔离

- 类型：访问边界
- 约束：Harness MAY 读取领域表和 mock IM 计算指标，但 MUST NOT 向 Agent 写入策略决定或伪造业务完成状态。
- 验证：AUTO-CON-010；Harness 权限与依赖测试。

## CON-011｜模拟器隔离

- 类型：访问边界
- 约束：模拟器 MUST NOT 读取 Agent 内部状态、领域表或策略参数；模拟器输入仅限寻址给该 actor 的 IM 消息、VirtualClock 与自身脚本状态。
- 验证：AUTO-CON-011；依赖检查与黑盒测试验证模拟器无领域仓储访问。

## CON-012｜关键状态外置

- 类型：不变量
- 约束：ActionItem、CommitmentRevision、ArtifactVersion、Approval 与 Intervention 的规范状态 MUST 持久化，MUST NOT 仅存在于 LLM 上下文。
- 验证：AUTO-CON-012；清空 LLM 上下文后系统仍能继续正确推进。

## CON-013｜策略确定性

- 类型：不变量
- 约束：是否触达、触达等级、预算处理与是否申请审批 MUST 由确定性决策表产生，LLM MUST NOT 参与这些决策。
- 验证：AUTO-CON-013；固定领域输入时更换 LLM 输出不得改变策略决策。

## CON-014｜高代价动作审批

- 类型：不变量
- 约束：P0 的终稿外发 MUST 在 FINAL_RELEASE Approval 为 APPROVED 后方可产生对应 OutboxEntry。若后续启用 L3，L3 同样 MUST 经显式 Approval，且不能以模型判断代替。
- 验证：AUTO-CON-014；终稿未审批、驳回与审批挂起状态下 mock IM 不得收到发布动作。

## CON-015｜版本不可变与 lineage

- 类型：不变量
- 约束：ArtifactVersion 创建后 MUST 不可变，终稿每个字段 MUST 记录 `(action_item_id, version_id, field_path)` lineage。
- 验证：AUTO-CON-015；版本更新必须创建新版本，终稿缺少 lineage 时聚合失败。

## CON-016｜指标可重算

- 类型：不变量
- 约束：P0 的 GATE 与 SIGNAL MUST 能仅依据领域表、AuditEvent、OutboxEntry、mock IM 投递记录与冻结配置重新计算。
- 验证：AUTO-CON-016；删除已有报告后重新运行计算得到相同数值。

## CON-016A｜门槛与信号分离

- 类型：评测约束
- 约束：只有安全、结果正确性与恢复类 GATE MAY 因数值阻塞验收；流程、效果和单点 SIGNAL MUST 报告实际值或合法 N/A 原因，MUST NOT 擅自增加硬通过线。
- 验证：AUTO-CON-016A；SIGNAL 偏低不改变 eval 退出码，GATE 失败或 SIGNAL 字段缺失必须返回非零退出码。

## CON-017｜产品验收与学术假设隔离

- 类型：禁止项
- 约束：学术假设、消融、敏感性、帕累托或 pass^k 结论 MUST NOT 成为当前项目的通过条件。
- 验证：REVIEW-CON-017；P0/P1 契约不得引用 H1、H2、H3 或统计结论作为断言依据。

## CON-018｜单 Episode 并发边界

- 类型：范围约束
- 约束：v1 MUST 只运行一个活动 Episode；该 Episode 内 ActionItem MAY 在模拟时间上并行，但领域事件 MUST 由单一事件循环串行提交。
- 验证：AUTO-CON-018；第二个 Episode 激活请求被拒绝，单 Episode 多 ActionItem 可独立推进。

## CON-019｜平台中立

- 类型：架构约束
- 约束：Agent Core MUST 仅依赖标准 IM Adapter 契约，MUST NOT 引用企微、钉钉或飞书专有字段。
- 验证：AUTO-CON-019；核心模块 schema 检查不含平台专有字段。

## CON-020｜规范变更治理

- 类型：治理约束
- 约束：constitution、已冻结系统级 SDD、指标契约与 SHOULD 偏离的修改 MUST 通过 ADR。
- 验证：REVIEW-CON-020；变更记录必须引用 ADR 编号。

## CON-021｜可信身份与服务端授权

- 类型：访问边界
- 约束：业务服务 MUST 从受信 PrincipalProvider 获得 actor 身份，MUST NOT 信任请求正文中的 actor_id；所有写动作和受限读取 MUST 在服务端校验 Episode membership、角色、资源归属和字段可见性。
- 验证：AUTO-CON-021；篡改 actor_id、跨 Episode 访问、非执行人提交和 PARTICIPANT 读取管理侧字段均被拒绝。

## CON-022｜模型最小上下文

- 类型：访问边界
- 约束：模型或 Skill/Adapter 只能按声明的 purpose 接收 allowlist 上下文；Prompt 与 temperature MUST NOT 承担认证、授权、状态迁移或人工批准。
- 验证：AUTO-CON-022；上下文 manifest 可重算，越权字段不会进入模型输入，模型输出不能直接改变关键状态。

## CON-023｜人类信号真实性

- 类型：协作约束
- 约束：页面在线、刷新、停留、已读、未提交草稿和系统自身消息 MUST NOT 计为任务执行人的有效业务信号；系统只能从已登记的 allowlist 事件计算 last_owner_signal_at。
- 验证：AUTO-CON-023；无业务动作的刷新不会延后异常询问，系统提醒本身不会被误判为负责人进展。
