"""Cert A（慢、opt-in）：真实 52K 基线上 native 与 owlrl 的**门禁裁决逐项相等** +
对抗 delta 区分度。这是把默认切到 native 之前的**决定性验收**。

owlrl 全图闭包 ~87s/次，故本文件不进每轮门禁，仅在显式 `SEMANTIC_REALBASELINE=1` 时运行：

    SEMANTIC_REALBASELINE=1 python -m pytest tests/semantic/test_native_equivalence_realbaseline.py -q

做法：复刻 semantic_validate.py 的输入集与裁决管线（闭包前采显式 target 并改写
targetClass→targetNode，两个推理器共用同一套 target，令被验节点与推理器无关），对每条
delta 分别在 native 闭包与 owlrl 闭包上跑「271 规则 + pyshacl(inference=none) + 安全检查」，
断言三元裁决 (conforms, 违规集, safety) **逐项相等**；并断言良性 delta 两侧都 PASS、对抗
delta（含只在闭包后才显现的 inverse 传播型）两侧都 FAIL —— 确保等价不是空转。
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

try:
    from rdflib import Dataset, Graph, Literal, Namespace, RDF, URIRef
    from rdflib.namespace import XSD
    from owlrl import DeductiveClosure, OWLRL_Semantics
    from pyshacl import validate as shacl_validate
    import native_reasoner  # noqa: E402
except ModuleNotFoundError:
    Graph = None

SEMI = Namespace("urn:pxai:semi:") if Graph else None
_SH_TARGET_CLASS = URIRef("http://www.w3.org/ns/shacl#targetClass") if Graph else None
_SH_TARGET_NODE = URIRef("http://www.w3.org/ns/shacl#targetNode") if Graph else None
_SH_RESULT = URIRef("http://www.w3.org/ns/shacl#ValidationResult") if Graph else None
_SH_FOCUS = URIRef("http://www.w3.org/ns/shacl#focusNode") if Graph else None
_SH_SOURCE = URIRef("http://www.w3.org/ns/shacl#sourceConstraintComponent") if Graph else None
_CONFIRMED = URIRef("urn:pxai:semi:ConfirmedRootCause") if Graph else None

_ENABLED = os.getenv("SEMANTIC_REALBASELINE", "0").casefold() in {"1", "true", "yes", "on"}


def _load_schema() -> "Graph":
    g = Graph()
    for path in sorted((ROOT / "ontology" / "modules").glob("*.ttl")):
        g.parse(path, format="turtle")
    return g


def _load_shapes() -> "Graph":
    g = Graph()
    for path in sorted((ROOT / "ontology" / "shapes").glob("*.ttl")):
        g.parse(path, format="turtle")
    return g


def _load_rules() -> list[str]:
    import json
    registry = json.loads((ROOT / "ontology" / "rules" / "registry.json").read_text(encoding="utf-8"))
    texts: list[str] = []
    for rule in registry.get("rules") or []:
        impl = rule.get("implementation")
        if isinstance(impl, str) and impl.endswith(".rq"):
            path = ROOT / impl
            if path.is_file():
                try:
                    Graph().query(path.read_text(encoding="utf-8"))
                    texts.append(path.read_text(encoding="utf-8"))
                except Exception:
                    pass
    return texts


def _load_base(schema: "Graph") -> "Graph":
    data = Graph()
    ds = Dataset()
    ds.parse(ROOT / "build" / "semantic" / "current.trig", format="trig")
    for quad in ds.quads((None, None, None, None)):
        data.add(quad[:3])
    for path in sorted((ROOT / "knowledge" / "semantic").glob("*.ttl")):
        data.parse(path, format="turtle")
    data += schema
    return data


def _shapes_targetnode(shapes: "Graph", abox: "Graph") -> "Graph":
    """复刻 semantic_validate：闭包前把 sh:targetClass 改写为显式直接实例的 sh:targetNode。"""
    g = Graph()
    for t in shapes:
        g.add(t)
    for shape, _, cls in list(g.triples((None, _SH_TARGET_CLASS, None))):
        for node in set(abox.subjects(RDF.type, cls)):
            g.add((shape, _SH_TARGET_NODE, node))
        g.remove((shape, _SH_TARGET_CLASS, cls))
    return g


def _verdict(closed: set, rules: list[str], shapes_tn: "Graph", schema: "Graph") -> tuple:
    g = Graph()
    for t in closed:
        g.add(t)
    for q in rules:
        try:
            res = g.query(q)
            cg = getattr(res, "graph", None)
            if cg is not None:
                for t in cg:
                    if t not in g:
                        g.add(t)
        except Exception:
            pass
    conforms, results_graph, _ = shacl_validate(
        data_graph=g, shacl_graph=shapes_tn, ont_graph=schema,
        inference="none", advanced=True)
    viol = set()
    for r in results_graph.subjects(RDF.type, _SH_RESULT):
        viol.add((str(results_graph.value(r, _SH_FOCUS)), str(results_graph.value(r, _SH_SOURCE))))
    safety_ok = not any(True for _ in g.triples((None, RDF.type, _CONFIRMED)))
    return conforms, frozenset(viol), safety_ok


def _deltas() -> "dict[str, tuple[set, str]]":
    """delta 名 -> (三元组集, 期望裁决 pass/fail)。对抗项自足触发、必挂。"""
    E = lambda name: URIRef("urn:pxai:semi:ind.NativeCert." + name)
    s = lambda code: Literal(code, datatype=XSD.string)
    return {
        "benign_equipment": ({
            (E("EqOK"), RDF.type, SEMI.Equipment),
            (E("EqOK"), SEMI.equipmentCode, s("NATIVE-CERT-OK")),
        }, "pass"),
        "adv_missing_required": ({
            (E("EqBad"), RDF.type, SEMI.Equipment),  # 缺 equipmentCode → EquipmentShape minCount
        }, "fail"),
        "adv_cardinality_direct": ({
            (E("ChBad"), RDF.type, SEMI.Chamber),
            (E("ChBad"), SEMI.chamberOf, E("EqA")),
            (E("ChBad"), SEMI.chamberOf, E("EqB")),  # 两个 chamberOf → maxCount 1
        }, "fail"),
        "adv_cardinality_via_inverse": ({
            (E("ChInv"), RDF.type, SEMI.Chamber),
            (E("ChInv"), SEMI.chamberOf, E("EqC")),
            (E("EqD"), SEMI.hasChamber, E("ChInv")),  # hasChamber⁻¹=chamberOf → 闭包后 ChInv 得第二个 chamberOf
        }, "fail"),
        "adv_processing_event_untyped": ({
            (E("PEBad"), RDF.type, SEMI.ProcessingEvent),  # 缺 processesLot/Wafer 等 → sh:or + 必填
        }, "fail"),
    }


@unittest.skipUnless(_ENABLED, "opt-in 慢测；置 SEMANTIC_REALBASELINE=1 运行")
@unittest.skipIf(Graph is None, "semantic dependencies not installed")
class RealBaselineEquivalenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = _load_schema()
        cls.shapes = _load_shapes()
        cls.rules = _load_rules()
        cls.base = _load_base(cls.schema)
        cls.base_set = set(cls.base)

    def _run_case(self, delta: set):
        abox = Graph()
        for t in self.base_set | delta:
            abox.add(t)
        shapes_tn = _shapes_targetnode(self.shapes, abox)
        combined = self.base_set | delta
        native_closed = native_reasoner.materialize_triples(combined)
        og = Graph()
        for t in combined:
            og.add(t)
        DeductiveClosure(OWLRL_Semantics).expand(og)
        v_native = _verdict(native_closed, self.rules, shapes_tn, self.schema)
        v_owlrl = _verdict(set(og), self.rules, shapes_tn, self.schema)
        return v_native, v_owlrl

    def test_baseline_verdict_equal_and_passes(self) -> None:
        v_native, v_owlrl = self._run_case(set())
        self.assertEqual(v_native, v_owlrl, "基线裁决 native≠owlrl")
        self.assertTrue(v_native[0] and v_native[2], "基线应 PASS")

    def test_each_delta_verdict_equal_and_discriminating(self) -> None:
        for name, (delta, expected) in _deltas().items():
            with self.subTest(delta=name):
                v_native, v_owlrl = self._run_case(delta)
                # 决定性断言：native 与 owlrl 三元裁决逐项相等
                self.assertEqual(v_native, v_owlrl, f"[{name}] native≠owlrl: {v_native} vs {v_owlrl}")
                # 区分度：良性两侧 PASS、对抗两侧 FAIL（否则等价空转）
                native_pass = v_native[0] and v_native[2]
                self.assertEqual("pass" if native_pass else "fail", expected,
                                 f"[{name}] 期望 {expected}，native 得 {'pass' if native_pass else 'fail'}")


if __name__ == "__main__":
    unittest.main()
