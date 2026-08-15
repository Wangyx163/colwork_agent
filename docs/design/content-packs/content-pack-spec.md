# ContentPack 规格

版本：1.3  
变更依据：ADR-030

## PACK-001｜Manifest

- 必需字段：pack_id、pack_version、source、timezone、start_sim_time、evaluation_cutoff_sim_time、organization、actors、transcript、policy、action_items。
- P0 ContentPack 是纯配置与 fixture，不得注入核心代码分支。
- P0 不单设第二套工作流或 TaskPack；交付 schema、行为和 SLA 收敛在 action_items/policy。
- 验证：AUTO-PACK-001。

## PACK-002｜Actor 与参会名单

- 必需字段：actor_id、display_name、roles；fixture 只保存验收所需的最小虚拟身份，不建立 BehaviorProfile 或 Conversation 实体。
- P0 roles：`COORDINATOR | PARTICIPANT`；固定场景为 1 名 COORDINATOR、3 名 PARTICIPANT，同一人可执行多个 ActionItem。
- Principal fixture 与 Actor 分开：虚拟会话必须签名绑定 actor_id，不能让请求正文选择任意身份。
- MANAGER/LINE_MANAGER 为 P1，不出现在 P0 固定场景。
- 验证：AUTO-PACK-002。

## PACK-003｜ActionItem 内嵌交付 Schema

- 每个 ActionItem 必需字段：action_item_id、title、deliverable_key、team_required_by_sim_time、source_span、work_requirements、management_review_policy、required_fields、behavior。
- P0 主 fixture 的 owner_actor_id 必须为空；个人 promised_by 不写进提取配置，由当前派发版本全部接受时创建的首个 CommitmentRevision 补齐。只有兼容旧上游的专门单测允许预分配 owner。
- management_review_policy 只进入 COORDINATOR projection 与 TASK_RESULT_REVIEW purpose。
- declared_version_label 仅展示；新旧判定以接收顺序、校验和人工验收结果为准。
- 验证：AUTO-PACK-003。

## PACK-004｜行为脚本

- P0 行为标签为字符串数组，固定场景联合覆盖：`NORMAL | SILENT_IN_CHECK_WINDOW | VERSION_REFRESH | INVALID_THEN_FIX | REQUEST_HELP`；一个任务可以同时承担版本刷新与求助场景。
- 每一步只由 VirtualClock、寻址给该 actor 的消息和 actor 自身脚本状态触发；不得读取 Agent 领域内部或他人私有字段。
- 刷新/已读 fixture 不得产生 ProgressSignal。
- 验证：AUTO-PACK-004。

## PACK-005｜SLA、信号与处理策略

- 默认值：
  - timezone=`Australia/Sydney`
  - confirmation_timeout=`0.5 simulated_workday`
  - check_in_lead=`1 simulated_workday`
  - silence_window=`0.5 simulated_workday`
  - l2_wait=`0.5 simulated_workday`
  - inquiry_cooldown=`0.5 simulated_workday`
  - daily_touch_budget=`2 per participant`
  - signal_default_ttl=`1 simulated_workday`
  - progress_window=`0.5 simulated_workday`
  - outbox_max_attempts=`5`
  - supported_mime=`text/*, application/pdf`
- 所有阈值必须从冻结配置追溯，不得散落在 Prompt 或模块代码。
- P0 不含 l3_wait、manager recipient 或组织外外包配置。
- 验证：AUTO-PACK-005。

## PACK-006｜P0 固定场景

- 会议转写产生 4 个 required ActionItem，覆盖 3 名 PARTICIPANT。
- 场景含：一次 promised_by 晚于 team_required_by、一次检查窗口无信号、一次参会者求助、一次未参会求助拒绝、一次 PDF/text 处理、同任务两个版本、一次无效后修正和一次 Outbox 恢复。
- 每项任务逐项验收并冻结 AcceptedTaskResult；终稿从 current_valid_version_id 和结果记录聚合。
- 首个待发布终稿产生后再验收一个任务的新版本；旧终稿和 Approval 必须自动 SUPERSEDED，并按新版本指纹主动生成替代终稿，不允许依赖人工“重新汇总”按钮。
- 验证：AUTO-PACK-006、TST-E2E-001。

## PACK-007｜后续扩展

- P1 可增加 LINE_MANAGER/L3、更多行为和跨 Episode Memory。
- P2 可泛化为 TaskPack 并接入飞书，但不得提前在 P0 引入插件框架或组织级 IAM。

## PACK-008｜外部素材来源

- P0 可从 AliMeeting4MUG/AMC-A 选择少量行动项句作为会议语境素材，不将外部数据集设为运行依赖。
- 引入样本保存 upstream URL、版本/获取日期、许可证、原始样本 ID 与项目标注者。
- 上游标签不能替代本项目的 title、work_requirements、team_required_by、source_span 与 participant 标注。
- 外部下载或清洗超过 1 小时未完成时，切换到同 schema 的项目自建 fixture。
