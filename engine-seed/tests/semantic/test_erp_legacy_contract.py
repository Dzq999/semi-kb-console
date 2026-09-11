"""ERP legacy YAML 契约：财务影响词闭集与 KB ID/domain 一致性。"""
from __future__ import annotations

import copy
import importlib.util
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

spec = importlib.util.spec_from_file_location("legacy_validate", SCRIPTS / "validate.py")
legacy_validate = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(legacy_validate)
import common as C  # noqa: E402

MANUFACTURING_HOOKS = {
    "yield", "cycle_time", "equipment_oee", "throughput", "cost_per_unit",
    "rework_rate", "scrap_rate", "capacity_commit",
}
ERP_HOOKS = {
    "unit_cost", "gross_margin", "asset_value", "period_expense",
    "inventory_value", "payables_accuracy", "period_close",
    "revenue_recognition", "dso",
}


class ErpLegacyContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.meta = yaml.safe_load((ROOT / "ontology" / "meta-schema.yaml").read_text(encoding="utf-8"))
        cls.entities, cls.duplicates = C.load_entities()
        cls.cases = C.load_kb()

    def setUp(self) -> None:
        legacy_validate.ISSUES.clear()

    @staticmethod
    def issues(rule: str) -> list[tuple[str, str, str]]:
        return [item for item in legacy_validate.ISSUES if item[1] == rule]

    def test_hook_vocabulary_is_exact_closed_union(self) -> None:
        actual = set(self.meta["economic_hooks_schema"]["affects"]["allowed"])
        self.assertEqual(MANUFACTURING_HOOKS | ERP_HOOKS, actual)

    def test_existing_erp_entities_and_cases_satisfy_legacy_contract(self) -> None:
        legacy_validate.check_entities(self.entities, self.duplicates, self.meta)
        legacy_validate.check_kb(self.entities, self.cases, self.meta)
        self.assertEqual([], self.issues("R011"))
        self.assertEqual([], self.issues("R013"))
        erp_cases = [case for case in self.cases if str(case.get("id", "")).startswith("kb.erp.")]
        self.assertEqual(6, len(erp_cases))

    def test_unknown_financial_hook_is_rejected(self) -> None:
        entity = copy.deepcopy(self.entities["erp.anomaly.cost_variance_excursion"])
        entity["economic_hooks"] = {"affects": ["ebitda"]}
        legacy_validate.check_entities({entity["id"]: entity}, [], self.meta)
        messages = [item[2] for item in self.issues("R011")]
        self.assertTrue(any("ebitda" in message for message in messages))

    def test_unknown_or_malformed_kb_domains_are_rejected(self) -> None:
        base = copy.deepcopy(next(case for case in self.cases if case["id"].startswith("kb.erp.")))
        for invalid_id in ("kb.crm.example", "kb.ERP.example", "kb.erp.example.extra"):
            with self.subTest(invalid_id=invalid_id):
                legacy_validate.ISSUES.clear()
                case = copy.deepcopy(base)
                case["id"] = invalid_id
                legacy_validate.check_kb(self.entities, [case], self.meta)
                self.assertTrue(self.issues("R013"))

    def test_kb_id_domain_must_match_case_domain(self) -> None:
        case = copy.deepcopy(next(case for case in self.cases if case["id"].startswith("kb.erp.")))
        case["domain"] = "fab"
        legacy_validate.check_kb(self.entities, [case], self.meta)
        messages = [item[2] for item in self.issues("R013")]
        self.assertTrue(any("与 KB ID 域 'erp' 不一致" in message for message in messages))


if __name__ == "__main__":
    unittest.main()
