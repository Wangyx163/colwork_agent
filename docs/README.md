# Documentation

这组文档面向项目评审者和希望复现工程链路的开发者。首页只保留理解和运行项目所需的信息，详细设计、评测口径和历史决策从这里进入。

## 推荐阅读顺序

1. [架构与责任边界](architecture.md)：模型、确定性系统和人的分工。
2. [运行指南](running.md)：内置演示、真实逐字稿、分进程 Worker、多会议和飞书接入。
3. [评测结果与复现](evaluation.md)：工作流 Gate、召回指标、Function Calling 对照及解释边界。
4. [已知限制](known-limitations.md)：哪些能力已验证，哪些仍只是原型或离线测试。
5. [AI 辅助开发说明](ai-assisted-development.md)：开发阶段使用的 AI 工具及职责。
6. [完整设计规范](design/00-README.md)：领域模型、状态机、接口契约和 ADR。

## 当前事实源

- 运行能力清单：[`capabilities.json`](../capabilities.json)
- 状态机：[`docs/design/07-state-machines.md`](design/07-state-machines.md)
- 测试：[`tests/`](../tests/)
- CI：[`/.github/workflows/tests.yml`](../.github/workflows/tests.yml)

`docs/design/` 中包含历史决策和阶段性计划。历史文档用于解释设计演进，不应覆盖代码、测试、`capabilities.json` 和已接受 ADR 所表达的当前事实。
