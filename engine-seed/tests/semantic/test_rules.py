from __future__ import annotations

import unittest
from pathlib import Path

try:
    from rdflib import Graph, Literal, Namespace, RDF
    from rdflib.namespace import XSD
except ModuleNotFoundError:
    Graph = None


ROOT = Path(__file__).resolve().parents[2]
SEMI = Namespace("urn:pxai:semi:") if Graph else None


@unittest.skipIf(Graph is None, "semantic dependencies not installed")
class BusinessRuleTest(unittest.TestCase):
    def test_impacted_lot_overlap(self) -> None:
        graph = Graph()
        graph.add((SEMI.down1, RDF.type, SEMI.DownEvent))
        graph.add((SEMI.down1, SEMI.affectsEquipment, SEMI.eqp1))
        graph.add((SEMI.down1, SEMI.validFrom, Literal("2026-08-29T01:00:00Z", datatype=XSD.dateTime)))
        graph.add((SEMI.down1, SEMI.validTo, Literal("2026-08-29T03:00:00Z", datatype=XSD.dateTime)))
        graph.add((SEMI.run1, RDF.type, SEMI.ProcessingEvent))
        graph.add((SEMI.run1, SEMI.usesEquipment, SEMI.eqp1))
        graph.add((SEMI.run1, SEMI.processesLot, SEMI.lot1))
        graph.add((SEMI.run1, SEMI.validFrom, Literal("2026-08-29T02:00:00Z", datatype=XSD.dateTime)))
        graph.add((SEMI.run1, SEMI.validTo, Literal("2026-08-29T04:00:00Z", datatype=XSD.dateTime)))
        inferred = Graph()
        for triple in graph.query((ROOT / "ontology" / "rules" / "impacted-lot.rq").read_text(encoding="utf-8")).graph:
            inferred.add(triple)
        self.assertIn((SEMI.down1, SEMI.impactsLot, SEMI.lot1), inferred)
        statements = set(inferred.subjects(RDF.predicate, SEMI.impactsLot))
        self.assertEqual(len(statements), 1)
        assertion = next(iter(statements))
        self.assertIn((assertion, SEMI.derivedFrom, SEMI.down1), inferred)
        self.assertIn((assertion, SEMI.derivedFrom, SEMI.run1), inferred)

    def test_root_cause_is_hypothesis(self) -> None:
        graph = Graph()
        graph.add((SEMI.cause1, SEMI.mayCause, SEMI.anomaly1))
        graph.add((SEMI.playbook1, RDF.type, SEMI.DiagnosticPlaybook))
        graph.add((SEMI.playbook1, SEMI.diagnosesAnomaly, SEMI.anomaly1))
        inferred = graph.query((ROOT / "ontology" / "rules" / "root-cause-hypothesis.rq").read_text(encoding="utf-8")).graph
        hypotheses = set(inferred.subjects(RDF.type, SEMI.RootCauseHypothesis))
        self.assertEqual(len(hypotheses), 1)
        hypothesis = next(iter(hypotheses))
        self.assertIn((hypothesis, SEMI.derivedFrom, SEMI.cause1), inferred)
        self.assertIn((hypothesis, SEMI.derivedFrom, SEMI.playbook1), inferred)
        self.assertFalse(any(inferred.subjects(RDF.type, SEMI.ConfirmedRootCause)))


if __name__ == "__main__":
    unittest.main()
