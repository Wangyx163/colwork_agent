# N01｜会议任务提取、复核与发布

版本：2.0  
变更依据：ADR-028、ADR-030、ADR-031、ADR-035

## P0 职责

### N01-001｜生成待复核任务

- Episode 激活输入必须同时包含会议转写与显式参会名单；参会名单建立 PARTICIPANT 权限边界，不从逐字稿说话人或模型结果推测。
- COORDINATOR 若也在显式参会名单中，同一 actor 同时获得本 Episode 的 COORDINATOR 与 PARTICIPANT membership，可被派发并按参与者规则回应、提交本人任务；系统不得复制用户，也不得仅因其是协调人就从参会名单静默删除。
- 同一会议重新载入时参会名单必须与首次导入一致；增删参会者需作为后续显式变更能力设计，P0 不静默修改既有 Episode 权限。
- Episode 激活后，使用固定 fixture 或统一抽取接口读取会议转写。
- 每项结果至少含 title/deliverable_key/source_span，并显式返回 unresolved_fields；suggested_owner 与 suggested_deadline 只作提取候选，不得填造。
- 发言人身份不得自动成为 suggested_owner；只有同一证据发言中出现明确第三人称指派，或本人明确“我来/我会/我负责/我去做”等承诺时才保留 owner hint，否则系统确定性置空并追加待确认原因。
- P0 协作采用“单负责人 + 会议明确协作者”的最小规则：只有会议组织人的同一条任务引文同时出现具体协作者姓名和“一起/配合/协作/合作”等明确指派时，才写入 `collaborator_names`；普通参会者建议、参会、讨论、群内查看、投票和“大家”泛称不得推导协作者。缺少组织人原文支持时系统确定性清空该字段并转为 `SOLO`。
- 会议中已当场完成的操作、纯建议、假设和头脑风暴方向不是会后 ActionItem；Prompt 先排除，遗漏项仍由 COORDINATOR 在发布 Gate 忽略/合并，模型不得绕过复核。
- source_span 必须绑定逐字稿中的原始时间戳和逐字引文；“行/OK/可以”等短确认不能单独证明任务内容，优先引用直接布置行动或明确接受完整任务的发言。
- 引文在全文只出现一次但模型时间戳错误时，系统按唯一逐字匹配确定性纠正时间戳并记录 repair；模型合并相邻发言时，仅在全文存在唯一、足够长且覆盖率达标的连续原文锚点时回绑到该单行并截取原文。零处、多处歧义或短公共片段不得猜测。
- 引文不能对齐时，允许一次受约束的证据修复调用：只能修改 source_timestamp/source_quote/uncertainties 或删除无单行证据的候选，不得新增候选或改变 title、deliverable、负责人、协作者、截止时间、置信度和确认状态；修复后仍须重新执行原文校验。
- 二次不能对齐固定报 `INVALID_CITATION` 并停止导入；语义字段被修复调用改变固定报 `INVALID_SCHEMA`，不得降低原文证据门槛。
- 模型 JSON 中仅涉及空负责人/空截止与 `needs_confirmation=false` 的矛盾，或 owner/deadline 可空字段缺失、空字符串、`uncertainties` 非字符串数组时，系统执行 fail-closed 结构归一化：空负责人/空截止一律强制待人工确认并追加原因，记录 `normalization_actions/count`，不追加模型调用。title、deliverable、confidence、source_timestamp/source_quote 不得由系统编造，缺失或非法时仍按契约失败。
- Episode 创建前的抽取尚无 Outbox，`NETWORK_TIMEOUT/RATE_LIMIT/PROVIDER_5XX` 在同一逻辑调用内最多传输 3 次并短退避；记录 `model_call_count` 与 `transport_attempt_count`，传输重试不得改变 Prompt、输入哈希或候选语义。
- 长逐字稿按完整发言轮次切分为受限字符片段并保留少量相邻轮次重叠；每个片段只抽取本片段有证据的行动，汇总后用完整逐字稿统一做证据对齐和去重。记录 `chunk_count`，分块不得拆断单条发言或改变整份 transcript input_hash。
- 每个成功片段按 `transcript_hash + meeting_date + prompt_version + model + chunk_index/count + chunk_hash` 原子写检查点；恢复时重新校验检查点 schema 和整份原文证据，只重跑缺失/无效片段，并记录 `checkpoint_hit_count`。检查点不得创建 Episode、ActionItem 或另一套流程状态。
- 结果直接创建 `ActionItem(status=PENDING_CONFIRMATION, published_sim_time=NULL)`，不创建 ActionItemCandidate。
- 同一 identity_key 重复出现时复用已有 ActionItem。
- 验证：AUTO-N01-001 至 AUTO-N01-003。

### N01-002｜COORDINATOR 复核

- 只有可信 Principal 的 COORDINATOR 可修改任务解释、work_requirements、management_review_policy、team_required_by，或执行 merge/ignore/publish。
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
