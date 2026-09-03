# vFab 真数据可物化清单

**生成时间**: 2026-09-03  
**数据源**: `data/engine/sources/internal/vfab/raw/*.csv` (93 文件, 8120 行)  
**本体现状**: 583 个类，**全部 0 实例** (owl:NamedIndividual)

---

## 执行摘要

vFab 的 8120 行真实 Aleris SEMI 生产数据，按 manifest 声明的 `target_class`，可物化为 **4 个本体类的实例**，填满 4 个现在空着的类：

| 本体类 | vFab 可物化行数 | 当前实例数 | 父类 | 子类数 |
|--------|---------------:|----------:|------|-------:|
| **Equipment** | 3,517 | 0 | PhysicalEntity | 9 |
| **Step** | 2,528 | 0 | InformationEntity | 0 |
| **Recipe** | 1,930 | 0 | InformationEntity | 0 |
| **QueueTimeObservation** | 145 | 0 | Observation | 0 |
| **合计** | **8,120** | **0** | — | **9** |

**关键发现**：
1. **本体是结构丰富、实例全空的纯 TBox**——583 类 0 实例，vFab 物化后能让 4 个类（0.7%）从 0 跃升到数千实例。
2. Equipment 有 9 个子类（GasFlowMeter/ParticleCounter/VacuumPumpPackage 等传感监测类），但 vFab 的 3517 行全是生产设备（TEL INDY+/Scanner/Wet Bench/Sorter 等 8 种），**不匹配子类语义**——直接物化到父类 Equipment。
3. Step/Recipe/QueueTimeObservation 无子类，直接物化。
4. 另外 579 个类（99.3%）仍将是空的——vFab 只覆盖半导体制造的设备/工艺/配方/队列观测，不覆盖诊断手册/故障树/根因分析工作流/废水中和监测等其他领域类。

---

## 详细分解

### 1. Equipment (3,517 行)

**本体定义**:
- IRI: `urn:pxai:semi:Equipment`
- 标签: (未设置 rdfs:label，IRI 即名)
- 父类: PhysicalEntity
- 子类: 9 个（传感监测类）
  - GasFlowMeter (气体流量计)
  - ParticleCounter (颗粒计数器)
  - PowerQualityMonitor (电能质量监测器)
  - ProbeEquipment (探针测试设备)
  - UPWQualitySensor (超纯水质量传感器)
  - UtilityMonitoringSensor (公用系统监测传感器)
  - VacuumPumpPackage (真空泵机组)
  - VibrationSensor (振动传感器)
  - WastewaterNeutralizationpHMonitor (废水中和pH监测器)

**vFab 数据**:
- 来源: 87 个 CSV (aleris/brooks/in_line_scanner_track/sorter/tel_indy/ultron/wet_bench + tool_list/vfab_5)
- 实体键: `equipment` + `phase`
- 设备分布 (8 种):

| 设备名 | 行数 | 场景类型 |
|--------|-----:|---------|
| TEL INDY+ | 817 | 光刻/刻蚀设备，normal/abnormal/startup/e90 |
| In-Line 机台（Scanner / Track） | 803 | 在线检测/传送，normal/abnormal(carrier_id/recipe/slotmap/pj/cj错误) |
| Wet Bench（Pinnacle 300） | 565 | 湿法清洗台，normal/abnormal/startup |
| Sorter（AAR 300） | 385 | 分选机，transfer/split/merge/startup |
| vfab5 流程与 Recipe 数据 | 333 | （tool_list/vfab_5 混入 Equipment，实为设备清单） |
| Aleris | 323 | 传送系统，carrier_id/recipe/slotmap 错误场景 |
| Brooks | 211 | 传送系统，类似 Aleris |
| Ultron | 80 | 设备，normal/startup |

**物化策略**:
- 全部物化到父类 `Equipment`（子类是传感监测器，语义不匹配生产设备）。
- 每行生成 1 个 `owl:NamedIndividual`，IRI 用 `entity_keys` 拼接（如 `urn:pxai:semi:equipment:TEL_INDY_PLUS:normal`）。
- 属性映射: `equipment` → `equipmentID` (DatatypeProperty)，`phase` → `operatingPhase`，其余列（state_control_model/buffer_control_model 等）→ 对应 DatatypeProperty（需在本体先声明 domain Equipment 的属性）。

---

### 2. Step (2,528 行)

**本体定义**:
- IRI: `urn:pxai:semi:Step`
- 父类: InformationEntity
- 子类: 0

**vFab 数据**:
- 来源: 3 个 CSV (22hk_process_flow / 28hk_process_flow / 28lk_process_flow)
- 实体键: `equipment` + `phase` + `stepsequence`
- 语义: 工艺流程步骤序列（22nm/28nm 高K金属栅工艺）

**物化策略**:
- 每行 1 个 `Step` 实例，IRI 用三键拼接（如 `urn:pxai:semi:step:22HK:litho:step001`）。
- 属性映射: `stepsequence` → `stepSequence`，`equipment`/`phase` → 关联 Equipment 实例（需 ObjectProperty `usesEquipment`）。

---

### 3. Recipe (1,930 行)

**本体定义**:
- IRI: `urn:pxai:semi:Recipe`
- 父类: InformationEntity
- 子类: 0

**vFab 数据**:
- 来源: 1 个 CSV (recipe_update.csv)
- 实体键: `equipment` + `phase` + `recipe`
- 语义: 配方更新记录（设备–阶段–配方名三元组）

**物化策略**:
- 每行 1 个 `Recipe` 实例，IRI 用三键拼接。
- 属性映射: `recipe` → `recipeName`，关联对应 Equipment。

---

### 4. QueueTimeObservation (145 行)

**本体定义**:
- IRI: `urn:pxai:semi:QueueTimeObservation`
- 父类: Observation
- 子类: 0

**vFab 数据**:
- 来源: 1 个 CSV (qt.csv)
- 实体键: `equipment` + `phase`
- 语义: 队列时间观测（等待时长数据）

**物化策略**:
- 每行 1 个 `QueueTimeObservation` 实例。
- 属性映射: 观测时长列 → `observedValue`（需 DatatypeProperty）。

---

## 物化后的本体状态预测

| 指标 | 物化前 | 物化后 | 变化 |
|------|-------:|-------:|------|
| 类总数 | 583 | 583 | 不变 |
| 有实例的类 | 0 | 4 | +4 |
| 实例总数 | 0 | 8,120 | +8,120 |
| 空类数 | 583 | 579 | -4 |
| 空类占比 | 100% | 99.3% | -0.7% |

**填满的 4 个类**占本体类总数的 0.7%，**但涵盖半导体制造的核心实体**（设备/工艺步骤/配方/观测），是 ABox 的基石。

---

## 实施路径（技术）

1. **新建脚本** `data/engine/scripts/materialize_vfab.py`（仿 `vfab_ingest.py` 结构）:
   - 读 `build/source/vfab-catalog.json`（已有 93 datasets）。
   - 按 `target_class` 分组，逐 CSV 读取。
   - 生成 Turtle 三元组:
     ```turtle
     @prefix semi: <urn:pxai:semi:> .
     @prefix owl: <http://www.w3.org/2002/07/owl#> .
     @prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
     
     semi:equipment_TEL_INDY_PLUS_normal a owl:NamedIndividual, semi:Equipment ;
         semi:equipmentID "TEL INDY+" ;
         semi:operatingPhase "normal" .
     ```
   - 写入 `ontology/instances/vfab-individuals.ttl`（新文件，不修改 modules/*.ttl）。

2. **属性声明补齐**（若 `equipmentID`/`operatingPhase` 等属性未在本体声明）:
   - 在 `ontology/modules/semiconductor-properties.ttl` 加 DatatypeProperty/ObjectProperty，指定 domain/range。

3. **校验**:
   - `kb.py check` 跑 OWL/SHACL，确保 8120 个体无违例。
   - `semantic_counts()` 应报告 `individuals: 8120`。

4. **UI 呈现**:
   - `/api/ontology/entities` 端点已支持按 `kind=individual` 筛选，物化后前端「本体实体」面板会列出 8120 个体（需分页）。

---

## 不能填的类（样本）

以下是本体中**不在 vFab 覆盖范围**的类（仍将是空的），按语义分组：

**诊断与分析**:
- AIEnabledRootCauseAnalysisWorkflow (AI辅助根因分析工作流)
- AssemblyPackagingFailureInvestigation (组装封装失效分析)
- AuditReadyUnifiedCausalFaultTree (审计就绪统一因果故障树)
- DiagnosticPlaybook (诊断手册)

**故障与成因**:
- AbatementBurnerFlameoutFaultMode (废气燃烧器熄火故障模式)
- AbatementScrubberScalingCause (洗涤塔结垢堵塞成因)
- BearingDegradationCause (轴承退化成因)

**废水与环境**:
- AcidWasteNeutralizationDiagnosticPlaybook (酸性废水中和pH漂移诊断手册)
- AcidWasteNeutralizationpHExcursionAnomaly (酸性废水中和pH漂移异常)
- WastewaterNeutralizationpHMonitor (废水中和pH监测器，是 Equipment 子类但 vFab 无此传感器数据)

**异常与验证**:
- Anomaly (异常)
- AnomalyCoverageValidationMetric (异常覆盖率验证指标)
- AnomalyToBusinessImpactLink (异常至经营影响链路)

**空间与工作流**:
- Area / Bay / AggregateWorkArea (区域/工作区)
- Action / ActionInstruction (动作/指令)
- AlarmEvent (告警事件)

vFab 是**生产执行层数据**（设备运行/工艺步骤/配方/队列时长），**不覆盖**故障树/诊断手册/根因分析/环境监测/异常覆盖验证等**知识管理与分析框架类**——这些类需要从其他来源（诊断文档、故障案例库、分析报告）物化，或由 LLM 从场景知识产物提取后结构化入图。

---

## 下一步

1. **你复核本清单**，确认 4 个类的物化策略（直接父类 vs 细化子类、IRI 命名规则、属性映射）。
2. 我写 `materialize_vfab.py` 脚本 + 属性补齐（若需）+ 单测。
3. 跑脚本生成 `vfab-individuals.ttl` → `kb.py check` 通过 → UI 可见 8120 个体。
4. **并行做第 2 件事**：19 条 model_prior 知识实例的人工复核（我直接帮你复核，把 `reviewed_by` 落实）——这个独立于 vFab 物化，可以立即开始。

清单完毕。要我继续写物化脚本，还是先复核 19 条 model_prior 实例？
