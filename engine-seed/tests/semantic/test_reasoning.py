from __future__ import annotations

import unittest
from pathlib import Path

try:
    from rdflib import BNode, Graph, Literal, Namespace, RDF, URIRef
    from rdflib.namespace import OWL
    from owlrl import DeductiveClosure, OWLRL_Semantics
except ModuleNotFoundError:  # 依赖检查由 semantic_validate.py 强制；单测收集阶段可跳过
    Graph = None


ROOT = Path(__file__).resolve().parents[2]
SEMI = Namespace("urn:pxai:semi:") if Graph else None


@unittest.skipIf(Graph is None, "semantic dependencies not installed")
class OntologyReasoningTest(unittest.TestCase):
    def schema(self) -> Graph:
        graph = Graph()
        for path in (ROOT / "ontology" / "modules").glob("*.ttl"):
            graph.parse(path, format="turtle")
        return graph

    def test_chamber_is_many_per_equipment_and_one_equipment_per_chamber(self) -> None:
        graph = self.schema()
        restrictions = list(graph.objects(SEMI.Chamber, URIRef("http://www.w3.org/2000/01/rdf-schema#subClassOf")))
        chamber_of = URIRef("urn:pxai:semi:chamberOf")
        self.assertTrue(any((node, OWL.onProperty, chamber_of) in graph and
                            any(int(value) == 1 for value in graph.objects(node, OWL.qualifiedCardinality))
                            for node in restrictions if isinstance(node, BNode)))
        self.assertNotIn((SEMI.hasChamber, RDF.type, OWL.FunctionalProperty), graph)

    def test_inverse_has_chamber(self) -> None:
        graph = self.schema()
        chamber, equipment = SEMI.testChamber, SEMI.testEquipment
        graph.add((chamber, SEMI.chamberOf, equipment))
        DeductiveClosure(OWLRL_Semantics).expand(graph)
        self.assertIn((equipment, SEMI.hasChamber, chamber), graph)

    def test_facility_property_chain(self) -> None:
        graph = self.schema()
        equipment, area, fab = SEMI.testEquipment, SEMI.testArea, SEMI.testFab
        graph.add((equipment, SEMI.locatedIn, area))
        graph.add((area, SEMI.belongsToFab, fab))
        DeductiveClosure(OWLRL_Semantics).expand(graph)
        self.assertIn((equipment, SEMI.belongsToFab, fab), graph)

    def test_precedes_transitive(self) -> None:
        graph = self.schema()
        graph.add((SEMI.stepA, SEMI.directlyPrecedes, SEMI.stepB))
        graph.add((SEMI.stepB, SEMI.directlyPrecedes, SEMI.stepC))
        DeductiveClosure(OWLRL_Semantics).expand(graph)
        self.assertIn((SEMI.stepA, SEMI.precedes, SEMI.stepC), graph)


if __name__ == "__main__":
    unittest.main()
