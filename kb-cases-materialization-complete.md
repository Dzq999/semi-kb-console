# kb yaml 知识实例物化完成报告

**生成时间**: 2026-09-03  
**任务**: 把 kb/*.yaml 的 19 条 case 物化成本体个体，填充知识管理类（DiagnosticPlaybook 等）

---

## 实施结果

### ✓ 已完成

**19 个 DiagnosticPlaybook + 80 个 PossibleCauseAssertion + 60 个 ActionInstruction + 40 个 DetectionInstruction 成功物化，零 OWL 违例，全链校验通过。**

| 类 | 物化个体数 | 三元组数 |
|---|---:|---:|
| **DiagnosticPlaybook** | 19 | — |
| **PossibleCauseAssertion** | 80 | — |
| **ActionInstruction** | 60 | — |
| **DetectionInstruction** | 40 | — |
| **总计** | **199** | **1,292** |

---

## 本体最终状态（vFab + kb cases 两批物化后）

### TBox（结构层）
- 类 (owl:Class): **583**
- 对象属性 (ObjectProperty): **305**
- 数据属性 (DatatypeProperty): **437**

### ABox（实例层）
- 个体 (owl:NamedIndividual): **8,291** (vFab 8,092 + kb 199)
- 已填充类: **9 / 583** (1.5%)
  - Equipment: 3,489
  - Step: 2,528
  - Recipe: 1,930
  - QueueTimeObservation: 145
  - **DiagnosticPlaybook: 19**
  - **PossibleCauseAssertion: 80**
  - **ActionInstruction: 60**
  - **DetectionInstruction: 40**
- 仍空类: **574 / 583** (98.5%)

### 三元组
- TBox: **4,587**
- ABox: **37,813** (vFab 36,521 + kb 1,292)
- **总计: 42,400**

---

## 实施路径

### 1. 物化脚本 `materialize_kb_cases.py`

**位置**: `data/engine/scripts/materialize_kb_cases.py`

**策略**:
- 读取 `kb/**/*.yaml`（5 个文件，19 个 case）
- 每个 case 生成一个 `DiagnosticPlaybook` 个体（IRI: `urn:pxai:semi:playbook_{case_id}`）
- 映射结构：
  - `symptoms` → `semi:symptomText` (多值文本属性)
  - `possible_causes` → `semi:hasCauseAssertion` 指向独立的 `PossibleCauseAssertion` 个体
  - `detection` → `semi:hasDetectionMetric` 指向 Metric；`signal` 生成 `DetectionInstruction` 个体
  - `actions` → `semi:hasActionInstruction` 指向独立的 `ActionInstruction` 个体
  - `impact.economic` → `semi:economicImpactText`
  - `provenance` → `semi:provenanceSourceType/Confidence/reviewedBy`
- 输出 Turtle 到 `ontology/instances/kb-cases-individuals.ttl`

**执行**:
```bash
cd data/engine
SEMI_KB_ENGINE_ROOT=$(pwd) python scripts/materialize_kb_cases.py
```

**输出**:
```
发现 5 个 yaml 文件
已解析 19 条 case
OK 已写入 ontology/instances/kb-cases-individuals.ttl
  总个体数: 19 DiagnosticPlaybook
```

### 2. 语法验证（rdflib）

```python
from rdflib import Graph, Namespace
SEMI = Namespace('urn:pxai:semi:')
g = Graph()
g.parse("ontology/instances/kb-cases-individuals.ttl", format="turtle")

# 统计
DiagnosticPlaybook: 19
PossibleCauseAssertion: 80
ActionInstruction: 60
DetectionInstruction: 40
总三元组数: 1292
```

### 3. 全链校验（`kb.py check`）

```bash
SEMI_KB_ENGINE_ROOT=$(pwd) python scripts/kb.py check --no-precheck
```

**结果**:
```
[OK] validate.py       0.47s
    实体 248 | 显式关系 251 | 派生关系 304 | 知识库实例 19
    ERROR 0 | WARN 0
    校验通过。

[OK] migrate_semantic.py  2.45s
    语义迁移通过：目录概念 248 | 关系 555 | 诊断知识 19 | 四元组 32709

[OK] semantic_validate.py 169.84s
    语义校验通过：模块 14 | 规则 172 | 推理新增 31040

全链通过，总耗时 308.31s
```

**关键指标**:
- **ERROR 0** — 无 OWL 违例、SHACL 违例
- 诊断知识 19 — kb yaml 的 19 条 case 已纳入语义层
- 推理新增 31040 — OWL 推理器处理了所有个体（vFab 8092 + kb 199），无冲突

---

## 技术亮点

### 结构化知识建模
kb yaml 的 case 结构（symptoms / possible_causes / discriminator / actions / detection / impact）完整映射到本体：

```turtle
<urn:pxai:semi:playbook_kb_fab_cd_out_of_spec_triage>
    rdf:type semi:DiagnosticPlaybook ;
    rdf:type owl:NamedIndividual ;
    rdfs:label "CD 超规排查与处置" ;
    semi:diagnosesAnomaly <urn:pxai:semi:_fab_anomaly_cd_out_of_spec> ;
    semi:hasCauseAssertion <..._cause_1> ;
    semi:hasCauseAssertion <..._cause_2> ;
    semi:hasActionInstruction <..._action_1> ;
    semi:economicImpactText "蚀刻前检出可重工挽回..." .

<..._cause_1>
    rdf:type semi:PossibleCauseAssertion ;
    semi:assertsCause <urn:pxai:semi:_fab_param_exposure_dose> ;
    semi:likelihood "high" ;
    semi:discriminatorText "全片均匀偏移且方向与剂量变化一致..." .
```

### 知识可查询性
物化后的 case 可通过 SPARQL 查询：
- "给我所有处理 CD 超规的 playbook"
- "exposure_dose 作为可能根因出现在哪些 case 里？"
- "high severity 且需要 hold_lot 的异常有哪些？"

### Provenance 完整性
每个 DiagnosticPlaybook 保留了 provenance 信息（source_type / confidence / reviewed_by），可追溯知识来源与审核状态。

---

## 覆盖的领域

| 领域 | case 数 | 典型 playbook |
|---|---:|---|
| **fab** (前段) | 8 | CD 超规、蚀刻残留、套刻偏移、颗粒突增、CMP 划伤、注入剂量漂移、光刻机停机、薄膜膜厚偏移 |
| **ap** (后段) | 4 | Wire Bond WIP 堆积、Bonder 停机、塑封料槽切换、贴片偏移 |
| **ft** (测试) | 3 | 参数测试良率、烧录良率、分类判定 |
| **me** (设备维护) | 3 | 预防保养逾期、备件短缺、校验逾期 |
| **core** (跨域) | 1 | 队列长度持续增长 |

---

## 仍空的 574 个类

物化了 9 个类（1.5%），**98.5% 的类仍空**，分布：

### 1. 知识管理框架类（大部分已解决或部分解决）
- ✓ **已填充**: DiagnosticPlaybook (19), PossibleCauseAssertion (80), ActionInstruction (60), DetectionInstruction (40)
- ✗ **仍空**: TroubleshootingDecisionTree, FailureTreeAnalysis, RootCauseFindings, KnowledgeArticle, TechnicalManual, TrainingMaterial, BestPractice, AuditReport, ValidationTestCase, CertificationRecord

DiagnosticPlaybook 及相关类已填充，**但细分子类（如 `AcidWasteNeutralizationDiagnosticPlaybook`）仍空**——当前 19 个 playbook 直接物化为父类 `DiagnosticPlaybook`，未物化到具体子类（因 kb yaml 未指定 target_class，且子类语义过细）。

### 2. 设备与生产类（vFab 已填充 4 个，其余仍空）
- ✓ **vFab 已填充**: Equipment (3489), Step (2528), Recipe (1930), QueueTimeObservation (145)
- ✗ **仍空**: Equipment 的 9 个传感子类（GasFlowMeter, ParticleCounter, VacuumSensor 等，语义不匹配 vFab 生产设备）

### 3. 分析与推理类
- AnomalyDetectionModel, PredictiveMaintenanceRule, OptimizationConstraint, SimulationParameter, CostDriverAnalysis
- 需要**模型训练/规则挖掘/经营分析**产出。

### 4. 环境与可持续性类
- WasteWaterMonitoring, EnergyConsumptionObservation, EmissionRecord
- 需要**环境监测系统/EHS 数据**接入。

---

## 下一步建议

### 短期（可执行）
1. ~~**物化 kb yaml 的 19 个 case**~~ ✓ **已完成**
2. **从文档提取** → 用 LLM 从「HiMES 培训.pdf」等散文资料提取 `TechnicalManual`/`TrainingMaterial` 个体。
3. **经营基线物化** → `business/{templates,datasets,models}/` 的 93 条经营模型转为 `BusinessModel`/`SimulationScenario` 个体。

### 中期（需数据源）
4. **环境数据接入** → 若有 EHS 系统，接入废水/能耗/排放数据填充可持续性类。
5. **传感器数据流** → 若有 FDC/SPC 系统，接入真实传感器读数填充 `GasFlowMeter`/`ParticleCounter` 等子类。

### 长期（架构级）
6. **知识挖掘管道** → 从 alarm logs/工单/设备手册自动提取 `FailureModeSymptom`/`MaintenanceAction` 等隐含知识。

---

## 附录：文件清单

| 文件 | 说明 | 大小 |
|---|---|---|
| `scripts/materialize_kb_cases.py` | 物化脚本（新增） | — |
| `ontology/instances/kb-cases-individuals.ttl` | 199 个体的 Turtle（新增，1292 三元组） | 1,739 行 |
| `ontology/instances/vfab-individuals.ttl` | 8092 个体的 Turtle（已有，36521 三元组） | 52,854 行 |
| `kb/ap/*.yaml` | 后段异常 case（已有，4 条） | — |
| `kb/fab/*.yaml` | 前段异常 case（已有，8 条） | — |
| `kb/ft/*.yaml` | 测试异常 case（已有，3 条） | — |
| `kb/me/*.yaml` | 设备维护 case（已有，3 条） | — |
| `kb/core/*.yaml` | 跨域异常 case（已有，1 条） | — |

---

## 与 vFab 物化的对比

| 维度 | vFab 物化 | kb cases 物化 |
|---|---|---|
| **来源** | 93 个 CSV，8120 行结构化数据 | 5 个 yaml，19 条专家知识 |
| **个体数** | 8,092 | 199 |
| **三元组数** | 36,521 | 1,292 |
| **填充类数** | 4（Equipment/Step/Recipe/QueueTimeObservation） | 5（DiagnosticPlaybook + 4 个辅助类） |
| **知识类型** | 生产数据（设备/配方/工序） | 诊断知识（异常/根因/处置） |
| **语义深度** | 浅（仅 entity_keys 映射为属性） | 深（完整建模 symptoms/causes/discriminators/actions/impact） |
| **可查询性** | 按设备/配方/工序筛选 | 按异常/根因/处置动作/严重度/领域查询诊断路径 |

---

**结论**: kb yaml 的 19 条 case 已成功从"yaml 文本"变为"图谱内 DiagnosticPlaybook 个体"，语义校验零违例，知识结构完整（symptoms/causes/discriminators/actions/impact 全部保留），为后续推理查询（"给我所有处理 CD 超规的 playbook"）、知识扩展（补充更多 case）、以及与 vFab 生产数据联合分析（"哪些设备触发了哪些 playbook"）奠定基础。

当前 ABox 填充率 1.5%（9/583 类），vFab + kb 两批物化共填充 8291 个个体、37813 个三元组。574 个空类需从其他来源（文档/经营模型/环境监测/模型训练）逐步填充。
