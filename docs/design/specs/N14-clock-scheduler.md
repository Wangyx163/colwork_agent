# N14｜业务时钟与 Scheduler

版本：1.3  
变更依据：ADR-032、ADR-033

## P0 VirtualClock

- VirtualClock 单调递增，不得回退；可推进到下一个已知业务时间或指定未来时间，以保证固定场景、跨时间工作流和故障恢复可复现。
- Scheduler 不保存独立持久 timer job，每次从 ActionItem、ACTIVE CommitmentRevision、Intervention、触达记录和 ContentPack 推导到期触发。
- 相同时间重复运行时，已存在相应业务决策的触发不得创建新 Intervention 或 EffectId。
- 业务日边界使用 ContentPack.timezone；跨日后当天触达计数从新日期重新计算并记录 DailyBudgetReset 审计。
- 一个 Tick 处理完所有到期输入后返回 `settled=true`；该返回值只供测试观察，不持久化为流程状态。
- 页面刷新不是 Tick 或业务信号；Harness/Agent CLI 明确推进 VirtualClock 并运行规则到 settled。
- 验证：AUTO-N14-001 至 AUTO-N14-007。

## 不排期扩展

- SystemClock、真实工作日历、生产级定时 Worker 和时区/节假日集成不属于当前面试项目 P0/P1。
