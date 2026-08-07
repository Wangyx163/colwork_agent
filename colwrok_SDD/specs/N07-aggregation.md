# N07｜聚合与终稿修订

版本：1.3

## P0 职责

- 只有所有 required ActionItem 为 ACCEPTED/AGGREGATED、拥有 current_valid_version_id 且存在匹配 AcceptedTaskResult 时才可聚合。
- 按 ContentPack.final_schema_mapping 把源字段写入终稿；每个字段创建唯一 FinalFieldLineage。
- 终稿同时保留两层：`deliverables` 是已验收原始字段的权威集合；`organized_report` 是面向负责人的可读整理稿，至少包含总体摘要、关键发现、分任务章节、风险/缺口和建议下一步。
- 整理稿只能使用当前有效版本及其冻结验收结果中的事实；每个章节必须同时引用对应 `action_item_id + version_id + accepted_task_result_id`，跨任务结论必须显式列出 `source_version_ids + source_result_ids`。模型输出不改变原始交付、`current_valid_version_id` 或 AcceptedTaskResult。
- FinalDeliverable.payload 同时保留 `deliverables` 原始交付、`accepted_task_results` 冻结结果和 `organized_report` 可读整理稿；发布前服务端再次核对三者与当前任务指针完全一致，页面可见不代替该校验。
- 全部必需任务验收后，系统自动创建唯一 `FINAL_ORGANIZATION` Outbox effect；业务用户不需要点击按钮触发。
- 部署策略为 `bailian` 时，模型不可用或输出校验失败必须进入 `RETRY_WAIT/DEAD_LETTER` 并显示原因，不得静默回退成简单终稿；`local` 确定性模板只能由部署策略显式选择。
- 任一 required 字段缺失时聚合失败并返回 field_path，不创建 FINAL_RELEASE Approval。
- 相同版本指针集合与映射必须产生相同 payload、value_hash 与 lineage。
- 新的有效版本到达后，旧修订标记 SUPERSEDED 并保留为 `supersedes_final_deliverable_id`；系统依据新的 `version_id + accepted_task_result_id` 指纹自动创建新的 FINAL_ORGANIZATION Outbox effect，生成 revision_no+1 的 FinalDeliverable，不覆盖旧内容，也不要求负责人手动点击聚合。
- 验证：AUTO-N07-001 至 AUTO-N07-005。
