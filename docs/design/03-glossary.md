# 多同事会议行动项协作 Agent｜术语表

版本：1.4  
变更依据：ADR-024、ADR-026、ADR-030、ADR-034

## 业务对象

### Organization
任务及成员所属的组织边界。P0 仅运行一个 Organization，但所有业务数据均携带 organization_id，为后续多组织隔离预留边界。

### Actor
参与协作的人，类型为 `USER | SIMULATED_USER`。P0 在 Episode 中只承担 `COORDINATOR | PARTICIPANT`；SYSTEM 是服务 principal，不是被模拟的人类 Actor。现有 `AGGREGATOR/ACTION_OWNER` 仅是实现兼容别名，`MANAGER` 不进入 P0。

### Episode
一次完整的会后多任务协作；输入是一份会议转写和参会名单，输出是经会议负责人审批的结构化终稿、单任务结果与完整审计记录。P0 同时最多一个 ACTIVE Episode。

### ActionItem
从会议转写中提取的工作项，包含唯一主负责人、可选协作者、交付定义、团队需要时间与 SLA。提取后先进入 `PENDING_CONFIRMATION`；COORDINATOR 派发当前定义版本且全部被派发成员接受后进入 `TRACKING`。`owner_actor_id` 指主执行人，不是 COORDINATOR。

### 候选阶段
ActionItem 的待确认阶段，不是单独实体。此阶段不参与催办、完成度判定或交付校验；被拒绝后状态变为 `REJECTED`。

### CommitmentRevision
协作执行人对 ActionItem 的结构化承诺版本，至少包含执行人、交付内容与个人承诺时间 `promised_by`。修订不可变，一个 ActionItem 同时最多一个 ACTIVE 修订。

### TeamRequiredBy
团队何时需要该任务结果，由 COORDINATOR 维护并对本次会议所有参与者可见。它不因执行人修改个人承诺而自动改变。

### ArtifactVersion
行动项的一次不可变交付版本；保存真实提交人、提交时 contributor_role、接收顺序、字段载荷、校验结果、内容哈希和来源消息。负责人和协作者写入同一版本链，P0 不建立 1:1 Artifact 容器或协作专用交付对象。

### Intervention
策略层针对 ActionItem 形成的外部协调动作。P0 仅含 L1 私聊询问与 L2 会议协作询问；L0 是不动作，不建实体；L3 直属上级升级为 P1。

### Approval
高代价动作的人工决策对象。P0 类型仅为 `FINAL_RELEASE`；L3 Approval 是启用组织外升级后的 P1 类型。

### ProgressSignal
与单个 ActionItem 绑定的 contributor 业务事件，包括派发接受/承诺修订、负责人或协作者的快捷状态回应、求助、交付/重交和退回后的明确回应；事件保留真实 actor 与 contributor_role。刷新、停留、已读和草稿不是信号。

### AssistanceRequest
任务执行人向同一 Episode 的参会者发起的求助，拥有目标参与者、原因、状态和解决时间。P0 不允许向未参会者外包。

### AcceptedTaskResult
人工验收通过后、绑定 accepted version 冻结的单任务结果记录，包含内容引用、完成报告、标准化结果、来源清单和处理元数据；它不是第二套任务流程。

### CollaborationMemory
基于已验收任务事实形成、取值来自版本化预制词表、可由本人确认/同 topic 替换/拒绝的协作习惯记录。当前有效协作者只能读取 CONFIRMED 的最小提示，不读取证据和历史；Memory 不得作为权限、验收或惩罚性升级依据。

### SLA
内容包定义的确认超时、提醒提前量、升级等待时间与最终截止时间等规则。

### EffectId
一次外部副作用的稳定业务标识。首次决策、重试和故障恢复必须复用同一 EffectId；只有新的业务决策才能生成新的 EffectId。

### OutboxEntry
待执行外部副作用的持久记录，与业务状态在同一事务写入；Dispatcher 按 EffectId 幂等投递。

### AuditEvent
append-only 审计记录，用于解释、排障和产品指标。它不承担当前状态查询，也不用于事件回放重建业务状态。

### FinalDeliverable
汇总人可审批的结构化终稿。修改后创建新修订，旧修订保留，不覆盖。

### RunContext
一次运行的不可变上下文，至少包含 `run_id`、`content_pack_version`、`clock` 与配置版本。

### VirtualClock
业务规则唯一时间源，按离散事件推进，不等同于系统墙上时钟。

### Tick
VirtualClock 的单调递增序号；同一 Tick 内按事件循环写入顺序处理。

### ContentPack
可替换的业务场景配置，定义会议转写、参与者、交付 schema、行为脚本、SLA 与策略参数。

### GATE
有明确通过线的验收门槛，只用于完整闭环、安全护栏、结果正确性与关键恢复；任一失败都会阻塞 P0。

### SIGNAL
用于解释流程、协调效果或单点健康度的计数、比例或耗时。SIGNAL 默认不设通过线，也不替代业务判断。

### IM Message
由 mock IM 或真实适配器持有的标准消息，包含 message_id、conversation_id、sender、recipients、sim_time、content 与 attachments。它不是核心领域表。

## 判定谓词

### 派发激活
当前 `definition_version` 恰有一名 OWNER assignment，且全部 OWNER/COLLABORATOR assignment 均为 ACCEPTED；系统在同一事务激活 owner、协作者和首个 ACTIVE CommitmentRevision，并将 ActionItem 转为 `TRACKING`。任一成员退回则不满足此谓词。

### 交付完备
在 Episode 截止时间，所有必需 ActionItem 均为 `ACCEPTED | AGGREGATED | ARCHIVED`，终稿已生成且字段均来自生成时最新人工验收通过版本，不存在待执行的关键审批。

### 旧版本混入
终稿字段 lineage 指向的 version_id 不是终稿生成时该 ActionItem.current_valid_version_id，或对应 AcceptedTaskResult 与该 version_id 不一致。

### 恢复成功
故障运行恢复后，与同输入的无故障运行在业务状态、终稿内容和外部可见动作上语义等价，且不存在重复外发。

### 零重复外发
同一 EffectId 在 IM 适配器中最多产生一个可见 message_id；重复投递仅返回已有回执。

### 承诺留存
系统返回的负责人、交付内容与承诺截止时间和当前 ACTIVE CommitmentRevision 完全一致。

### 有效评测 Episode
内容包加载和测试基础设施成功启动的 Episode。被测系统自身故障不得将 Episode 排除出结果。

### 最新有效验收版本
同一 ActionItem 中 `validation_status=PASSED AND review_status=ACCEPTED` 且 `(received_sim_time, received_sequence)` 最大的 ArtifactVersion；由 ActionItem.current_valid_version_id 指向。
