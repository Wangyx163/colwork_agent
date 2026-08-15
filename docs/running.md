# Running the Project

本页集中说明演示、真实逐字稿、多会议控制台和分进程运行。README 的 Quickstart 只保留最快打开产品的路径。

## 1. Built-in demo

```powershell
$env:PYTHONPATH = "src"
python -m collab_agent serve
```

默认地址：

- 会议负责人工作台：`http://127.0.0.1:8765/manage`
- 任务负责人／协作者页面：`http://127.0.0.1:8765/tasks`
- 系统 Observatory：`http://127.0.0.1:8765/observatory`

`serve` 使用仓库内置演示数据和 SQLite，结果整理默认走本地确定性策略，不需要调用外部模型。

## 2. Import a real transcript

真实抽取需要在 `.env.local` 中配置 `DASHSCOPE_API_KEY`。

```powershell
python -m collab_agent extract `
  --input "C:\path\meeting.txt" `
  --output var\extractions\meeting.json `
  --meeting-date 2026-03-02
```

抽取 artifact 生成后，可以选择单进程体验或分进程运行。

### Option A: one process

`serve-meeting` 同时提供工作台并在后台运行结果处理 Worker，适合本地体验：

```powershell
python -m collab_agent serve-meeting `
  --extraction var\extractions\meeting.json `
  --transcript "C:\path\meeting.txt" `
  --organization "示例团队" `
  --coordinator "会议负责人" `
  --participant "成员甲" `
  --participant "成员乙" `
  --result-processing bailian
```

默认端口为 `8766`。单会议模式直接使用 `/manage` 和 `/tasks`，没有 meeting slug 前缀。

### Option B: separate web and worker processes

持久运行时可以把人的交互面和 Agent Worker 分开。两个进程必须使用同一个数据库；下面使用 PostgreSQL：

```powershell
# 终端 1：只提供工作台，不在 Web 进程内运行 Worker
python -m collab_agent serve-meeting `
  --extraction var\extractions\meeting.json `
  --transcript "C:\path\meeting.txt" `
  --organization "示例团队" `
  --coordinator "会议负责人" `
  --participant "成员甲" `
  --participant "成员乙" `
  --postgres `
  --result-processing disabled

# 终端 2：独立恢复并推进 Agent 工作
python -m collab_agent agent-meeting `
  --extraction var\extractions\meeting.json `
  --transcript "C:\path\meeting.txt" `
  --organization "示例团队" `
  --coordinator "会议负责人" `
  --participant "成员甲" `
  --participant "成员乙" `
  --postgres `
  --result-processing bailian
```

不要同时让 `serve-meeting` 使用 `--result-processing bailian` 并再启动独立 `agent-meeting`；前者已经包含后台 Worker。

## 3. Serve multiple meetings

`serve-meeting` 第一次加载会议时会登记来源。之后可以从同一个数据库恢复所有已登记会议：

```powershell
python -m collab_agent serve-console --db var\meeting.sqlite3
```

多会议模式使用：

- `/{meeting_slug}/manage`
- `/{meeting_slug}/tasks`
- `/observatory`：跨会议系统观察入口，仍位于进程根路径

## 4. Ports and network binding

| Command | Default port | Purpose |
|---|---:|---|
| `serve` | 8765 | 内置离线演示 |
| `serve-meeting` | 8766 | 导入并服务一场会议 |
| `serve-console` | 8766 | 从同一数据库服务多场会议 |

所有命令都接受 `--port`。默认只绑定 `127.0.0.1`；容器或局域网部署需要显式传入 `--host 0.0.0.0`，并在外层配置访问控制和 TLS。

## 5. Feishu

飞书接入需要安装 `feishu` extra，并配置 `.env.example` 中列出的应用凭据。`serve-meeting --feishu` 适合单场会议；`serve-console --feishu` 使用一个长连接服务数据库中的多场会议。

参会名单始终是授权边界。逐字稿中出现某个人的名字，不会自动授予该人访问会议任务的权限。
