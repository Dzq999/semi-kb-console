from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from simulate import ModelError, load_resolved_model, run_scenario, validate_project  # noqa: E402


class GeneralBusinessSimulationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for rel in ("business/templates", "business/models", "business/datasets",
                    "simulation/scenarios", "ontology/ap/entities"):
            (self.root / rel).mkdir(parents=True, exist_ok=True)
        self._write("ontology/ap/entities/test.yaml", {"entities": [
            {"id": "ap.anomaly.process_yield_loss", "type": "Anomaly"},
            {"id": "ap.action.optimize_process_window", "type": "Action"},
        ]})

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, rel: str, data: dict) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def _project(self, bad_ref: bool = False) -> None:
        self._write("business/templates/manufacturing.yaml", {"template": {
            "id": "template.manufacturing",
            "variables": [
                {"id": "input_units", "unit": "unit/month"},
                {"id": "process_yield", "unit": "ratio"},
                {"id": "selling_price", "unit": "CNY/unit"},
                {"id": "unit_cost", "unit": "CNY/unit"},
                {"id": "intervention_opex", "unit": "CNY/month"},
            ],
            "calculations": [
                {"id": "saleable_units", "algorithm": "production.saleable_output",
                 "inputs": ["input_units", "process_yield"], "unit": "unit/month"},
                {"id": "revenue", "algorithm": "finance.revenue",
                 "inputs": ["saleable_units", "selling_price"], "unit": "CNY/month"},
                {"id": "profit", "formula": "revenue - input_units * unit_cost - intervention_opex",
                 "unit": "CNY/month"},
            ],
        }})
        self._write("business/templates/semiconductor.yaml", {"template": {
            "id": "template.semiconductor", "extends": ["business/templates/manufacturing.yaml"],
            "variables": [
                {"id": "assembly_yield", "unit": "ratio"},
                {"id": "test_yield", "unit": "ratio"},
            ],
            "calculations": [
                {"id": "process_yield", "algorithm": "yield.cascade",
                 "inputs": ["assembly_yield", "test_yield"], "unit": "ratio", "override": True},
            ],
        }})
        self._write("business/templates/ap.yaml", {"template": {
            "id": "template.ap", "extends": ["business/templates/semiconductor.yaml"],
            "variables": [{"id": "rework_rate", "unit": "ratio"}],
            "calculations": [],
        }})
        self._write("business/datasets/demo.yaml", {"dataset": {
            "id": "dataset.demo", "values": [
                {"id": "input_units", "value": 1000, "source": "assumption"},
                {"id": "assembly_yield", "value": 0.9, "source": "assumption",
                 "ontology_ref": "ap.anomaly.unknown" if bad_ref else "ap.anomaly.process_yield_loss"},
                {"id": "test_yield", "value": 0.8, "source": "assumption"},
                {"id": "selling_price", "value": 10, "source": "assumption"},
                {"id": "unit_cost", "value": 5, "source": "assumption"},
                {"id": "intervention_opex", "value": 0, "source": "assumption"},
                {"id": "rework_rate", "value": 0.02, "source": "assumption"},
            ]}})
        self._write("business/models/ap.yaml", {"model": {
            "id": "business.ap.general", "domain": "ap", "period": "month", "currency": "CNY",
            "template_ref": "business/templates/ap.yaml",
            "dataset_ref": "business/datasets/demo.yaml",
            "engine": {"type": "deterministic"},
            "outputs": ["process_yield", "saleable_units", "revenue", "profit"],
        }})
        self._write("simulation/scenarios/improve.yaml", {"scenario": {
            "id": "sim.ap.improve", "model_ref": "business/models/ap.yaml",
            "interventions": [
                {"variable": "assembly_yield", "operation": "set", "value": 0.95,
                 "target_ref": "ap.anomaly.process_yield_loss"},
                {"variable": "intervention_opex", "operation": "set", "value": 100,
                 "target_ref": "ap.action.optimize_process_window"},
            ], "investment": {"one_time": 500, "unit": "CNY"},
        }})

    def test_resolves_inherited_templates_dataset_and_algorithm_registry(self) -> None:
        self._project()
        model = load_resolved_model(self.root / "business/models/ap.yaml", self.root)

        self.assertEqual(model["id"], "business.ap.general")
        self.assertEqual(model["values"]["input_units"]["value"], 1000)
        self.assertEqual(model["calculations"][0]["id"], "saleable_units")
        self.assertEqual(model["calculations"][-1]["id"], "process_yield")

    def test_scenario_uses_general_ap_model_not_process_specific_model(self) -> None:
        self._project()
        result = run_scenario(self.root / "simulation/scenarios/improve.yaml", root=self.root)

        self.assertEqual(result["baseline"]["process_yield"]["value"], 0.72)
        self.assertEqual(result["baseline"]["profit"]["value"], 2200.0)
        self.assertEqual(result["intervention"]["profit"]["value"], 2500.0)
        self.assertEqual(result["delta"]["profit"]["value"], 300.0)
        self.assertAlmostEqual(result["payback_periods"], 1.666667)
        self.assertEqual(result["evidence_grade"], "assumption_only")

    def test_project_validation_rejects_broken_ontology_reference(self) -> None:
        self._project(bad_ref=True)
        issues = validate_project(self.root)
        self.assertTrue(any("本体引用不存在" in issue for issue in issues))

    def test_rejects_unknown_algorithm(self) -> None:
        self._project()
        path = self.root / "business/templates/manufacturing.yaml"
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        doc["template"]["calculations"][0]["algorithm"] = "magic.answer"
        self._write("business/templates/manufacturing.yaml", doc)
        with self.assertRaisesRegex(ModelError, "未知算法"):
            run_scenario(self.root / "simulation/scenarios/improve.yaml", root=self.root)

    def test_rejects_template_cycle(self) -> None:
        self._project()
        path = self.root / "business/templates/manufacturing.yaml"
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        doc["template"]["extends"] = ["business/templates/ap.yaml"]
        self._write("business/templates/manufacturing.yaml", doc)
        with self.assertRaisesRegex(ModelError, "模板继承成环"):
            load_resolved_model(self.root / "business/models/ap.yaml", self.root)


if __name__ == "__main__":
    unittest.main()
