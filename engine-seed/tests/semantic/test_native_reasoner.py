"""native 推理器的单元测试：Cert B（依赖护栏）+ Cert C（覆盖正确性/幂等/合流）。

由 semantic_test.py 的 `unittest discover -s tests/semantic` 在每轮门禁内收集执行，
必须保持毫秒级：全部用小合成图或纯静态扫描，**不跑真实 52K 基线闭包**（那属慢测、
opt-in，见 test_native_equivalence_realbaseline.py）。

- Cert B（PreconditionGuardTest）：把「当前 core-shapes.ttl 与 271 条规则无一依赖 native
  跳过的构造」这条静态结论固化为**可执行不变量**——日后有人越界立即变红；并证明护栏
  对构造过的越界输入确实报 blocker。
- Cert C（ReasonerCorrectnessTest）：逐规则断言 native 物化正确、幂等、合流（含 owlrl
  做不到的「对已物化图增量再物化 == 全量」），并断言 native 闭包 ⊆ owlrl 闭包（不多产）。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

try:
    from rdflib import BNode, Graph, Literal, Namespace, RDF, RDFS, OWL, URIRef
    from owlrl import DeductiveClosure, OWLRL_Semantics
    import native_reasoner  # noqa: E402
except ModuleNotFoundError:  # 依赖检查由 semantic_validate.py 强制；收集阶段可跳过
    Graph = None

EX = Namespace("urn:pxai:semi:spike#") if Graph else None
SH = "http://www.w3.org/ns/shacl#"


def _native(triples) -> set:
    return native_reasoner.materialize_triples(set(triples))


def _owlrl(triples) -> set:
    g = Graph()
    for t in triples:
        g.add(t)
    DeductiveClosure(OWLRL_Semantics).expand(g)
    return set(g)


def _object_prop(*props) -> list:
    return [(p, RDF.type, OWL.ObjectProperty) for p in props]


@unittest.skipIf(Graph is None, "semantic dependencies not installed")
class PreconditionGuardTest(unittest.TestCase):
    """Cert B：native 等价前置的依赖护栏。"""

    def _real_shapes(self) -> "Graph":
        g = Graph()
        for path in (ROOT / "ontology" / "shapes").glob("*.ttl"):
            g.parse(path, format="turtle")
        return g

    def _real_schema(self) -> "Graph":
        g = Graph()
        for path in (ROOT / "ontology" / "modules").glob("*.ttl"):
            g.parse(path, format="turtle")
        return g

    def _real_rule_texts(self) -> list[str]:
        registry = json.loads((ROOT / "ontology" / "rules" / "registry.json").read_text(encoding="utf-8"))
        texts: list[str] = []
        for rule in registry.get("rules") or []:
            impl = rule.get("implementation")
            if isinstance(impl, str) and impl.endswith(".rq"):
                path = ROOT / impl
                if path.is_file():
                    texts.append(path.read_text(encoding="utf-8"))
        return texts

    def test_current_ontology_is_fully_covered(self) -> None:
        # 可执行不变量：现网 shapes+规则+本体无一依赖 native 跳过的构造 → 护栏返回空。
        # 越界（加 sh:not / 规则引用 owl:sameAs / >2 元 chain）会立即让此断言变红。
        blockers = native_reasoner.equivalence_precondition(
            self._real_shapes(), self._real_rule_texts(), self._real_schema())
        self.assertEqual(blockers, [], f"native profile 被越界依赖破坏：{blockers}")

    def test_flags_sh_not_shape(self) -> None:
        shapes = Graph()
        shape = EX.S
        shapes.add((shape, RDF.type, URIRef(SH + "NodeShape")))
        shapes.add((shape, URIRef(SH + "not"), EX._blank))
        blockers = native_reasoner.equivalence_precondition(shapes, [], None)
        self.assertTrue(any("sh:not" in b for b in blockers))

    def test_flags_sh_sparql_shape(self) -> None:
        shapes = Graph()
        shapes.add((EX.S, URIRef(SH + "sparql"), EX._q))
        blockers = native_reasoner.equivalence_precondition(shapes, [], None)
        self.assertTrue(any("sh:sparql" in b for b in blockers))

    def test_flags_rule_referencing_owl_sameas(self) -> None:
        rule = "PREFIX owl: <http://www.w3.org/2002/07/owl#>\nCONSTRUCT { ?a ?p ?o } WHERE { ?a owl:sameAs ?b . ?b ?p ?o }"
        blockers = native_reasoner.equivalence_precondition(Graph(), [rule], None)
        self.assertTrue(any("owl:sameAs" in b for b in blockers))

    def test_flags_rule_referencing_full_owl_uri(self) -> None:
        # 规则用完整 URI 而非前缀也要拦到（http://www.w3.org/2002/07/owl#equivalentClass）。
        rule = "CONSTRUCT { ?x a ?c } WHERE { ?x a ?d . ?d <http://www.w3.org/2002/07/owl#equivalentClass> ?c }"
        blockers = native_reasoner.equivalence_precondition(Graph(), [rule], None)
        self.assertTrue(any("owl:equivalentClass" in b for b in blockers))

    def test_flags_oversized_property_chain(self) -> None:
        schema = Graph()
        n1, n2, n3 = BNode(), BNode(), BNode()
        schema.add((EX.r, OWL.propertyChainAxiom, n1))
        schema.add((n1, RDF.first, EX.p)); schema.add((n1, RDF.rest, n2))
        schema.add((n2, RDF.first, EX.q)); schema.add((n2, RDF.rest, n3))
        schema.add((n3, RDF.first, EX.s)); schema.add((n3, RDF.rest, RDF.nil))
        blockers = native_reasoner.equivalence_precondition(Graph(), [], schema)
        self.assertTrue(any("propertyChainAxiom" in b for b in blockers))

    def test_two_element_chain_is_not_flagged(self) -> None:
        schema = Graph()
        n1, n2 = BNode(), BNode()
        schema.add((EX.r, OWL.propertyChainAxiom, n1))
        schema.add((n1, RDF.first, EX.p)); schema.add((n1, RDF.rest, n2))
        schema.add((n2, RDF.first, EX.q)); schema.add((n2, RDF.rest, RDF.nil))
        blockers = native_reasoner.equivalence_precondition(Graph(), [], schema)
        self.assertEqual(blockers, [])


@unittest.skipIf(Graph is None, "semantic dependencies not installed")
class ReasonerCorrectnessTest(unittest.TestCase):
    """Cert C：逐规则覆盖正确性 + 幂等 + 合流 + ⊆ owlrl。"""

    def test_subclass_transitivity_and_type_propagation(self) -> None:
        base = [
            (EX.A, RDFS.subClassOf, EX.B), (EX.B, RDFS.subClassOf, EX.C),
            (EX.x, RDF.type, EX.A),
        ]
        closed = _native(base)
        self.assertIn((EX.x, RDF.type, EX.B), closed)
        self.assertIn((EX.x, RDF.type, EX.C), closed)
        self.assertIn((EX.A, RDFS.subClassOf, EX.C), closed)
        self.assertTrue(closed <= _owlrl(base))

    def test_subproperty(self) -> None:
        base = _object_prop(EX.p, EX.q) + [
            (EX.p, RDFS.subPropertyOf, EX.q), (EX.x, EX.p, EX.y),
        ]
        closed = _native(base)
        self.assertIn((EX.x, EX.q, EX.y), closed)
        self.assertTrue(closed <= _owlrl(base))

    def test_domain(self) -> None:
        base = _object_prop(EX.p) + [(EX.p, RDFS.domain, EX.C), (EX.x, EX.p, EX.y)]
        closed = _native(base)
        self.assertIn((EX.x, RDF.type, EX.C), closed)
        self.assertTrue(closed <= _owlrl(base))

    def test_range(self) -> None:
        base = _object_prop(EX.p) + [(EX.p, RDFS.range, EX.D), (EX.x, EX.p, EX.y)]
        closed = _native(base)
        self.assertIn((EX.y, RDF.type, EX.D), closed)
        self.assertTrue(closed <= _owlrl(base))

    def test_inverse_both_directions(self) -> None:
        base = _object_prop(EX.p, EX.q) + [(EX.p, OWL.inverseOf, EX.q), (EX.x, EX.p, EX.y)]
        closed = _native(base)
        self.assertIn((EX.y, EX.q, EX.x), closed)
        # 反向断言也应回填正向
        base2 = _object_prop(EX.p, EX.q) + [(EX.p, OWL.inverseOf, EX.q), (EX.y, EX.q, EX.x)]
        self.assertIn((EX.x, EX.p, EX.y), _native(base2))
        self.assertTrue(closed <= _owlrl(base))

    def test_transitive(self) -> None:
        base = _object_prop(EX.t) + [
            (EX.t, RDF.type, OWL.TransitiveProperty),
            (EX.a, EX.t, EX.b), (EX.b, EX.t, EX.c), (EX.c, EX.t, EX.d),
        ]
        closed = _native(base)
        self.assertIn((EX.a, EX.t, EX.c), closed)
        self.assertIn((EX.a, EX.t, EX.d), closed)  # 传递闭包到底
        self.assertIn((EX.b, EX.t, EX.d), closed)
        self.assertTrue(closed <= _owlrl(base))

    def test_two_element_property_chain(self) -> None:
        n1, n2 = BNode(), BNode()
        base = _object_prop(EX.p, EX.q, EX.r) + [
            (EX.r, OWL.propertyChainAxiom, n1),
            (n1, RDF.first, EX.p), (n1, RDF.rest, n2),
            (n2, RDF.first, EX.q), (n2, RDF.rest, RDF.nil),
            (EX.x, EX.p, EX.y), (EX.y, EX.q, EX.z),
        ]
        closed = _native(base)
        self.assertIn((EX.x, EX.r, EX.z), closed)
        self.assertTrue(closed <= _owlrl(base))

    def test_idempotent(self) -> None:
        g = Graph()
        for t in _object_prop(EX.p, EX.q) + [
            (EX.A, RDFS.subClassOf, EX.B), (EX.x, RDF.type, EX.A),
            (EX.p, OWL.inverseOf, EX.q), (EX.x, EX.p, EX.y),
        ]:
            g.add(t)
        n1 = native_reasoner.materialize(g)
        self.assertGreater(n1, 0)
        n2 = native_reasoner.materialize(g)  # 已到不动点 → 不再新增
        self.assertEqual(n2, 0)

    # --- 合流 / 顺序无关：native 有、owlrl 无的关键性质 ------------------------- #
    def _confluence_schema(self) -> list:
        return _object_prop(EX.p, EX.q, EX.anc) + [
            (EX.p, OWL.inverseOf, EX.q),
            (EX.anc, RDF.type, OWL.TransitiveProperty),
        ]

    def _assert_confluent(self, base: list, delta: list) -> None:
        base_set, delta_set = set(base), set(delta)
        prior = native_reasoner.materialize_triples(base_set)
        full = native_reasoner.materialize_triples(base_set | delta_set)
        tb = native_reasoner.extract_tbox(base_set | delta_set)
        inc = native_reasoner.materialize_triples(
            base_set | delta_set, tbox=tb, seed=delta_set, start=prior | delta_set)
        self.assertEqual(full, inc, f"symdiff={full ^ inc}")

    def test_confluent_benign_delta(self) -> None:
        base = self._confluence_schema() + [(EX.s1, EX.anc, EX.s2), (EX.s2, EX.anc, EX.s3)]
        self._assert_confluent(base, [(EX.n1, EX.anc, EX.n2)])  # 全新节点，不触及基线

    def test_confluent_transitive_extension_to_base(self) -> None:
        # 把基线传递链向后延伸一节 —— 朴素增量最容易漏的场景（需回溯 in_idx 补 s1/s2→s4）。
        base = self._confluence_schema() + [(EX.s1, EX.anc, EX.s2), (EX.s2, EX.anc, EX.s3)]
        full = native_reasoner.materialize_triples(set(base) | {(EX.s3, EX.anc, EX.s4)})
        self.assertIn((EX.s1, EX.anc, EX.s4), full)  # 确认场景非空转
        self._assert_confluent(base, [(EX.s3, EX.anc, EX.s4)])

    def test_confluent_inverse_to_base(self) -> None:
        # delta 主语是新节点，经 inverseOf 把值传回**基线** s1（s1 只作 delta 宾语出现）。
        base = self._confluence_schema() + [(EX.s1, EX.anc, EX.s2)]
        full = native_reasoner.materialize_triples(set(base) | {(EX.nx, EX.q, EX.s1)})
        self.assertIn((EX.s1, EX.p, EX.nx), full)  # 确认传回了基线
        self._assert_confluent(base, [(EX.nx, EX.q, EX.s1)])


if __name__ == "__main__":
    unittest.main()
