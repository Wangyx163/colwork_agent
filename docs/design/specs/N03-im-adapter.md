# N03｜IM Adapter（P0 mock）

版本：1.1

## P0 职责

- 接收和发送 `08-interface-contracts.md` 定义的标准消息。
- 从调用凭证确定 sender_actor_id，不信任消息体自报身份。
- 为每条接受的入站消息分配严格递增 accepted_sequence；相同 message_id 幂等返回。
- 为每个出站 EffectId 最多创建一个可见消息；重复命令返回相同 external_message_id 与 `deduplicated=true`。
- 支持两种故障注入：接受前失败、接受后响应丢失。
- 验证：AUTO-N03-001 至 AUTO-N03-005。

## P2 扩展缝

真实平台 Adapter 只负责标准消息映射、鉴权、限流与平台回执，不得改变 ActionItem、策略或审批语义。
