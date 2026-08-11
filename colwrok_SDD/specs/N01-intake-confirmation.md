# N01｜会议任务提取、复核与发布

版本：2.1
变更依据：ADR-028、ADR-030、ADR-031、ADR-035、ADR-036

## P0 职责

### N01-001｜生成待复核任务

- Episode 激活输入必须同时包含会议转写与显式参会名单；参会名单建立 PARTICIPANT 权限边界，不从逐字稿说话人或模型结果推测。
- COORDINATOR 若也在显式参会名单中，同一 actor 同时获得本 Episode 的 COORDINATOR 与 PARTICIPANT membership，可被派发并按参与者规则回应、提交本人任务；系统不得复制用户，也不得仅因其是协调人就从参会名单静默删除。
- 同一会议重新载入时参会名单必须与首次导入一致；增删参会者需作为后续显式变更能力设计，P0 不静默修改既有 Episode 权限。
- Episode 激活后，使用固定 fixture 或统一抽取接口读取会议转写。
- 抽取内部区分候选发现与结构整理责任，但只向 COORDINATOR 提供一个复核表面，不建立第二套业务流程。候选发现以稳定 `unit_id` 扫描全文片段：一套统一宽召回 Prompt 与确定性词法/结构规则分别产生候选后取并集；不得按模型 confidence、字段完整度或评测口径设置硬淘汰阈值。
- 模型召回片段采用 `left_context + emit_zone + right_context` 重叠窗口。模型可读取整个窗口，Prompt 要求 anchor 位于 emit zone；每个 unit 必须恰好一次属于某个 emit zone。模型若违规输出本窗可见 context 的合法 anchor，系统以恢复标记保留并在全局按同 anchor/type 与责任窗口候选合并，不得当作未知 anchor 丢弃或计作责任窗口覆盖。`emit-owned-char-window.v2` 使用 `800 + 1600 + 800` 字符预算，窗口首尾可重分配空余预算，所有边界必须落在完整 unit 之间。多步骤投票/审批的完整性由后续跨窗口 event/thread 关联承担，不得通过无限放大发现窗口替代流程建模。
- 候选发现只返回 anchor unit、解释 anchor 必需的直接确认/指代 support、类型提示与触发来源；原文、时间戳和字符范围由系统按 unit 确定性生成。support 不承担同话题整段证据打包，系统按版本化字符预算裁剪未知或过宽 support，且不得因 support 无效、owner、deadline 或 deliverable 未知而放弃合法 anchor 候选。模型不得自由改写 `source_quote`。
- 结构整理先判断上下文是否充分：TASK 至少有“动作 + 对象或结果”；COMMITMENT/DECISION 至少有“未来事件或决定内容 + 明确接受、时间或条件之一”。owner 和 deadline 不属于可抽取性的必需条件。动作或决定信号必须与 anchor 绑定；上下文可补对象、结果、时间、条件或独立确认，但不能把无关邻句的动作借给仅含指派词的 anchor。`选择/通过/同意/就按/采用/改成/不再` 等弱决策信号缺少第二证据时只进入 `review_hint`。
- 发现窗口负责有界局部语境，不能限制证据整理。只有一个独立语义事实，或出现短确认、指代、省略、单独时间等不自足表达时，证据整理必须回到全局逐字稿：先加入前后各一个 unit，仍不足则沿同一话题继续扩展；短确认/指代优先向前，时间/结果补充同时检查前后。每轮扩展后重新判断，直到语义充分、遇到话题边界或达到版本化字符预算。话题边界只提示停止，字符预算只作安全上限；达到预算仍不足不得删除。
- 抽取 artifact 至少包含 `raw_candidates`、`draft_items`、`review_hints`、`coverage` 与逐候选校验/降级记录。语义充分的 `draft_items` 至少含 title/deliverable_key/source_span/unresolved_fields；suggested_owner 与 suggested_deadline 只作提示，不得填造。仍不充分的候选进入 `review_hints`，保留 evidence/context units、触发来源和不足原因。
- 发言人身份不得自动成为 suggested_owner；只有同一证据发言中出现明确第三人称指派，或本人明确“我来/我会/我负责/我去做”等承诺时才保留 owner hint，否则系统确定性置空并追加待确认原因。
- P0 协作采用“单负责人 + 会议明确协作者”的最小规则：只有会议组织人的同一条任务引文同时出现具体协作者姓名和“一起/配合/协作/合作”等明确指派时，才写入 `collaborator_names`；普通参会者建议、参会、讨论、群内查看、投票和“大家”泛称不得推导协作者。缺少组织人原文支持时系统确定性清空该字段并转为 `SOLO`。
- 会议中已当场完成的操作、纯背景、假设和头脑风暴方向不得自动成为 ActionItem；但无法可靠判断是否已落定或是否仍需会后处理时，必须保留为 `review_hint`，由 COORDINATOR 决定添加任务或忽略。
- source_span 必须由一个或多个 evidence unit 确定性生成；“行/OK/可以”等短确认不能独立成为候选 anchor 或任务卡片。找到被确认内容时以前序动作/决定为 anchor、确认句为 support；找不到时只保留为 `review_hint`。
- 校验和修复以单个候选为隔离边界。无效 anchor、缺少必要结构或单条模型 JSON 损坏时，只隔离该候选并记录原因。确定性 draft 充分性通过时，结构模型失败或漏项保留可追溯的 fallback draft；充分性未通过时进入 `review_hint`，模型不得强行升级。不得因一条候选失败删除同批其他候选、停止整场导入或把会议解释为空结果。
- owner/deadline 为空时一律设置 unresolved；title/deliverable 无法在证据中形成最小语义时不得由系统编造，候选转为 `review_hint` 而不是失败关闭。
- Episode 创建前的抽取尚无 Outbox，`NETWORK_TIMEOUT/RATE_LIMIT/PROVIDER_5XX` 在同一逻辑调用内最多传输 3 次并短退避；记录 `model_call_count` 与 `transport_attempt_count`，传输重试不得改变 Prompt、输入哈希或候选语义。
- 长逐字稿按上述窗口生成器处理，coverage ledger 分别记录每个 unit 的 emit-zone 责任窗口和 context 出现窗口。模型责任窗口失败时仍运行确定性规则补网并标记 `DEGRADED`，不得将未覆盖 emit zone 当作无候选。
- provider 窗口响应按数据哈希、unitization/window/prompt/model 版本与窗口 hash 原子写检查点；meeting/stage 结果还必须包含候选归一化、rule 与 evidence 策略版本。恢复时只重跑缺失或无效阶段，不得在 Prompt 或任一下游策略改变后复用过期结果。检查点和抽取 artifact 不创建 Episode、ActionItem 或另一套流程状态。
- 只有 `draft_items` 直接创建 `ActionItem(status=PENDING_CONFIRMATION, published_sim_time=NULL)`；`review_hints` 留在原始抽取 artifact，由 COORDINATOR 添加为任务后才创建 ActionItem。两者都不建立 ActionItemCandidate 领域实体。
- 自动去重只允许合并完全相同、证据相同的重放结果；其他相似项只提供分组建议，最终 merge/ignore 由 COORDINATOR 决定。同一 identity_key 的幂等重载仍复用已有 ActionItem。
- 验证：AUTO-N01-001 至 AUTO-N01-003。

### N01-002｜COORDINATOR 复核

- 只有可信 Principal 的 COORDINATOR 可修改任务解释、work_requirements、management_review_policy、team_required_by，或执行 merge/ignore/publish。
- 同一复核表面同时展示 `draft_items` 与 `review_hints`。COORDINATOR 可从 hint 添加任务、完全手工添加任务或忽略 hint；添加时记录原 hint/evidence unit、人工填写字段和操作者，且不得覆盖原抽取 artifact。
- 抽取后、Episode 激活前若协调人已显式确认清洗结果，必须保留原始抽取 artifact，并在 reviewed artifact 与 EpisodeActivated audit 中记录复核人、权限依据、参会名单、已确认别名和删除/拆分/合并决策；不得覆盖模型原始输出。
- source_span 与原始提取证据不可覆盖；合并只追加来源并将被合并项标记 REJECTED。
- 字段不完整时不得发布；模型置信度不替代人工复核。
- 验证：AUTO-N01-004 至 AUTO-N01-006。

### N01-003｜版本化多人派发与回应

- COORDINATOR 发布时必须选择恰好一名主负责人和零到多名协作者；所有被派发人都必须是本 Episode 的显式 PARTICIPANT，主负责人不得同时作为协作者。
- 首次派发设置 `published_sim_time` 并为当前 `definition_version` 创建逐人 `ActionItemAssignment`，ActionItem 进入 `PENDING_ASSIGNMENT`；不再进入公开认领池。
- 被派发人只能对当前版本执行 `ACCEPT | RETURN_FOR_REVISION` 并可附带一条留言，不能修改标题、交付物、工作要求、验收规则或团队日期。
- 全部派发人接受后，系统原子写入主负责人 `owner_actor_id`、创建首个 ACTIVE CommitmentRevision、激活协作者并进入 TRACKING；不建立共同 owner。
- 任一派发人退回时，ActionItem 进入 `NEEDS_REVISION`，同一版本其余 PENDING/ACCEPTED assignment 全部进入 SUPERSEDED。COORDINATOR 修改并重派时 `definition_version + 1`，所有成员必须重新回应。
- suggested_owner 与会议明确 collaborator 只作为 COORDINATOR 的默认选择，不自动形成授权事实。
- 重复 message_id 幂等；相同任务版本不得对同一 actor 重复创建 assignment。
- 验证：AUTO-N01-007 至 AUTO-N01-012。

### N01-004｜忽略与未决责任

- COORDINATOR 可在首次派发前忽略误提取项，原始证据和审计仍保留；已经派发的任务不能忽略，只能等待回应或在退回后修改重派。
- 系统不得自动加入或指派未参会者。`RETURN_FOR_REVISION` 表示当前任务定义需要负责人修改，不是对个人能力或合作意愿的评价。
- 验证：AUTO-N01-011、AUTO-N01-012。

## P1｜协作结构识别

- SYSTEM 可从会议组织人的同一组连续原文中提出白名单结构：`PARALLEL_COLLECTION | SEQUENTIAL_HANDOFF | COLLECTIVE_DECISION`，并分别记录参与者范围、汇总/接收人、顺序词、最终交付和 source spans。
- “每个人/大家”只能在 COORDINATOR 复核后按本 Episode 显式参会名单展开；简称或别名未确认时不得映射 Actor。
- 结构建议不得直接创建运行状态；COORDINATOR 确认后由确定性展开器生成基础 ActionItem 及 N02 定义的关系。
- P0 遇到此类语句仍只产生可独立复核的基础任务，不自动生成依赖、投票或阶段 Gate。
