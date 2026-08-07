# ADR-030｜最小权限、事件信号、双工期与协作记忆

状态：ACCEPTED；公开认领与 Memory 自由纠正文案由 ADR-035 修订  
日期：2026-08-06  
影响：部分取代 ADR-024、ADR-029 中 P0 的角色、L3 与交付处理定义

## 背景

当前产品只服务“一次会议形成多项任务，由参会同事协作完成”的闭环。未来会接入飞书身份与交互，因此 P0 既不能继续信任网页请求中的姓名，也不应先建设一套需要飞书迁移时重写的完整 IAM。与此同时，手工填写长篇进展成本过高；协作推进需要由可审计的业务事件推导，并在缺少信号时进行低成本询问。

## 决策

### 1. P0 角色只保留三类 principal

- `COORDINATOR`：会议负责人，同时承担任务发起、管理规则维护、业务验收和终稿放行；P0 不另建“任务负责人”角色。
- `PARTICIPANT`：本次会议参与者；可被派发为 ActionItem 主负责人或协作者，并对当前版本逐人回应。
- `SYSTEM`：按明确 purpose 读取最小上下文并执行规则、模型或外发动作，不拥有人的批准权。

`LINE_MANAGER`、未参会的普通团队成员和组织级管理员不进入 P0。现有实现中的 `AGGREGATOR/ACTION_OWNER` 可暂作数据库兼容别名，但产品语言和新接口使用上述角色。

### 2. 只做可迁移的最小权限层

- 每个请求先由 `PrincipalProvider` 给出可信 `Principal{actor_id, episode_id, roles, auth_source}`；业务 API 不接受请求正文中的 actor_id 作为身份事实。
- 服务端校验 Episode 成员关系、ActionItem 执行人关系、动作权限和字段可见性。
- 查询按角色返回裁剪后的 projection，不能先返回全量对象再依赖前端隐藏。
- P0 可用签名虚拟会话模拟身份；接入飞书时只替换 `PrincipalProvider` 与交互 Adapter，领域授权规则不变。
- Prompt、temperature 和模型输出不得承担身份认证、字段权限或状态变更授权。

### 3. 用业务信号判断“是否失联”

有效活动信号只包括与 ActionItem 绑定、由执行人产生的可审计事件：派发接受或承诺修订、快捷状态回应、求助创建/更新、交付或重新提交、退回后的明确回应。页面在线、刷新、停留、已读、未提交草稿和系统自身发出的消息均不算信号。

通用异常询问只有在以下条件全部满足时产生：

1. ActionItem 为 `TRACKING`；
2. 不存在待处理的提交或有效交付；
3. 已进入个人承诺时间前的 `check_in_lead` 窗口，或已经逾期；
4. 距 `last_owner_signal_at` 已超过 `silence_window`；
5. 没有未解决的 AssistanceRequest，也没有仍有效的询问；
6. 当日触达预算未耗尽。

距离承诺时间尚远时，沉默本身不触发询问。存在未解决求助时，系统跟踪求助，不再发送泛化的“进展如何”。快捷回应为 `ON_TRACK | AT_RISK | BLOCKED | WAITING_INPUT | READY_TO_SUBMIT`，并带有效期，不能永久代表当前状态。

### 4. 求助对象只限本次会议参与者

执行人可以向会议负责人或指定参会者求助；目标必须存在于同一 EpisodeParticipant 集合。P0 不允许转给未参会同事，系统可以推荐参会者，但必须由发起人确认。AssistanceRequest 因具备独立参与者、状态和解决时点，作为业务实体持久化。

### 5. 分开团队需要时间与个人承诺时间

- `team_required_by`：团队何时需要结果，由 COORDINATOR 维护，对全体参会者可见。
- `promised_by`：执行人个人承诺，保存在 ACTIVE CommitmentRevision 中。
- 当 `promised_by <= team_required_by`，执行人可直接修订承诺；当 `promised_by > team_required_by`，不修改团队日期，创建可见的 schedule conflict，由 COORDINATOR 调整团队日期或要求重新承诺。

P0 提供团队工期视图，展示每项任务的执行人、两类日期、状态、最后有效信号和风险原因；不建设完整任务依赖图，只允许可选 `blocking_action_item_id`。

### 6. 交付结果与记忆分层

- 每次提交生成不可变 ArtifactVersion；附件二进制只存对象引用和哈希，抽取文本与模型处理结果绑定 version_id。
- 确定性校验通过后，系统可生成一次“验收辅助包”；执行人看到提交回执和通用问题，COORDINATOR 看到完整验收依据及仅管理侧可见的 review policy。
- 人工验收通过后冻结 AcceptedTaskResult：完成内容引用、完成报告、标准化结果、来源清单与处理元数据。它是版本绑定的派生记录，不另建第二套任务流程。
- 任务验收后自动生成事实型协作报告，并形成 CollaborationMemory 候选。ADR-035 修订为版本化预制词表：本人可确认、同 topic 替换或拒绝；当前有效协作者只读 CONFIRMED 最小提示。跨 Episode 汇总、衰减与冲突进入 P2。

### 7. L3 延后

直属上级不在当前会议任务协作边界内，P0 不提供 L3 或经理审批。P0 仅使用私聊/会话内询问、求助协作和 FINAL_RELEASE 人工放行。L3 作为可插拔 P1 策略保留，不得影响 P0 数据模型和接口。

## 结果

- P0 主旨仍是多同事协作，不因删除组织外角色而退化为单人任务工具。
- 进展更新由事件与快捷回应承担，减少手写维护成本；无信号的判断可测试、可解释。
- 飞书接入只替换身份与交互边缘，避免重写核心领域规则。
- 新增的业务实体仅有确有独立生命周期的 AssistanceRequest 与 CollaborationMemory；状态卡、协作报告、验收辅助包和 schedule conflict 均为事件或派生视图。
