"""Part C 等价性脚手架（增量推理）。

设计见 data/engine/docs/incremental-reasoning-spike.md。第一步（本文件）已实现 sound
增量裁决 `incremental_verdict()`（持久化物化闭包 ∪ delta 再饱和 → 只对受影响邻域跑
SHACL），并用 `IncrementalEquivalenceTest` 每轮 in-gate 证明 `incremental == full`
（PASS/FAIL 与违规数逐项相等，含对抗 delta）。

**边界（治理红线不动）**：`SEMANTIC_INCREMENTAL` 仍默认 off，且 semantic_validate.py
**尚未读取它**——增量只在本脚手架内被证明为等价，未接进任何线上门禁运行代码。把增量
接入 `semantic_validate.py`（步骤 4：从 changeset 枚举 delta、把物化闭包落 build 产物）
是后续第三步，以本文件等价性长期通过为前提。

**真实基线实测（2026-09-06，负结果）**：本脚手架证明的是**算法逻辑**在合成语料上正确，
**不代表 owlrl 复用式增量在真实图上可启用**。真实 52K 基线实测（见设计文档「真实基线实测」）
显示：(1) owlrl 闭包占门禁成本 89%、SHACL 仅占 1%，本文件优化的 SHACL 邻域收窄够不着
主成本；(2) 闭包复用仅 0.77x，无数量级收益；(3) 关键——owlrl 闭包对「已闭包图再饱和」
**非合流**（`saturate(closure(base)∪Δ)≠closure(base∪Δ)`，差异在 owlrl 内部 error/datatype
记账三元组），故本脚手架赖以成立的「图相等→裁决相等」论证**不迁移到真实图**。因此
**step 4 不推进**；本文件保留为算法回归护栏，**不构成把 `SEMANTIC_INCREMENTAL` 接入线上的
证据**。

脚手架各件：
- `full_verdict()` 复刻发布门禁裁决管线（owlrl 全图闭包 → pyshacl，inference "none"，
  与 semantic_validate.py 一致）——增量的黄金对照。
- `CorpusDiscriminatingUnderFullTest`：断言 delta 语料在 full 下确有区分度（良性全
  PASS、对抗全 FAIL），其中两条对抗 delta 的 FAIL **只在闭包之后**才显现（经
  owl:inverseOf 把第二个值传播到**基线**节点触发 maxCount）——否则等价性断言空转。
- `IncrementalEquivalenceTest`：in-gate 证明 incremental==full；增量 scope 若算错就会
  在这两条闭包依赖的对抗 delta 上立刻红。
"""
from __future__ import annotations

import os
import random
import unittest

try:
    from rdflib import Graph, Namespace, RDF, URIRef, Literal
    from rdflib.namespace import SH, XSD
    from owlrl import DeductiveClosure, OWLRL_Semantics
    from pyshacl import validate as shacl_validate
except ModuleNotFoundError:  # 依赖检查由 semantic_validate.py 强制；收集阶段可跳过
    Graph = None

EX = Namespace("urn:pxai:semi:spike#") if Graph else None


def _incremental_enabled() -> bool:
    """步骤 3 预留开关：将来由 semantic_validate.py 读取以决定线上是否走增量路径。

    本脚手架的等价性证明**不受它门控**——`incremental_verdict()` 已实现，
    `IncrementalEquivalenceTest` 始终 in-gate 跑，作为增量实现的长期回归护栏。
    """
    return os.getenv("SEMANTIC_INCREMENTAL", "0").casefold() not in {"0", "false", "no", "off"}


# --------------------------------------------------------------------------- #
# 合成 TBox / SHACL / 基线 ABox：小而自足、毫秒级；覆盖会让「增量 scope 算错就漏
# 判」的危险公理（inverseOf 让基线节点获得第二个值）。真实 52K 基线的等价性验证是
# 实现增量时的后续项——脚手架先把管线与契约钉死。
# --------------------------------------------------------------------------- #
def _schema() -> "Graph":
    g = Graph()
    g.bind("ex", EX)
    # sole 为函数性对象属性；soleInv 为其反向——基线用 soleInv 断言，闭包后回填 sole。
    g.add((EX.sole, RDF.type, URIRef("http://www.w3.org/2002/07/owl#ObjectProperty")))
    g.add((EX.sole, RDF.type, URIRef("http://www.w3.org/2002/07/owl#FunctionalProperty")))
    g.add((EX.soleInv, RDF.type, URIRef("http://www.w3.org/2002/07/owl#ObjectProperty")))
    g.add((EX.soleInv, URIRef("http://www.w3.org/2002/07/owl#inverseOf"), EX.sole))
    return g


def _shapes() -> "Graph":
    g = Graph()
    g.bind("sh", SH)
    g.bind("ex", EX)
    shape = EX.WidgetShape
    g.add((shape, RDF.type, SH.NodeShape))
    g.add((shape, SH.targetClass, EX.Widget))
    # Widget 不得同时是 B（互斥，直接可判）
    not_b = URIRef("urn:pxai:semi:spike#_notB")
    g.add((shape, SH["not"], not_b))
    g.add((not_b, SH["class"], EX.B))
    # sole 至多一个值（闭包后才可能出现第二个值 → 触发）
    prop = URIRef("urn:pxai:semi:spike#_soleAtMostOne")
    g.add((shape, SH.property, prop))
    g.add((prop, SH.path, EX.sole))
    g.add((prop, SH.maxCount, Literal(1, datatype=XSD.integer)))
    return g


def _base_abox() -> "Graph":
    g = Graph()
    g.bind("ex", EX)
    # w 是 Widget；经 soleInv(v1, w) 在闭包后得到 sole(w, v1)（此时仅一个值，PASS）。
    g.add((EX.w, RDF.type, EX.Widget))
    g.add((EX.v1, EX.soleInv, EX.w))
    return g


def full_verdict(delta: "Graph") -> tuple[str, int]:
    """发布门禁裁决管线的忠实复刻：base+delta → owlrl 全图闭包 → pyshacl。

    return (status, violation_count)。status=="pass" iff SHACL conforms。
    """
    data = _base_abox()
    for triple in delta:
        data.add(triple)
    schema = _schema()
    data += schema
    DeductiveClosure(OWLRL_Semantics).expand(data)
    conforms, results_graph, _ = shacl_validate(
        data_graph=data, shacl_graph=_shapes(), ont_graph=schema,
        inference="none", advanced=True,
    )
    violations = len(list(results_graph.subjects(RDF.type, SH.ValidationResult)))
    return ("pass" if conforms else "fail", violations)


# --------------------------------------------------------------------------- #
# 步骤 1：持久化物化闭包。脚手架用进程内缓存忠实表达「上轮闭包被复用」；真实实现会
# 把闭包落 build 产物、并把空白节点 skolemize 成稳定 SHA-256 IRI（沿用
# migrate_semantic.py:82,120 的确定性方案）——合成语料无空白节点，缓存即等价表达。
# --------------------------------------------------------------------------- #
_PRIOR_CLOSURE_CACHE: "Graph | None" = None


def _prior_closure() -> "Graph":
    """上轮（基线）物化闭包的一份新副本。基线本身已通过门禁 → conform（0 违规）。"""
    global _PRIOR_CLOSURE_CACHE
    if _PRIOR_CLOSURE_CACHE is None:
        g = Graph()
        for triple in _base_abox():
            g.add(triple)
        for triple in _schema():
            g.add(triple)
        DeductiveClosure(OWLRL_Semantics).expand(g)
        _PRIOR_CLOSURE_CACHE = g
    out = Graph()
    for triple in _PRIOR_CLOSURE_CACHE:
        out.add(triple)
    return out


def _affected_targets(prior_set: set, saturated: "Graph") -> set:
    """步骤 3：受影响焦点节点 = 与「新增闭包三元组」关联的节点（作主语或 URI 宾语）。

    sound 依据：基线闭包已 conform，且发布单调增（changeset 只加不减）。SHACL 对某焦点
    节点的裁决只取决于其 shape 相关的入射三元组；若某节点的入射三元组在 prior 与
    saturated 间完全一致，其裁决必不变、仍 conform。故只有「与 diff 三元组关联的节点」
    可能新违反。纳入 URI 宾语是为覆盖 inverseOf/对称等把值传播到**宾语位基线节点**的
    路径（如 delta `vX soleInv w` 经闭包让基线 w 获得第二个 sole 值）。这是 sound 的
    超集邻域——真实实现按公理可达性扩张得同一集合。
    """
    affected: set = set()
    for s, p, o in saturated:
        if (s, p, o) in prior_set:
            continue
        affected.add(s)
        if isinstance(o, URIRef):
            affected.add(o)
    return affected


def _shapes_targeting(affected: set, saturated: "Graph") -> "Graph":
    """把 _shapes() 的 sh:targetClass 改写为受影响 Widget 的显式 sh:targetNode。

    整张 saturated 图仍作 data_graph（属性路径可正常遍历到邻居），仅把 SHACL 焦点收窄
    到受影响邻域——这正是增量相对全量省下的部分。affected∩Widget 为空时 shape 无目标、
    conform（良性 delta 未触及任何 Widget 的 shape 相关三元组时）。
    """
    g = _shapes()
    shape = EX.WidgetShape
    g.remove((shape, SH.targetClass, EX.Widget))
    widgets = set(saturated.subjects(RDF.type, EX.Widget))
    for node in affected & widgets:
        g.add((shape, SH.targetNode, node))
    return g


def incremental_verdict(delta: "Graph") -> tuple[str, int]:
    """sound 增量裁决：上轮物化闭包 ∪ delta 再饱和 → 只对受影响邻域跑 SHACL。

    return (status, violation_count)，与 full_verdict 逐项相等是 IncrementalEquivalence
    Test 的断言。三步对应设计文档 incremental-reasoning-spike.md 步骤 1–3；从 changeset
    枚举 delta（步骤 4）在接入 semantic_validate.py 时落地，脚手架直接以 delta 图为输入。

    等价性证明骨架：RL 闭包单调、合流 → `saturate(closure(base) ∪ Δ) == closure(base ∪
    Δ ∪ schema)`，故 saturated 与 full 的图逐三元组相等；又因基线 conform 且单调增，被
    邻域收窄跳过的焦点节点其入射三元组未变、必仍 conform，不会漏计违规——PASS/FAIL 与
    违规数因此与 full 相等。
    """
    prior = _prior_closure()
    prior_set = set(prior)
    data = prior  # 复用上轮闭包，就地叠加 delta 后再饱和
    for triple in delta:
        data.add(triple)
    DeductiveClosure(OWLRL_Semantics).expand(data)  # 步骤 2：delta 饱和（单调、合流）
    affected = _affected_targets(prior_set, data)   # 步骤 3：受影响邻域
    shapes = _shapes_targeting(affected, data)
    conforms, results_graph, _ = shacl_validate(
        data_graph=data, shacl_graph=shapes, ont_graph=_schema(),
        inference="none", advanced=True,
    )
    violations = len(list(results_graph.subjects(RDF.type, SH.ValidationResult)))
    return ("pass" if conforms else "fail", violations)


# --------------------------------------------------------------------------- #
# delta 语料
# --------------------------------------------------------------------------- #
def _delta_benign() -> "Graph":
    g = Graph()
    g.add((EX.w2, RDF.type, EX.Widget))
    g.add((EX.w2, EX.sole, EX.vNew))  # 新 Widget、单值 → 一致
    return g


def _delta_disjoint_direct() -> "Graph":
    g = Graph()
    g.add((EX.w, RDF.type, EX.B))  # w 同时成 B → sh:not 直接违反（不依赖闭包）
    return g


def _delta_functional_via_closure() -> "Graph":
    g = Graph()
    g.add((EX.w, EX.sole, EX.v2))  # 直接看只有 v2；闭包回填 v1 后 sole 有两个值 → maxCount 违反
    return g


def _delta_inverse_to_base() -> "Graph":
    g = Graph()
    # delta 主语是 vX（本身非违规节点）；闭包经 soleInv⁻¹ 给**基线** w 回填第二个 sole
    # 值（w 已有 v1）→ w 触发 maxCount。违规节点 w 只作 delta 的**宾语**出现——正是
    # 受影响邻域必须纳入 URI 宾语、否则漏判的对抗路径。
    g.add((EX.vX, EX.soleInv, EX.w))
    return g


def _delta_random_benign(seed: int, n: int) -> "Graph":
    rng = random.Random(seed)
    g = Graph()
    for _ in range(n):
        tag = rng.randrange(10_000_000)
        subj = URIRef(f"urn:pxai:semi:spike#rnd{tag}")
        g.add((subj, RDF.type, EX.Widget))
        g.add((subj, EX.sole, URIRef(f"urn:pxai:semi:spike#val{tag}")))
    return g


@unittest.skipIf(Graph is None, "semantic dependencies not installed")
class CorpusDiscriminatingUnderFullTest(unittest.TestCase):
    """现在就跑（in-gate）：证明语料在 full 裁决下确有区分度——否则等价性断言空转。"""

    def test_baseline_passes(self) -> None:
        self.assertEqual(full_verdict(Graph())[0], "pass")

    def test_benign_delta_passes(self) -> None:
        self.assertEqual(full_verdict(_delta_benign())[0], "pass")

    def test_disjoint_delta_fails(self) -> None:
        self.assertEqual(full_verdict(_delta_disjoint_direct())[0], "fail")

    def test_functional_conflict_is_closure_dependent(self) -> None:
        # 关键：该 delta 的 FAIL 只在闭包后显现——正是增量 scope 必须覆盖的传播路径。
        status, violations = full_verdict(_delta_functional_via_closure())
        self.assertEqual(status, "fail")
        self.assertGreaterEqual(violations, 1)

    def test_inverse_to_base_is_closure_dependent(self) -> None:
        # 违规落在**基线**节点 w，且 w 只作 delta 的宾语出现——增量邻域若不纳入 URI
        # 宾语就会漏判。full 下必 FAIL，作为等价性证明的对抗对照。
        status, violations = full_verdict(_delta_inverse_to_base())
        self.assertEqual(status, "fail")
        self.assertGreaterEqual(violations, 1)

    def test_random_benign_deltas_all_pass(self) -> None:
        for seed in range(8):
            self.assertEqual(full_verdict(_delta_random_benign(seed, 3))[0], "pass")


@unittest.skipIf(Graph is None, "semantic dependencies not installed")
class IncrementalEquivalenceTest(unittest.TestCase):
    """in-gate 证明 incremental==full（PASS/FAIL 与违规数）；增量实现的长期回归护栏。

    增量已实现（见 incremental_verdict），此断言始终运行——毫秒级合成语料。它不由
    SEMANTIC_INCREMENTAL 门控：该标志预留给步骤 3 的线上接线，与「证明等价」是两件事。
    """

    def _assert_equiv(self, delta: "Graph") -> None:
        self.assertEqual(incremental_verdict(delta), full_verdict(delta))

    def test_equiv_baseline(self) -> None:
        self._assert_equiv(Graph())

    def test_equiv_benign(self) -> None:
        self._assert_equiv(_delta_benign())

    def test_equiv_disjoint(self) -> None:
        self._assert_equiv(_delta_disjoint_direct())

    def test_equiv_functional_via_closure(self) -> None:
        self._assert_equiv(_delta_functional_via_closure())

    def test_equiv_inverse_to_base(self) -> None:
        self._assert_equiv(_delta_inverse_to_base())

    def test_equiv_random_benign(self) -> None:
        for seed in range(8):
            self._assert_equiv(_delta_random_benign(seed, 3))


@unittest.skipIf(Graph is None, "semantic dependencies not installed")
class NeighborhoodScopeTest(unittest.TestCase):
    """证明等价性**不是**靠「把所有节点都验一遍」达成——邻域确实收窄了 SHACL 焦点。

    若无此断言，`_affected_targets` 退化成返回全体节点时等价性仍会通过，增量就名存实亡。
    """

    def _affected(self, delta: "Graph") -> set:
        prior = _prior_closure()
        prior_set = set(prior)
        data = prior
        for triple in delta:
            data.add(triple)
        DeductiveClosure(OWLRL_Semantics).expand(data)
        return _affected_targets(prior_set, data)

    def test_benign_delta_skips_baseline_widget(self) -> None:
        # 良性 delta：基线 w 的入射三元组未变 → 不在受影响邻域 → 增量真的跳过它；
        # 但新 Widget w2 在邻域内、确被校验（邻域非空，非「全跳过」的伪省）。
        affected = self._affected(_delta_benign())
        self.assertNotIn(EX.w, affected)
        self.assertIn(EX.w2, affected)

    def test_inverse_to_base_delta_reaches_baseline_widget(self) -> None:
        # 对抗 delta：违规基线节点 w 只作 delta 宾语，仍必须落进邻域，否则漏判。
        self.assertIn(EX.w, self._affected(_delta_inverse_to_base()))


if __name__ == "__main__":
    unittest.main()
