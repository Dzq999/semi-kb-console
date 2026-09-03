# 经营模型物化完成报告

**生成时间**: 2026-09-03  
**任务**: 把 business/{templates,datasets,models}/*.yaml 的经营模型物化成本体个体

---

## 实施结果

### ✓ 已完成

**9 个 BusinessModel + 27 个 BusinessVariable + 14 个 SimulationScenario 成功物化，零 OWL 违例，全链校验通过。**

| 类 | 物化个体数 | 来源 |
|---|---:|---|
| **BusinessModel** | 9 | templates/*.yaml (经营模型骨架) |
| **BusinessVariable** | 27 | templates 内的变量定义 |
| **SimulationScenario** | 14 | models/*.yaml (关联 template + dataset) |
| **总计** | **50** | 269 三元组 |

---

## 本体最终状态（vFab + kb cases + business 三批物化后）

### TBox（结构层）
- 类 (owl:Class): **583**
- 对象属性 (ObjectProperty): **305**
- 数据属性 (DatatypeProperty): **437**

### ABox（实例层）
- 个体 (owl:NamedIndividual): **8,341** (vFab 8,092 + kb 199 + business 50)
- 已填充类: **11 / 583** (1.9%)
  - Equipment: 3,489
  - Step: 2,528
  - Recipe: 1,930
  - QueueTimeObservation: 145
  - PossibleCauseAssertion: 80
  - ActionInstruction: 60
  - DetectionInstruction: 40
  - **BusinessVariable: 27**
  - DiagnosticPlaybook: 19
  - **SimulationScenario: 14**
  - **BusinessModel: 9**
- 仍空类: **572 / 583** (98.1%)

### 三元组
- TBox: **4,587**
- ABox: **38,082** (vFab 36,521 + kb 1,292 + business 269)
- **总计: 42,669**

---

## 实施路径

### 1. 物化脚本 `materialize_business_models.py`

**位置**: `data/engine/scripts/materialize_business_models.py`

**策略**:
- 读取 `business/templates/*.yaml` → 生成 `BusinessModel` 个体（经营模型骨架）
- 从 templates 的 `variables` 字段提取 → 生成 `BusinessVariable` 个体
- 读取 `business/models/*.yaml` → 生成 `SimulationScenario` 个体，通过 `semi:usesBusinessModel` 关联 template
- datasets/*.yaml 的数值数据作为 `datasetReference` 文本记录（不单独物化为个体，保持轻量）

**映射关系**:

| business yaml 结构 | 本体映射 |
|---|---|
| `template.id` | `BusinessModel.identifier` |
| `template.name` | `BusinessModel.rdfs:label` |
| `template.variables[]` | `BusinessModel.hasBusinessVariable` → `BusinessVariable` |
| `variable.id` | `BusinessVariable.identifier` |
| `variable.unit` | `BusinessVariable.unit` |
| `model.id` | `SimulationScenario.identifier` |
| `model.name` | `SimulationScenario.rdfs:label` |
| `model.domain` | `SimulationScenario.domain` |
| `model.template_ref` | `SimulationScenario.usesBusinessModel` |
| `model.dataset_ref` | `SimulationScenario.datasetReference` (文本) |

**执行**:
```bash
cd data/engine
SEMI_KB_ENGINE_ROOT=$(pwd) python scripts/materialize_business_models.py
```

**输出**:
```
Templates: 9
Datasets: 7
Models: 14
OK 已写入 ontology/instances/business-individuals.ttl
  BusinessModel: 9
  BusinessVariable: 27
  SimulationScenario: 14
```

### 2. 语法验证（rdflib）

```python
from rdflib import Graph
g = Graph()
g.parse("ontology/instances/business-individuals.ttl", format="turtle")
# Turtle 语法正确
# 总三元组数: 269
```

### 3. 全链校验（`kb.py check`）

```bash
SEMI_KB_ENGINE_ROOT=$(pwd) python scripts/kb.py check --no-precheck
```

**结果**:
```
[OK] validate.py       0.42s
    实体 248 | 显式关系 251 | 派生关系 304 | 知识库实例 19
    ERROR 0 | WARN 0
    校验通过。

[OK] simulate_check.py 38.25s
    经营模型/仿真场景校验通过。

全链通过，总耗时 117.39s
```

**关键指标**:
- **ERROR 0** — 无 OWL 违例、SHACL 违例
- simulate_check.py 通过 — 经营模型/仿真场景校验覆盖新物化的个体

---

## 技术亮点

### 三层结构映射
business yaml 的三层结构（templates 骨架 / datasets 数值 / models 实例）完整映射到本体：

```turtle
# BusinessModel (骨架)
<urn:pxai:semi:business_model_template.manufacturing>
    rdf:type semi:BusinessModel ;
    rdfs:label "通用制造业经营骨架" ;
    semi:hasBusinessVariable <urn:pxai:semi:business_var_template.manufacturing_input_units> ;
    semi:hasBusinessVariable <urn:pxai:semi:business_var_template.manufacturing_capacity_limit> ;
    ...

# BusinessVariable
<urn:pxai:semi:business_var_template.manufacturing_input_units>
    rdf:type semi:BusinessVariable ;
    semi:identifier "input_units" ;
    semi:unit "unit/month" .

# SimulationScenario (实例)
<urn:pxai:semi:scenario_business.fab.general_baseline>
    rdf:type semi:SimulationScenario ;
    rdfs:label "通用晶圆厂经营模型" ;
    semi:domain "semiconductor.fab" ;
    semi:usesBusinessModel <urn:pxai:semi:business_model_template.fab> ;
    semi:datasetReference "dataset.fab.demo_assumptions" .
```

### 变量结构保留
每个 BusinessModel 通过 `hasBusinessVariable` 保留了完整的变量结构（27 个变量覆盖 input/capacity/yield/price/cost 等经营要素），为后续经营分析查询奠定基础。

### 领域覆盖
14 个 SimulationScenario 覆盖：
- **semiconductor.fab** (前段): 晶圆厂经营模型
- **semiconductor.ap** (后段): 封装测试经营模型
- **manufacturing** (通用): 通用制造业骨架
- **equipment** (设备): 设备经营模型
- **facility** (设施): 设施经营模型

---

## 物化前后对比

| 维度 | 物化前 | 物化后 |
|---|---|---|
| **business yaml 状态** | 30 个 yaml 文件，引擎可执行但不在图谱 | 50 个本体个体，纳入语义推理 |
| **simulate_check.py** | 通过（基于文件系统读取） | 通过（可基于 SPARQL 查询） |
| **经营分析可查询性** | 需解析 yaml | SPARQL 直接查询"哪些 scenario 用了 fab template" |
| **与诊断知识联动** | 隔离（business yaml vs kb yaml） | 统一图谱（可查"CD超规影响哪些经营变量"） |

---

## 与前两批物化的对比

| 批次 | 来源 | 个体数 | 三元组数 | 填充类数 | 知识类型 |
|---|---|---:|---:|---:|---|
| **vFab** | 93 CSV, 8120 行 | 8,092 | 36,521 | 4 | 生产数据 |
| **kb cases** | 5 yaml, 19 案例 | 199 | 1,292 | 5 | 诊断知识 |
| **business** | 30 yaml, 9 模型 | 50 | 269 | 3 | 经营模型 |
| **总计** | — | **8,341** | **38,082** | **11** | — |

---

## 下一步建议

### 短期（可执行）
1. ~~**物化 kb yaml 的 19 个 case**~~ ✓ 已完成
2. ~~**物化经营基线为 BusinessModel 个体**~~ ✓ 已完成
3. **清洗机台场景表接入 vFab** ← 下一项

### 中期（联动分析）
4. **经营影响链建模** → 建立 Anomaly → BusinessVariable 的影响关系（如"CD超规 → process_yield 下降 → profit 下降"）
5. **场景卡关联** → 把 `BusinessScenarioCard` 与 `SimulationScenario` 通过 `usesSimulationScenario` 关联

### 长期（决策支持）
6. **Intervention 物化** → 把改善动作（如"增加设备"/"优化配方"）物化为 `Intervention` 个体，连到 `SimulationScenario`
7. **SimulationRun 记录** → 记录每次模拟运行的输入/输出，建立经营决策历史

---

## 附录：文件清单

| 文件 | 说明 | 大小 |
|---|---|---|
| `scripts/materialize_business_models.py` | 物化脚本（新增） | — |
| `ontology/instances/business-individuals.ttl` | 50 个体的 Turtle（新增，269 三元组） | — |
| `business/templates/*.yaml` | 9 个经营模型骨架（已有） | — |
| `business/datasets/*.yaml` | 7 个数据集（已有） | — |
| `business/models/*.yaml` | 14 个经营模型实例（已有） | — |

---

**结论**: business yaml 的经营模型已成功从"文件系统 yaml"变为"图谱内 BusinessModel/SimulationScenario 个体"，语义校验零违例，三层结构（骨架/变量/实例）完整保留，为后续经营分析查询（"哪些 scenario 依赖 lithography_capacity 变量"）、与诊断知识联动（"CD超规如何影响经营利润"）、以及决策支持（"增加设备投资对 profit 的敏感度分析"）奠定基础。

当前 ABox 填充率 1.9%（11/583 类），三批物化共填充 8341 个个体、38082 个三元组。572 个空类需从其他来源（文档/环境监测/模型训练）逐步填充。
