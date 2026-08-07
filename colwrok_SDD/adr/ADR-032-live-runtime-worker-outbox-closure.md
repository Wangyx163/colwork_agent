# ADR-032｜真实业务时钟、独立运行循环与主动通知进入 P0

状态：ACCEPTED  
日期：2026-08-06  
影响：04-system-sdd、N04、N09、N14、接口契约、验收计划与 MVP Profile

## 背景

P0 的领域服务和确定性评测已经具备信号判断、异常询问、成果处理、终稿组织与 Outbox 能力，但生产工作台入口没有推进业务时间或持续执行策略/Dispatcher；模型处理还与 HTTP 请求位于同一串行循环。由此会出现工作台打开后不主动追交付、求助对象不被主动通知、模型慢时页面不可用、未终态 Outbox 重启后不自动恢复等问题。

## 决策

1. 最小真实业务时钟进入 P0。N14 提供统一 ClockSource：评测模式使用 VirtualClock，真实工作台使用单调前进的 SystemClock；二者调用同一 Scheduler 与领域策略。
2. Web/API 与持久化 Worker 使用隔离的运行循环，并各自持有数据库连接；可以由同一代码库和启动脚本管理，但模型调用不得阻塞 HTTP 请求处理。
3. Worker 只从 PostgreSQL 当前状态推导下一步：同步业务时间、恢复旧领取、处理待处理版本、排队/处理终稿、评估策略并投递 Outbox；不得依赖进程内流程位置。
4. AssistanceRequest 的创建、确认、解决和取消复用 OutboxEntry、AuditEvent 与稳定 EffectId 主动通知对应当事人；不增加通知业务实体。
5. 通用 Dispatcher 必须实现接受前失败重试、接受后响应丢失去重、重试耗尽死信和启动恢复。
6. P0 增加只通过 HTTP、数据库和进程启停驱动的 Live E2E；测试不得直接调用领域 service 方法推进时间、策略或投递。

## 运行顺序

每个 Worker Tick 按以下逻辑顺序运行，单步提交领域事务：

1. 从 ClockSource 取得不早于已持久化业务时间的当前时间；
2. 回收旧 Worker/Dispatcher 遗留的未终态领取；
3. 处理一个待处理 ArtifactVersion；
4. 检查并处理一个待组织终稿；
5. 从领域状态推导到期、无信号、返工和求助相关动作；
6. 原子领取并投递当前可用 OutboxEntry；
7. 无可执行工作时按有界间隔等待，不持久化第二套 timer job。

同刻已到达的用户输入仍优先于调度动作；EffectId 和数据库唯一约束保证重复 Tick 不重复外发。

## 非目标

- 不引入 Redis、Celery、Kafka、LangGraph、通用工作流引擎或新的业务角色。
- 不把 SystemClock 用于确定性评测；不让页面刷新成为业务信号。
- 不在 P0 接入真实飞书身份或多 Episode 并行调度。

## 结果

- P0 的“主动追交付”和“主动求助通知”从测试能力变为生产运行能力。
- 模型慢或重试时工作台仍可操作。
- 运行补位复用现有实体与状态机，没有重复造 WorkerJob、Notification 或 Workflow 实体。
