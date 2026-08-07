# 多同事会议行动项协作 Agent

## AI 面试版 P0

当前 P0 的主线是：单 Agent 从数据库读取下一步，跨多步推进任务成果处理和终稿组织；任务状态、版本与上下文引用全部外置，人工负责确认与验收，进程中断后按原版本和 EffectId 恢复。

零外部模型调用的日常演示与评测：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_ai_p0_demo.ps1
```

输出 `var/ai-p0-report.json`，包含四组证据：结构化事实抽取、Agent/HITL/lineage、恢复/幂等、Context/Token。真实百炼契约烟测仍使用 `scripts/run_bailian_contract_smoke.ps1`，只在需要验证模型契约时执行，避免每次回归消耗 Token。

真实会议运行时使用两个独立进程，共享同一个 PostgreSQL/SQLite：

```powershell
# 终端 1：Web/API，只处理人的交互
powershell -ExecutionPolicy Bypass -File scripts/serve_meeting_workbench.ps1 `
  -ResultProcessing bailian

# 终端 2：Agent Worker，恢复并推进数据库里的下一步
powershell -ExecutionPolicy Bypass -File scripts/run_meeting_agent.ps1 `
  -ResultProcessing bailian
```

Worker 默认只处理任务负责人确认的最终候选。若组织已明确授权把“尚未由负责人处理的协作贡献正文/链接/附件抽取文本”发送模型，可显式增加 `-AllowContributionAnalysis`；不加该开关时协作贡献仍可由负责人直接纳入、退回或提升，附件二进制始终不发送。

模型成本规则见 `colwrok_SDD/17-ai-interview-p0-plan-and-model-routing.md`：常规文档/UI/固定测试用 Luna 或 Terra low，Harness 与普通实现用 Terra medium，只有 Agent Loop、Context Builder 和 lineage 跨层变更使用 Terra high，xhigh 不作为默认模型。

P0 当前采用模块化单体。本地开发使用 SQLite，目标 PostgreSQL DDL 位于 `db/postgres_schema.sql`，两者共享相同的领域与事务语义；附件正文抽取使用 `pypdf`。

## 运行

```powershell
$env:PYTHONPATH = "src"
python -m collab_agent eval --fresh
python -m unittest discover -s tests -v
python -m collab_agent serve
```

使用百炼从真实逐字稿抽取待确认行动项：

```powershell
.venv\Scripts\python.exe -m collab_agent extract `
  --input "C:\path\meeting.txt" `
  --output var\extractions\meeting.json `
  --meeting-date 2026-03-02
```

百炼负责候选抽取与受约束的终稿语义整理；schema 校验、本人确认、协调策略、版本引用、审批和发布仍由本项目控制。

将抽取结果导入 PostgreSQL 并打开任务协作工作台：

```powershell
$env:COLWORK_MEETING_TRANSCRIPT = "C:\path\meeting.txt"
$env:COLWORK_MEETING_ORGANIZATION = "你的团队"
$env:COLWORK_MEETING_COORDINATOR = "会议负责人"
$env:COLWORK_MEETING_PARTICIPANTS = "参会者甲,参会者乙,参会者丙"
powershell -ExecutionPolicy Bypass -File scripts/serve_meeting_workbench.ps1 `
  -ResultProcessing bailian
```

参会名单是 P0 的权限边界，必须显式提供，系统不会从逐字稿猜测成员。也可以向脚本传入
`-Participants "参会者甲","参会者乙","参会者丙"`。同一会议再次载入时名单必须与首次
导入一致，避免悄悄改变已建立的派发、协作和求助权限。

需要隔离验证启动脚本时可选择 SQLite，不会改动 PostgreSQL 工作台：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/serve_meeting_workbench.ps1 `
  -Participants "参会者甲","参会者乙" `
  -DatabaseMode sqlite `
  -Database var\meeting-smoke.sqlite3 `
  -ResultProcessing local
```

`-ResultProcessing bailian` 是部署级外部处理授权，Web/API 本身不执行模型调用；独立 Agent Worker 只会在负责人最终候选通过确定性校验、但尚未由会议负责人验收时，把该任务的标题、提交正文、链接元数据和附件抽取文本发送给百炼，不发送附件二进制。两个进程应使用相同模式。得到持续授权后可在 `.env.local` 写入 `COLWORK_RESULT_PROCESSING_MODE=bailian`，此后两条启动脚本省略参数也会沿用授权；命令行显式参数仍可覆盖。也可选择 `local`（离线确定性模板）或在工作台侧选择 `disabled`。模型失败会由持久化重试状态/Outbox 记录，不静默回退为简单终稿。

打开以下页面：

- `http://127.0.0.1:8766/tasks`：处理派发提醒、查看个人任务时间线、记录快捷状态、向参会者求助、修改个人承诺和提交成果；任务负责人还可处理协作者贡献，协作结束后协作者保留只读跟踪。
- `http://127.0.0.1:8766/manage`：会议负责人修改任务定义、配置一名主负责人和多名协作者并派发，只验收已由任务负责人确认的最终候选，并查看自动终稿和最终放行。
- `http://127.0.0.1:8766/diagnostics`：开发验收指标与全局审计，不占用业务页面。

提取结果仍存放在同一张 `action_items` 表中。负责人先修订任务定义和团队时间，
再配置一名主负责人及零到多名协作者并派发；所有被派发成员逐个接受后，系统原地
创建首个 CommitmentRevision 并进入 `TRACKING`，不建立 Claim 或独立协作任务实体。
任一成员“退回重改”都会终止本轮响应，负责人修改定义后重新派发。表单草稿保存在
浏览器本地；页面不会定时刷新，也不提供手动刷新按钮，只在用户完成业务提交后重新读取状态。

一键配置 `.venv`、Psycopg 与便携 PostgreSQL 18：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/setup_dev.ps1
```

也可单独配置数据库（无需 Windows 服务或管理员权限；脚本校验官方 ZIP、
生成随机凭据并写入已忽略的 `.env.local`）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/setup_postgres.ps1
powershell -ExecutionPolicy Bypass -File scripts/verify_postgres.ps1
# 使用 PostgreSQL 的 colwork_evaluation schema 跑 P0 E2E；不会清理工作台 public schema
.venv\Scripts\python.exe -m collab_agent eval --postgres --fresh
# 从 PostgreSQL 读取工作台
.venv\Scripts\python.exe -m collab_agent serve --postgres
# PostgreSQL 集成测试（连接串只通过环境变量传入）
$env:COLWORK_TEST_POSTGRES_URL = (Get-Content .env.local | Select-String '^DATABASE_URL=').Line.Replace('DATABASE_URL=', '')
.venv\Scripts\python.exe -m unittest discover -s tests -v
# 不使用时可停止本项目的本地实例
powershell -ExecutionPolicy Bypass -File scripts/stop_postgres.ps1
```

评测输出写入 `var/report.json`；SQLite 使用 `var/p0.sqlite3`，PostgreSQL
评测固定使用隔离的 `colwork_evaluation` schema。运行
`serve` 后在 `http://127.0.0.1:8765` 查看本地工作台。

固定评测不是预分配任务的捷径：四个 ActionItem 初始 owner 为空，场景执行
负责人复核/派发、成员响应、双工期冲突、信号/求助、PDF/text/正文提交、退回
重交、Memory 决策、崩溃恢复、终稿被新版本替换和最终归档。HTTP 运行态回归可用：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/runtime_smoke.ps1 `
  -BaseUrl http://127.0.0.1:8766
```

真实百炼契约烟测（只发送结构化测试文本和附件抽取文本）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_bailian_contract_smoke.ps1
```

真实会议回归不需要建设大数据集。建议总计准备 3–5 份：1–2 份开发回归、
1 份带最小人工预期、至少 1 份从未用于调整 Prompt/规则的最终盲测。每份只需
UTF-8 逐字稿、会议日期、会议负责人和准确参会名单；盲测在首次抽取前不要提供
“应该有哪些任务”。另准备一份正文/PDF/text 的正常交付和一份故意不完整的初版，
即可覆盖退回、重交、版本替换与终稿更新。参会名单仍需显式提供，不能从正文猜。

开发适配器使用 Python 标准库与 SQLite；生产目标仍为 PostgreSQL，权威
DDL 位于 [`db/postgres_schema.sql`](db/postgres_schema.sql)。二者共享领域模型、
稳定 `effect_id`、审计轨迹和 Outbox 语义，不另建一套工作流状态。

## 飞书接入（最小闭环）

派发、确认、求助从自建网页搬到飞书。`FeishuIM` 与 `MockIM` 是同一份适配器契约
（`send(command, *, accepted_sim_time)` → `external_message_id` / `deduplicated` /
`accepted_sim_time`），所以 `dispatch_all` 一行未改就能对真实租户发消息，确定性
评测继续用 `MockIM`。

Outbox 的 `effect_id` 直接作为飞书发消息接口的 `uuid` 幂等键：进程在"飞书已接收、
本地尚未落库"之间崩溃时，重试用同一个 uuid，由飞书返回原消息而不是给收件人发第二条。

装依赖：

```powershell
.venv\Scripts\python.exe -m pip install -e ".[feishu]"
```

绑定参会人到飞书 `open_id`。与参会名单一样，绑定必须显式，系统不从消息里猜人。
绑定的键是 `actors.actor_id`，命令接受显示名并自动解析，因此必须先载入会议：

```powershell
.venv\Scripts\python.exe -m collab_agent feishu-bind `
  --db var\feishu-meeting.sqlite3 --actor "参会者甲" --open-id "ou_xxxxxxxx"
.venv\Scripts\python.exe -m collab_agent feishu-bind --db var\feishu-meeting.sqlite3 --list
```

在 `.env.local` 写入 `FEISHU_APP_ID` 和 `FEISHU_APP_SECRET` 后启动长连接。长连接
自带加密与鉴权，不需要公网 IP、内网穿透或自己验签。启动约需 17–20 秒，
这段时间没有输出是 lark SDK 在导入。

回调侧遵守飞书 3 秒预算：`handle_message_receive` 和 `handle_card_action` 只做
持久化入队并立即返回（卡片点击当场回一个 toast），发送、渲染和状态推进全部落到
worker 线程。卡片点击以飞书 `event_id` 去重，重投不会变成第二次决策；处理失败写
`feishu_inbound_actions.status = 'FAILED'` 并保留 `process_error`，不静默丢弃。

给机器人发消息只会得到只读回复：未绑定的人收到一张带自己 `open_id` 的提示卡片
（这是拿到 open_id 的唯一自举路径），已绑定的人收到一句"派发会自动推送、无需回复"。
带决策按钮的卡片只由 `AssignmentNotifier` 从真实的待响应派发推出——聊天里现造的卡片
其 EffectId 对应不到任何任务，点下去只会失败。

### 用飞书跑一场真实会议

```powershell
# 1. 先载入会议，建立参会者（此时还没人绑定）
.venv\Scripts\python.exe -m collab_agent feishu-serve `
  --extraction var\extractions\meeting.json `
  --transcript C:\path\meeting.txt `
  --organization "你的团队" --coordinator "会议负责人" `
  --participant "会议负责人" --participant "王昱翔" `
  --db var\feishu-meeting.sqlite3 --dry-run

# 2. 按显示名绑定，命令会自动解析成内部 actor_id
.venv\Scripts\python.exe -m collab_agent feishu-bind `
  --db var\feishu-meeting.sqlite3 --actor "王昱翔" --open-id "ou_xxxx"

# 3. 去掉 --dry-run 正式运行
```

绑定必须与 `feishu-serve` 用同一个 `--db`，两条命令的默认库不同。绑定的键是
`actors.actor_id`，不是显示名——所有协作命令都按内部 id 鉴权；`feishu-bind`
接受显示名并自动解析，解析不到会明确告诉你并原样存储。

派发通知不走 Outbox。本项目的派发是拉取式的：`dispatch_action` 只记录谁被派发，
不产生外部效应，因为网页工作台预期成员自己去看。飞书需要推送，所以
`AssignmentNotifier` 把"待响应的派发"投影成卡片——这是投影不是领域改动，EffectId
由 `(action_item_id, definition_version, actor_id)` 这个天然键导出，重启、重投或
重复轮询都解析到同一个效应，FeishuIM 拒绝重发；修订后重新派发会抬高
`definition_version`，那是真正的新效应，应该再次送达。

卡片上的「退回重改」是下拉选择而非按钮：领域拒绝没有理由的退回，而裸按钮点击带不回
理由，所以选原因和退回必须是同一个动作。

点击处理链路：飞书 `event_id` → `feishu_inbound_actions`（去重）→ worker 线程 →
`respond_to_assignment(message_id=event_id)`。最后这个参数让 `inbound_receipts`
接管幂等，重投的点击直接回放已存结果，不会二次决策。

## 当前边界

- 固定评测 fixture + 百炼真实逐字稿抽取/终稿整理；外部模型不决定身份、状态、催办等级、审批或版本指针。
- 单 Episode 内串行事件循环与 VirtualClock；数据库允许多个 Episode 并存。
- 领域状态、AuditEvent 与 Outbox 同事务。
- 外部发送以 EffectId 幂等。

完整规范见 `colwrok_SDD/00-README.md`。
