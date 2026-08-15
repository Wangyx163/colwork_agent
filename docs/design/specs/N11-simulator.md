# N11｜脚本同事模拟器

版本：1.2  
变更依据：ADR-030

## P0

- 同事的回复时机、是否回复、提交版本和交付有效性全部由 ContentPack 脚本与 VirtualClock 决定。
- P0 必须覆盖：`NORMAL | SILENT_IN_CHECK_WINDOW | VERSION_REFRESH | INVALID_THEN_FIX | REQUEST_HELP`。
- SILENT_IN_CHECK_WINDOW：可以产生已读/刷新 fixture，但在检查窗口内不产生有效业务信号，用于验证系统按规则询问。
- VERSION_REFRESH：先提交有效 v1，再提交字段变化的有效 v2。
- INVALID_THEN_FIX：先提交违反 schema 的版本，收到返工后提交有效修正版。
- REQUEST_HELP：只从同一 EpisodeParticipant 中选择他人发起求助，收到解决后继续提交。
- 每个脚本步骤只响应寻址给该 actor 的消息、VirtualClock 与自身脚本状态；固定 ContentPack 的行为顺序可复现。
- 模拟器通过签名虚拟 Principal 以固定 actor 身份调用 API，不得读取其他 actor 私有消息、提交正文或 Memory。
- 验证：AUTO-N11-001 至 AUTO-N11-008。

## P1

- 增加中途改数、主动澄清、部分交付与更多失败脚本。

## P2

- 可用 LLM 改写话术，但不得改变脚本决定的行为、时间、版本或结果；无模型时必须回退冻结模板。
- 不为 P0 建设独立真值库、反事实对象或模型缓存系统。
