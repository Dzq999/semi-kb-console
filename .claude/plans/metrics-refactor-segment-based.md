# 指标体系重构方案：统一segment业务基数

## 背景

当前指标体系存在基数混用问题：
- `individuals_domain: 3004`（技术基数，包含所有非噪声个体）
- `segment统计: 2436`（业务基数，只包含12个策展模块内的个体）

用户正确指出：**工艺段拆分（fab/ap/cross）才是核心业务指标**，应该用统一的业务基数。

## 当前问题

### 数据分层

```
总个体 9411
  ↓ 去噪（排除5种结构/溯源噪声）
领域个体 3004 (individuals_domain)
  ↓ 按来源拆分              ↓ 按12个策展模块过滤
knowledge/operational    策展模块个体 2436
  /untagged                 ↓ 按工艺段拆分
 (总和3004)              fab/ap/cross
                          (总和2436)
```

### 当前metrics返回

```python
{
  "individuals": 9411,           # 含噪声
  "individuals_domain": 3004,    # 技术基数
  "individuals_knowledge": 1874, # 基于3004计算
  "individuals_operational": 0,  # 基于3004计算
  "individuals_untagged": 1130,  # 基于3004计算
  "segment_fab": 120,            # 基于2436计算
  "segment_ap": 111,             # 基于2436计算
  "segment_cross": 2205          # 基于2436计算
}
```

问题：**基数不统一，1874 + 0 + 1130 = 3004，但 120 + 111 + 2205 = 2436**

### 业务视角的正确口径

在2436个策展模块个体中：
- knowledge: 1307 (54%)
- operational: 0 (0%)
- untagged: 1129 (46%)

## 重构方案

### 目标

统一使用**2436个策展模块个体**作为业务基数，提供清晰的工艺段×来源双维度视图。

### 新的metrics结构

```python
{
  # 核心业务指标（基数：2436）
  "segment_fab": 120,
  "segment_ap": 111,
  "segment_cross": 2205,
  "segment_total": 2436,          # 新增：明确基数
  
  # 数据来源（同样基数：2436）
  "individuals_knowledge": 1307,  # 调整：基于2436计算
  "individuals_operational": 0,   # 调整：基于2436计算
  "individuals_untagged": 1129,   # 调整：基于2436计算
  
  # 保留技术指标但降优先级
  "classes": 1079,
  "properties": 1680,
  "individuals": 9411,            # 含噪声的总个体数
  
  # 其他业务指标
  "business_relations": 394,
  "simulation_scenarios": 198,
  ...
}
```

### 实现变更

#### 1. backend/app/services/semi_kb.py

**修改 `_individual_source_split()` 方法**：

当前逻辑：基于3004个去噪个体统计
```python
def _individual_source_split(self, data: Graph) -> dict[str, int]:
    noise = {OWL.Class, ...}
    denoise = {s for s, _, o in data.triples((None, RDF.type, None)) if o not in noise}
    # 统计所有denoise个体的sourceType
```

新逻辑：基于2436个策展模块个体统计
```python
def _individual_source_split(self, data: Graph) -> dict[str, int]:
    """按 sourceType 拆分策展模块个体（与 segment_split 使用相同基数）。
    
    基数对齐：只统计能映射到12个策展模块的实例，与 _segment_split() 保持一致。
    返回 {"domain": 2436, "knowledge": 1307, "operational": 0, "untagged": 1129}
    """
    prov_entity = URIRef("http://www.w3.org/ns/prov#Entity")
    noise = {OWL.Class, OWL.ObjectProperty, OWL.DatatypeProperty, RDF.Statement, prov_entity}
    
    # 与 _segment_split 相同的过滤逻辑：只保留12个策展模块的个体
    _, class_to_module, _ = self._load_schema_graph()
    domain_individuals = []
    for s, _, o in data.triples((None, RDF.type, None)):
        if o in noise:
            continue
        module = class_to_module.get(o)
        if module and module in self._DOMAIN_MODULE_LABELS:
            domain_individuals.append(s)
    
    # 在这2436个个体上统计sourceType分布
    # ... 原有的sourceType统计逻辑 ...
```

**修改 `semantic_counts()` 方法**：

```python
def semantic_counts(self) -> dict[str, int]:
    # ... 现有逻辑 ...
    
    source_split = self._individual_source_split(data)
    segment_split = self._segment_split(data)
    
    result = {
        # ... 其他指标 ...
        
        # 策展模块个体总数（新增，明确基数）
        "segment_total": segment_split["domain"],  # 2436
        
        # 数据来源（基于2436）
        "individuals_knowledge": source_split["knowledge"],   # 1307
        "individuals_operational": source_split["operational"], # 0
        "individuals_untagged": source_split["untagged"],     # 1129
        
        # 移除 individuals_domain（避免混淆）或标注为deprecated
    }
    
    # 工艺段拆分保持不变
    for seg, count in segment_split["by_segment"].items():
        result[f"segment_{seg}"] = count
    result["segment_cross"] = segment_split["cross"]
    
    return result
```

#### 2. backend/app/services/reports.py

**更新 METRIC_LABELS**：

```python
METRIC_LABELS = [
    ("classes", "类 Class"),
    ("properties", "属性 Property"),
    ("relations", "关系 Relation"),
    # 移除 ("individuals", "实例 Individual"),  # 不再展示含噪声的总数
    ("segment_fab", "实例 · 前段厂 (fab)"),
    ("segment_ap", "实例 · 后段厂 (ap)"),
    ("segment_cross", "实例 · 跨段通用"),
    ("segment_total", "实例 · 总计"),  # 新增
    ("axioms", "公理 Axiom"),
    ("rules", "推理规则 Rule"),
    ("knowledge_entries", "知识条目"),
    ("business_relations", "经营模型关系"),
    ("simulation_scenarios", "仿真场景"),
    ("scenario_articles", "场景知识产物"),
]
```

**更新 fixed_metrics_markdown() 中的脚注**：

```python
def fixed_metrics_markdown(snapshot: dict) -> str:
    # ... 表格生成逻辑 ...
    
    # 更新脚注：明确说明统计口径
    total = int(totals.get("segment_total", 0))
    if total:
        knowledge = int(totals.get("individuals_knowledge", 0))
        operational = int(totals.get("individuals_operational", 0))
        untagged = int(totals.get("individuals_untagged", 0))
        lines.append(
            f"\n**注**：实例统计基于12个策展模块（{total:,}个），"
            f"按来源分类：知识实例 {knowledge:,}、运行数据 {operational:,}、未标注 {untagged:,}。"
        )
```

#### 3. frontend/src/App.tsx

**调整指标卡片**：

移除或降优先级：
- `individuals`（含噪声）
- `individuals_domain`（技术概念）

保持核心指标：
- `segment_fab`
- `segment_ap`
- `segment_cross`
- `segment_total`（新增）

新增数据来源卡片（可选）：
- `individuals_knowledge`（调整为1307）
- `individuals_operational`（0）
- `individuals_untagged`（1129）

#### 4. backend/app/services/qa.py

**无需修改**：

`ontology_context()` 使用的是本体schema（类/属性定义），不涉及个体实例统计，不受此次重构影响。

问答系统的接地上下文主要依赖：
- `business_models`（经营模型）
- `simulation_scenarios`（仿真场景）
- `knowledge_sources`（vFab知识来源）
- `ontology_terms`（本体术语定义，来自schema）

这些都不依赖individuals_domain或segment统计。

## 影响范围

### 需要修改的文件

1. ✅ `backend/app/services/semi_kb.py`
   - `_individual_source_split()` - 改用策展模块个体基数
   - `semantic_counts()` - 调整返回结构

2. ✅ `backend/app/services/reports.py`
   - `METRIC_LABELS` - 更新指标列表
   - `fixed_metrics_markdown()` - 更新脚注说明

3. ✅ `frontend/src/App.tsx`
   - 调整指标卡片展示

### 不需要修改的文件

- `backend/app/services/qa.py` - 不依赖individual统计
- `backend/app/services/wecom_qa_bridge.py` - 不依赖individual统计
- 其他使用metrics的地方 - API结构保持向后兼容

## 验证计划

### 1. 单元测试

创建 `backend/tests/test_segment_source_alignment.py`：

```python
def test_segment_source_alignment():
    """验证 source_split 和 segment_split 使用相同基数"""
    data = semi_kb._load_data_graph()
    source = semi_kb._individual_source_split(data)
    segment = semi_kb._segment_split(data)
    
    # 两者domain应该相等
    assert source["domain"] == segment["domain"]
    
    # source的三类总和应该等于segment总数
    assert (source["knowledge"] + source["operational"] + 
            source["untagged"]) == segment["domain"]
    
    # segment的工艺段总和应该等于domain
    assert (sum(segment["by_segment"].values()) + 
            segment["cross"]) == segment["domain"]
```

### 2. API验证

```bash
curl -s http://127.0.0.1:8765/api/ontology/metrics | python -c "
import json, sys
m = json.load(sys.stdin)['totals']
assert m['individuals_knowledge'] + m['individuals_operational'] + m['individuals_untagged'] == m['segment_total']
assert m['segment_fab'] + m['segment_ap'] + m['segment_cross'] == m['segment_total']
print('✓ 基数对齐验证通过')
print(f'策展模块个体: {m[\"segment_total\"]}')
print(f'  fab={m[\"segment_fab\"]}, ap={m[\"segment_ap\"]}, cross={m[\"segment_cross\"]}')
print(f'  knowledge={m[\"individuals_knowledge\"]}, operational={m[\"individuals_operational\"]}, untagged={m[\"individuals_untagged\"]}')
"
```

### 3. 日报验证

生成一份日报，检查：
- 脚注说明是否正确（基数2436）
- 指标表是否展示segment_total
- 数字是否自洽

### 4. 前端验证

打开前端页面，检查：
- 指标卡片是否展示正确
- 数字是否与API一致
- 工艺段和来源两组指标是否清晰

## 回滚策略

如果重构后发现问题，回滚方案：

1. **Git回滚**：`git revert <commit-hash>`

2. **代码层面**：
   - 在 `_individual_source_split()` 中保留旧逻辑作为fallback
   - 通过配置开关控制使用哪个基数

3. **数据层面**：
   - 重构不涉及数据迁移，只是计算口径变化
   - 回滚后metrics立即恢复

## 时间估算

- 代码修改：30分钟
- 测试验证：20分钟
- 前端调整：15分钟
- 提交部署：10分钟

**总计：约1.5小时**

## 风险评估

### 低风险
- ✅ 只改计算逻辑，不改数据结构
- ✅ API字段保持向后兼容
- ✅ 可快速回滚

### 需注意
- ⚠️ individuals_knowledge/operational/untagged 数值会变化（1874→1307，1130→1129）
- ⚠️ 日报中的脚注说明需要同步更新
- ⚠️ 已发送的历史日报中的数字与新口径不一致（属正常，说明口径优化）

## 总结

此次重构将指标体系统一到**策展模块个体（2436）**这一业务基数上，使得：
1. 工艺段拆分（fab/ap/cross）和数据来源（knowledge/operational/untagged）使用相同基数
2. 指标体系更清晰，业务人员更易理解
3. 数字自洽，不再出现基数混用导致的困惑

核心变化：**individuals_knowledge从1874降至1307，individuals_untagged从1130降至1129**，这是因为原来的1874包含了568个不在12个策展模块内的个体，现在统一口径后排除了这部分。
