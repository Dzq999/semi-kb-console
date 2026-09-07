# 增量推理 spike（合成算法已证；**真实基线实测不可行，线上不接线**）

> 状态：步骤 1–3 的算法已实现于等价性脚手架、在合成语料上 in-gate 证明等价；但
> **2026-09-06 真实 52K 基线实测为负结果**（见文末「真实基线实测」）：owlrl 闭包占门禁
> 成本 89%、SHACL 仅占 1%，且 owlrl 对「已闭包图再饱和」**非合流**，
> `saturate(closure(base)∪Δ) ≠ closure(base∪Δ)`。故 owlrl 复用式增量既无显著提速、又不满足
> step-1 赖以成立的图相等论证。**step 4 接线不推进**，`semantic_validate.py` 继续全量闭包
> + Part B 缓存。合成脚手架单测保留（算法逻辑正确、防回归），但**不构成真实门禁可启用的证据**。

## 为什么发布门禁的成本会随库增长而恶化

`semantic_validate.py` 每轮把 `build/semantic/current.trig` 全量喂进：

1. `owlrl` 的 OWL RL 全图闭包物化（随图规模约二次增长的纯 Python 计算）；
2. `pyshacl` 全图校验；
3. 全部 SPARQL 规则。

当前图约 52K quads，闭包已是分钟级并会持续变差。Part B 的缓存只在「整输入集
逐字节未变」时命中——**成功合并了新本体/ABox 的轮次键会变、不命中**，仍走全量。
增量推理要解的正是这一类轮次：只对 delta 及其影响邻域做闭包与 SHACL，而非全图。

## 为什么只做 spike，不直接启用

OWL RL 闭包是**全局**性质，一致性对新增**非单调**：一条新三元组能让**基线**节点
经如下公理链传播、进而参与矛盾：

- `owl:TransitiveProperty`（`ontology/modules/common.ttl:51,54`、`process-route.ttl:25-27`）
- `owl:propertyChainAxiom`（`facility.ttl:19`）
- `owl:inverseOf`、`rdfs:subClassOf`、`rdfs:domain` / `rdfs:range`

只要「受影响邻域」的可达性扩张算错一点，就可能**漏掉一个矛盾**——这是对发布门禁
不可接受的静默削弱。因此必须先用等价性脚手架证明 `incremental == full`，再启用。

## sound 增量方案

步骤 1–3 已实现于 `test_incremental_equivalence.py`（`_prior_closure` / `incremental_verdict`
/ `_affected_targets` / `_shapes_targeting`），并在合成语料上 in-gate 证明与 full 等价。
步骤 4 是把它接进 `semantic_validate.py` 的线上接线，尚未落地。

1. **持久化物化闭包作为 build 产物**：把上一轮的饱和闭包存下来；空白节点按
   `migrate_semantic.py:82,120` 已有的确定性 SHA-256 IRI 方案 skolemize 成稳定 IRI，
   使闭包可跨轮复用、可复现（区别于 `current.trig` 里 run-random 的空白节点）。
   *脚手架现状*：合成语料无空白节点，用进程内 `_PRIOR_CLOSURE_CACHE` 忠实表达「上轮
   闭包被复用」；接线时替换为读 build 产物 + skolemize。
2. **delta 饱和**：新一轮用「上一轮闭包 ∪ delta」再喂 `owlrl` 饱和。OWL RL 闭包
   **单调且合流**，故 `saturate(closure(base) ∪ Δ) == closure(base ∪ Δ)` → 与全量图逐
   三元组相等，是 sound 的必要基础。*脚手架已实现，等价性断言即验证此性质。*
3. **SHACL 只重验受影响邻域** = 与「新增闭包三元组」关联的节点（作主语或 **URI 宾语**）。
   sound 依据：基线闭包已 conform 且发布单调增，某节点入射三元组不变则裁决不变、仍
   conform；纳入 URI 宾语覆盖 inverseOf/对称等把值传播到**宾语位基线节点**的路径。这是
   sound 的超集邻域，等同于对上列公理（TransitiveProperty/propertyChainAxiom/inverseOf/
   subClassOf/domain-range）做可达性扩张的结果。*脚手架已实现；`NeighborhoodScopeTest`
   额外证明邻域确实收窄（良性 delta 跳过基线 w、对抗 delta 仍纳入 w），非「全验」伪省。*
4. **delta 枚举 + 线上接线（未落地）**：`semantic_changesets/pending/*.json` 的 `additions`
   各类型段，减去 `apply_semantic_changeset.py:82-89` 计算的 `seen` 基线集，得纯 delta；
   再把 build 产物闭包 + 本裁决接进 `semantic_validate.py`，由 `SEMANTIC_INCREMENTAL` 门控。

## 接线门槛（第三步前置）

- 引擎侧 env `SEMANTIC_INCREMENTAL`：**默认 off**。`semantic_validate.py` **当前无任何
  运行代码读取它**——这是步骤 4 线上接线的预留标志，本阶段不接线，以免误启用。
- `test_incremental_equivalence.py::IncrementalEquivalenceTest` 已实现并**每轮 in-gate**
  证明 `incremental_verdict == full_verdict`（代表性 + 随机 + 两条闭包依赖对抗 delta，
  PASS/FAIL 与违规数逐项相等）；`NeighborhoodScopeTest` 证明邻域确实收窄。**这些必须长期
  保持全绿**——它们是把增量接进线上的前置条件与回归护栏。
- 步骤 4 接线时：把等价性从合成语料**提升到真实 52K 基线**（对真实 changeset delta 抽样
  跑 `incremental==full`），通过后才把 `SEMANTIC_INCREMENTAL` 接进 `semantic_validate.py`。
- 在此之前：全量闭包 + Part B 缓存是唯一路径，门禁裁决语义完全不变。

## 与其它部分的边界

- 不改发布 PASS/FAIL 判定，不改「未证明候选一律隔离、永不发布」的隔离边界。
- Part B（`SEMANTIC_GATE_CACHE`）与本 spike 正交：缓存命中跳过全量；未命中且
  增量已证明启用时才走增量；两者都失败/关闭则回落全量。

## 真实基线实测（2026-09-06，负结果）

在真实引擎（`data/engine`，`current.trig` + `knowledge/semantic` + `ontology`，闭包前
78,220 三元组）上**只读**测了两个决定 owlrl 复用式增量是否值得接线的问题。脚本见
`scratchpad/measure_incremental.py` / `measure_clean.py`（只读、不写 KB）。

**Q1 成本分解**（单轮全量门禁）：

| 阶段 | 耗时 | 占比 |
|---|---|---|
| owlrl OWL RL 闭包（78,220→122,416，+44,196） | 86.9–89.4s | **89%** |
| 271 条 SPARQL 规则（+3,300） | 9.7s | 10% |
| pyshacl（433 显式 target，conform） | 1.4s | **1%** |

→ step-1 里实现的 sound 优化（SHACL 受影响邻域收窄）只作用在 **1%** 上；即便实测
1.5x SHACL 提速，也就省 ~0.5s / ~100s。规则是任意 SPARQL、不可廉价增量化。**唯一
有意义的成本杠杆是闭包（89%）。**

**Q2 闭包复用是否提速**：`re-saturate(prior_closure ∪ Δ)` = 66.3s vs
`fresh closure(base ∪ Δ)` = 86.5s → 仅 **0.77x**。owlrl `DeductiveClosure` 重算整个
不动点、不利用图已闭包这一事实 → **无数量级收益。**

**claim A（合流性）在真实图上不成立**：即使隔离规则污染、只比纯闭包，
`saturate(closure(base) ∪ Δ) ≠ closure(base ∪ Δ)`：
- `only_in_fresh=64`：owlrl 的 `ErrorMessage`/`error` 记账三元组（例：
  *"Disjoint classes xsd:decimal and xsd:double have a common individual 0.62"* 的钳制标记）；
- `only_in_inc=1425`：对已闭包图再饱和时 owlrl 生成的 `Literal a rdfs:Datatype` 噪声。

→ **owlrl 闭包对「已闭包图再饱和」非确定/非合流**。step-1 赖以成立的「图逐三元组相等
→ 裁决相等」论证**不迁移到真实图**。要救活只能改证「这些差异三元组永不影响任何 SHACL
shape 与安全检查」——脆弱且随 owlrl 版本变化，不足以作 sound 发布门禁的基础。

**结论**：owlrl 复用式增量**既无显著提速（闭包 0.77x、SHACL 仅占 1%），又不 sound
（真实图非合流）**。合成脚手架单测仍正确（验证的是算法逻辑），但**不构成真实门禁可
启用的证据**。**step 4 接线不推进**；治理边界全程未被触碰（`SEMANTIC_INCREMENTAL`
仍无运行代码读取）。真正能压闭包成本的是**换推理器**（RDFox / souffle-datalog / 专用
semi-naive OWL RL 物化器），属独立基建决策，不在本 spike 内。
