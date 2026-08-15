# ADR-035｜版本化多人派发与个人执行视图

状态：ACCEPTED  
日期：2026-08-07  
影响：替代 ADR-028 的公开认领语义，修订 ADR-030、ADR-034、N01、N02、N10、N13、N15

## 背景

公开认领池不能表达会议负责人已经明确指定执行人与协作者的真实协作，也无法让多人分别接受、留言或把有问题的任务定义退回修改。若把多人响应塞入 `ActionItem.owner_actor_id` 或 JSON 元数据，会失去逐人状态、权限校验和版本绑定；若为协作者再建一套任务系统，又会破坏单任务、单版本链和双 Human Gate 的既有主线。

## 决策

1. COORDINATOR 完成任务定义后，不发布到公开认领池，而是派发给一名主负责人和零到多名协作者；所有人必须是当前 Episode 的显式参会者。
2. ActionItem 始终只有一个 `owner_actor_id`。多人不是共同 owner：主负责人负责最终候选，协作者提交的仍是同一 ArtifactVersion 链中的贡献。
3. 增加关系记录 `ActionItemAssignment`，按 `action_item_id + definition_version + actor_id` 唯一，记录 `OWNER | COLLABORATOR`、`PENDING | ACCEPTED | RETURNED | SUPERSEDED`、回应留言与时间。它只表达版本化派发关系，不建立第二套任务状态机。
4. ActionItem 增加 `definition_version`。派发后进入 `PENDING_ASSIGNMENT`；全部成员接受后，在同一事务设置 owner、创建主负责人首个 CommitmentRevision、激活协作者并进入 TRACKING。
5. 任一成员选择“退回重改”时，ActionItem 进入 `NEEDS_REVISION`，整轮其余派发响应全部失效；暂停提交、催办和协作推进。COORDINATOR 修改后令 `definition_version + 1` 并重新派发，所有成员必须对新版本重新回应。
6. 接受或退回时可附带一条留言；P0/P1 不建设任务聊天线程。派发、接受、退回、重派与留言均进入现有 AuditEvent 时间线。
7. 参与者工作台不展示公开任务池。待回应任务放在右上角闹铃 Popover；接受后进入统一个人时间线。蓝色表示本人负责，紫色表示本人协作；历史参与任务移出活跃时间线并折叠展示。
8. 参与者只看自己的任务明细、同一任务成员的角色/回应状态，以及会议级聚合进度；不读取其他人的个人承诺、进展详情、阻塞原因或提交正文。COORDINATOR 使用同一事实源的完整管理投影。
9. 时间线点击任务后，高亮当前条并降低其他条透明度；下方任务执行区切换到被选任务。手动刷新按钮与高频轮询均不进入目标产品。
10. CollaborationMemory 改为预制 topic/value 词表。本人只能确认、从同一 topic 的预制值中替换或拒绝，不允许自由创建评价词条，也不提供展示偏好开关。CONFIRMED 词条可被 SYSTEM 用于 `COLLABORATION_HINT`，并只向当前有效协作关系展示最小可操作提示。

## 结果

- 派发对象、回应和任务定义版本一一绑定，不会出现有人接受旧定义、有人执行新定义。
- 多人协作仍复用一条 ActionItem 和 ArtifactVersion lineage，任务负责人 Gate 与会议负责人 Gate 保持不变。
- 个人端聚焦执行，管理端保留全局可视，权限和页面信息架构一致。
- 飞书只需在 P2 替换 PrincipalProvider 与消息/卡片 Adapter，不改变领域命令。

