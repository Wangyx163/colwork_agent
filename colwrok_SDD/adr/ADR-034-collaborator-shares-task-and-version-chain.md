# ADR-034｜协作者复用任务与版本链

状态：ACCEPTED  
日期：2026-08-06  
影响：修订 ADR-031、N04、N06、N13、N15 与 MVP Profile 中的协作者权限

## 背景

把协作者只作为展示元数据，会迫使产品再造一套“协作交互/协作交付”界面：协作者无法在原任务更新状态或提交，负责人也无法从任务记录判断谁实际推进和交付。这既割裂版本 lineage，也增加了不必要的业务实体。

## 决策

1. ActionItem 仍只有一个 `owner_actor_id`；协作者不是共同 owner，按 ADR-035 接受自己的 COLLABORATOR assignment，但不拥有负责人的 CommitmentRevision。
2. 协作者来源只有两类：会议原文明确写入的 `collaborator_actor_ids`，以及状态为 `OPEN/ACKNOWLEDGED` 的 AssistanceRequest 目标；前者持续有效，后者随关系解决或取消而撤销。
3. 任务负责人和当前协作者统称 contributor。contributor 复用同一任务空间，可记录快捷状态并向同一 ArtifactVersion 序列提交；只有负责人可修改个人承诺，只有 COORDINATOR 可人工验收。
4. ArtifactVersion 冻结 `submitted_by_actor_id` 与 `contributor_role=OWNER | MEETING_COLLABORATOR | REQUESTED_COLLABORATOR`，使负责人、协作者和最终报告都能识别真实交付人。
5. 协作者提交默认分类为 `CONTRIBUTION`：版本通过确定性校验后保持 ActionItem 原状态，由 Worker 以 `CONTRIBUTION_ANALYSIS` purpose 生成“与整项任务契约的覆盖/缺口”辅助信息，不直接进入 COORDINATOR 验收。
6. 任务负责人是协作贡献的第一道 Human-in-the-loop Gate，对每个贡献版本只作一次 `INCLUDE | REQUEST_REVISION | PROMOTE` 决定：纳入资料或要求补充时任务继续推进；只有 `PROMOTE` 才把该版本变成最终候选并令 ActionItem 进入 `PENDING_ACCEPTANCE`。COORDINATOR 的人工验收是第二道 Gate。
7. AssistanceRequest 结束后，协作者失去新增提交和返修权限，但保留对该任务状态、自己历史贡献和相关协作记录的只读可见性；若要继续返修，负责人必须重新邀请。会议原文明示协作者不受 AssistanceRequest 生命周期影响。
8. 任务协作记录合并承诺、所有 contributor 状态、协作邀请/确认/解决/取消、贡献版本、负责人处理与最终验收结果，并按事实事件时间排序。不得建立协作专用 ActionItem、Artifact、状态机或第二套页面流程。
9. 权限由服务端根据 ActionItem 元数据与 AssistanceRequest 当前状态推导；UI 标记和隐藏不作为授权依据。

## 结果

- 多人协作仍是一个可验收任务，不产生任务同步和双版本链问题。
- 协作者拥有真实工作空间，负责人能从单条时间线追踪协作与交付。
- “有人交了协作材料”与“整项任务已完成”不再混成同一个状态；部分成果可以被 AI 分析，但不能越过任务负责人直接送验。
- P1 的多人并行收集、顺序交接和多人决策模板仍保持延期；本决策只扩展基础 ActionItem 的 contributor 权限，不引入 Workflow、Stage、LangGraph 或多 owner。
