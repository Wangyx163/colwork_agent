# Evaluation

项目将“必须正确的工程事实”和“用于理解效果的观察指标”分开：

- Gate：权限、状态、版本、审批、幂等和恢复；失败即工程不通过。
- Signal：召回、人工负担、Token和流程耗时；用于比较和定位，不单独证明产品有效。

## 离线工程回归

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
python -m collab_agent eval --fresh
python -m collab_agent eval-ai-p0 --fresh
```

默认回归不调用外部模型。GitHub Actions分别在SQLite和PostgreSQL上运行测试，避免只有开发后端满足领域契约。

## 行动项召回实验

固定样本来自AMC-A `except_TS_test1`：15场、7,398句、49条句级金标，其中9场有正例、6场零标注。

模型与规则并集的严格句级结果：

| 指标 | 结果 |
|---|---:|
| TP / FP / FN | 39 / 1,181 / 10 |
| Precision | 0.0320 |
| Recall | 0.7959 |
| F1 | 0.0615 |

召回链路还报告三个不同表面：

| 表面 | 覆盖 | 可以说明什么 |
|---|---:|---|
| raw anchor | 39/49，0.7959 | 候选锚点是否直接命中金标句 |
| explicit anchor + support | 46/49，0.9388 | 金标是否作为显式候选证据被引用 |
| routed draft/hint evidence | 49/49，1.0 | 金标信息是否进入负责人可复核证据 |

最后一个指标包含2,379个`LINKED_EVIDENCE_BRIDGE`引用，只能解释为证据可达率，不能冒充模型句级分类能力。

工作量同样必须报告：15场共产生1,049个raw candidate、251个draft和780个review hint，平均每场约16.7个draft和52个hint。当前结果证明召回链路不再大面积静默丢失信息，也同时证明复核负担尚未达到产品可用水平。

报告文件：`evaluation_runs/recall-selected-v23-pilot15-final-v2.json`。该目录默认不作为仓库交付物；需要复现时使用：

```powershell
python scripts/evaluate_recall_windows.py --help
```

## Function Calling对照

held-out 8场比较了两种引文定位方式：

| 方案 | 抽取量 | 真阳性 | F1 | 引文可定位率 | 硬失败 |
|---|---:|---:|---:|---:|---:|
| 一次抽取后代码校验 | 49 | 5 | 0.1538 | 1.0 | 0 |
| 模型自主调用只读搜索工具 | 94 | 3 | 0.0556 | 1.0 | 2 |

工具读取的仍是系统已经拥有的逐字稿，而`align_source_evidence`已经能够确定性定位引用。因此工具没有填补信息缺口，只增加了决策自由度和失败面。该实验支持的是一个局部结论：引文定位不应由模型自主调用工具；它不支持“Function Calling普遍无用”。

## 解释限制

- AMC-A是句级标注，不是本产品的任务卡或hint产品口径。
- “零标注会议”不能直接称为“没有行动项的会议”。
- 15场足以做工程定位，不足以宣称总体产品效果。
- 下一步需要由真实负责人将候选标为`DRAFT / HINT / HIDE`，同时标记重复任务和全文漏项。
