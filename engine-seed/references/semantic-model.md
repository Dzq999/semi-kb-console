# 语义模型

本体模块位于 `ontology/modules/`，稳定命名空间为 `urn:pxai:semi:`。

| 构成 | 表达 |
|---|---|
| 类 | `owl:Class`，覆盖 Fab、Facility、Equipment、Chamber、Lot、Wafer、Process、质量、维护、经营与场景 |
| 实例 | 真实设备、批次、事件和观测进入 ABox；旧 YAML 类型目录进入 `CatalogConcept` |
| 数据属性 | 编码、时间、数值、单位和状态等 `owl:DatatypeProperty` |
| 对象关系 | 归属、加工、量测、因果、处置、路径和影响等 `owl:ObjectProperty` |
| 公理与约束 | OWL 基数、逆属性、传递性、属性链；闭世界要求用 SHACL |
| 推理规则 | OWL-RL 与 `ontology/rules/`；规则结论保留规则与输入来源 |

一对多通过方向化基数表达：一台 `Equipment` 可有多个 `Chamber`，每个 `Chamber` 的 `chamberOf` 恰好一个 `Equipment`。历史状态用多值事件，当前状态可为函数属性。

语义变更集中的对象属性限制使用 `target_class`；数据属性限制使用 `target_datatype`。唯一编码可用函数型数据属性或最大基数 1 表达，格式、必填和跨字段约束继续由 SHACL 校验。

根因规则只能生成 `RootCauseHypothesis`，不得自动生成确认根因。定量结果由经营模型与仿真负责，不塞入 OWL 推理。
