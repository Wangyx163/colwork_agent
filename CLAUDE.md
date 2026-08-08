# 给 Coding Agent 的上下文路由

`colwrok_SDD/` 有 49 篇文档、约 17 万字符。**整包读进来约 12–17 万 input token，
而绝大多数工程任务只需要其中 15k–30k。** 这份文件决定该读哪些。

**默认不要通读 `colwrok_SDD/`。** 按下面三层取用。

---

## Layer A：任何任务都先读（约 10k token）

| 文件 | 回答什么 |
|---|---|
| `README.md` | 项目是什么、当前能力、怎么跑 |
| `colwrok_SDD/02-constitution.md` | 什么永远不能做 |
| `colwrok_SDD/07-state-machines.md` | 状态机与不变量 |
| `capabilities.json` | 每项能力当前是 DONE 还是 NOT DONE（机器可读） |

## Layer B：按任务加载（约 10k–30k token）

| 任务类型 | 加载 |
|---|---|
| 飞书 / IM 适配 | `src/collab_agent/feishu_*.py`、`08-interface-contracts.md` 的 IM Adapter 段、`adr/ADR-032` |
| 抽取 / Prompt | `src/collab_agent/extraction.py`、`fixtures/ANNOTATION_GUIDE.md`、`adr/ADR-033` |
| 评测 / 指标 | `src/collab_agent/extraction_evaluation.py`、`adr/ADR-026` |
| 领域状态迁移 | `05-domain-model.md`、`06-event-catalog.md`、`07-state-machines.md` |
| 跨会议关联 | `src/collab_agent/linkage.py`、`src/collab_agent/embeddings.py` |
| 存储 / 迁移 | `db/postgres_schema.sql`、`src/collab_agent/store.py`、`postgres_store.py` |

## Layer C：只在审计或追溯时读

`adr/`、`10-traceability-matrix.md`、`09-acceptance-test-plan.md`、
`15-*`、`16-*`、`18-*`。

---

## 陈旧信息警告

以下文档含**历史 TODO**，读的时候不要当成当前待办：

- `15-p0-closure-audit-and-agent-routing.md` — 标记 HISTORICAL
- `16-p0-p1-vertical-slices-and-model-routing.md` — 标记 SUPERSEDED
- `18-current-and-target-full-chain-blueprint.md` — 30.6k 字符，含大量已完成项的历史描述

它们仍写着「真实 ClockSource 尚未完成」「通知尚未完成」「P0 尚未闭合」，
而后续文档已说明这些已完成。**判断"现在做完没有"一律以 `capabilities.json` 为准，
不以正文叙述为准。**

同理，**规范正文里出现的测试数量（102 tests、111 tests 之类）是某次构建的快照，
不是架构事实**。当前数量跑 `python -m unittest discover -s tests` 自己看。

---

## 硬约束（改代码前必读）

1. **模型不决定身份、状态、催办等级、审批或版本指针。** 模型只做候选抽取、
   受约束的终稿整理、跨会议关联提议——三者产出都要过确定性校验，且都不推进状态。
2. **参会名单是授权边界。** 不从逐字稿猜参会者；跨 Episode 读取必须复用名单过滤。
3. **领域状态、AuditEvent 与 Outbox 同事务。**
4. **外部发送以 EffectId 幂等**，重试复用原 EffectId，已投递不可逆。
5. **SQLite 与 PostgreSQL 共享同一套领域语义**，CI 两边各跑一遍全量。
   任何只在一个后端可用的特性（如 pgvector）都要先付这个代价。
6. **能事后确定性校验或修复的操作，不要做成工具交给模型。**
   引文定位属于代码；语义关联属于模型。依据见 README 的工具调用对照组一节。

## 惯例

- 注释解释**为什么**，不解释代码在做什么
- 新功能先能离线测试（注入可替换的 completer / embedder），再接真实 provider
- 演示路径优先确定性方案，不在演示中调模型
