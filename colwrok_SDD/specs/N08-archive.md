# N08｜发布与归档

版本：1.1

## P0 职责

- FINAL_RELEASE Approval 的 OutboxEntry DELIVERED 后，FinalDeliverable=RELEASED、Episode=ARCHIVED、所有 AGGREGATED ActionItem=ARCHIVED。
- 归档视图至少包含终稿、AcceptedTaskResult、lineage、当前承诺、所有 ArtifactVersion、AuditEvent 导出、ContentPack 版本与审批记录。
- 普通参会者在发布前不可见终稿；FINAL_RELEASE 送达后才可见 RELEASED 终稿。协作 Memory 仍按“仅本人可见”单独投影，不随终稿向负责人或其他参会者公开。
- ARCHIVED Episode 不再接受业务变更；相关晚到消息仅追加 `LateMessageIgnored` 审计，不修改归档结果。
- P0 可使用同一数据库的只读归档视图；独立归档存储与下载包为 P1。
- 验证：AUTO-N08-001 至 AUTO-N08-004。
