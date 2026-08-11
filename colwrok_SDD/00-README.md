版本：1.4  
上游依据：`01-project-charter.md`

## 1. 文档优先级

`02-constitution.md` ＞ 已接受 ADR ＞ `12-mvp-priority-profile.md` ＞ `04-system-sdd.md` ＞模块 spec ＞实现计划＞ README 与演示材料。

## 2. 使用方式

1. 先阅读 `02-constitution.md` 与 `03-glossary.md`
2. 再阅读 `04-system-sdd.md`、`05-domain-model.md`、`06-event-catalog.md`、`07-state-machines.md`
3. 阅读 `adr/ADR-030-minimal-access-signals-schedule-memory.md`、`adr/ADR-031-base-task-first-collaboration-patterns-p1.md`、`adr/ADR-033-interview-ai-engineering-focus.md`、`adr/ADR-036-recall-first-extraction-and-review-hints.md`、`12-mvp-priority-profile.md` 与 `17-ai-interview-p0-plan-and-model-routing.md`，先实现 P0
4. 按 `specs/` 中 N01–N15 的逻辑依赖实现，但不要求拆成独立服务
5. 以 `09-acceptance-test-plan.md` 的 P0 场景与 `contracts/metrics-contracts.md` 的 P0 契约验收
6. 修改冻结规范时，在 `adr/` 追加 ADR；不得直接修改低层文档规避冲突

## 3. 包含文件

- `01-project-charter.md`：定稿立项书原文
- `02-constitution.md`：不可违反的系统原则
- `03-glossary.md`：术语入口
- `04-system-sdd.md`：系统边界、架构、非功能约束
- `05-domain-model.md`：领域对象、字段与数据关系
- `06-event-catalog.md`：AuditEvent 信封与审计事件目录
- `07-state-machines.md`：ActionItem、Approval、Outbox 状态机
- `08-interface-contracts.md`：模块间逻辑接口与标准消息结构
- `09-acceptance-test-plan.md`：自动化、恢复、端到端验收计划
- `10-traceability-matrix.md`：目标—模块—契约—测试追踪
- `11-nonblocking-deferred.md`：P1/P2 非阻塞后续项
- `12-mvp-priority-profile.md`：组织/用户分层、业务分层与 P0–P3 优先级
- `13-ai-capability-routing.md`：Prompt、Skill/Adapter 与确定性系统的运行边界
- `14-business-flow-access-context.md`：P0 业务链路、最小权限矩阵与上下文分层
- `15-p0-closure-audit-and-agent-routing.md`：基础任务真实运行闭环审计、P0 阻断项与轻量 Agent 路由草案
- `16-p0-p1-vertical-slices-and-model-routing.md`：P0/P1 端到端研发任务、依赖、验收与 Luna/Terra 等级路由
- `17-ai-interview-p0-plan-and-model-routing.md`：面试版 AI P0 主线、停止项与低成本模型升级纪律
- `specs/`：N01–N15 模块规格
- `contracts/metrics-contracts.md`：GATE＋流程/效果/单点 SIGNAL 分层验收契约
- `content-packs/content-pack-spec.md`：内容包标准及通用周会包默认参数
- `adr/ADR-index.md`：ADR 索引
