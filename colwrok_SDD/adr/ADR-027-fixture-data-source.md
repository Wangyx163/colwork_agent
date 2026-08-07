# ADR-027｜P0 Fixture 数据来源与止损

状态：ACCEPTED  
日期：2026-08-05

## 背景

P0 需要会议行动项、多人行为和交付版本来验证完整业务链。若从零编写大量会议文本会浪费时间；若把数据集下载、训练或完整基准复现纳入主链，又会挤占 10–15 日产品实现时间。

## 决策

1. 首选 AliMeeting4MUG / AMC-A 作为中文会议行动项句的素材来源，因为其许可公开，且已有行动项检测子集与官方基线。
2. 该数据只解决“哪些句子像会议行动项”；本项目仍对少量入选句补 owner、deliverable、deadline、required_fields 与 source_span 最小标注。
3. P0 只将样本导入 ContentPack fixture，不新增 Dataset、Corpus、AnnotationJob 或训练流水线等领域实体。
4. 上游访问、许可核验与格式检查总计最多投入 1 小时；若下载不稳定，立即使用同 schema 的项目自建 fixture，后续再替换素材。
5. 外部样本必须记录来源 URL、获取日期、许可、样本 ID 和本地标注变更；项目自建 fixture 必须明确标为 project_fixture。
6. 本决策不恢复 ADR-024 已删除的 AMC-A 200 片段测试床、统计泛化或论文式评测。

## 结果

- 可节省会议文本构造和行动项初筛时间，预计净节省约 0.5–1 个工作日。
- 无法省掉本项目特有的责任人、交付内容、截止时间与版本/升级行为标注。
- 外部站点不可用不会阻塞 P0，当前仓库可先以项目自建 fixture 完成 E2E。

## 参考

- AliMeeting4MUG：`https://modelscope.cn/datasets/modelscope/Alimeeting4MUG/su`
- MUG Challenge：`https://signalprocessingsociety.org/publications-resources/data-challenges/icassp2023-general-meeting-understanding-and-generation`
- Action Item Detection baseline：`https://github.com/alibaba-damo-academy/SpokenNLP/tree/main/action-item-detection`
