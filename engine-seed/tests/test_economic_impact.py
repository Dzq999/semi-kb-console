from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from economic_impact import generate_impact_proposal  # noqa: E402


class EconomicImpactProposalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "ontology/ap/entities").mkdir(parents=True)
        (self.root / "business/models").mkdir(parents=True)
        (self.root / "business/changesets/pending").mkdir(parents=True)
        self._write("ontology/ap/entities/new.yaml", {"entities": [{
            "id": "ap.cause.mold_contamination", "type": "Cause",
            "economic_hooks": {"affects": ["yield_rate", "scrap_rate", "capacity"]}
        }]})
        self._write("business/models/ap.yaml", {"model": {
            "id": "business.ap.general", "template_ref": "business/templates/ap.yaml",
            "dataset_ref": "business/datasets/demo.yaml", "outputs": ["profit"]
        }})

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, rel: str, data: dict) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def test_generates_data_request_without_inventing_numbers(self) -> None:
        path = generate_impact_proposal(self.root, "2026-08-28")
        self.assertIsNotNone(path)
        proposal = yaml.safe_load(path.read_text(encoding="utf-8"))
        items = proposal["business_impact_proposal"]["items"]
        item = next(x for x in items if x["ontology_ref"] == "ap.cause.mold_contamination")
        self.assertEqual(item["status"], "needs_human_input")
        self.assertIn("yield_rate", item["required_data"])
        self.assertNotIn("value", item)

    def test_does_not_duplicate_existing_proposal(self) -> None:
        first = generate_impact_proposal(self.root, "2026-08-28")
        second = generate_impact_proposal(self.root, "2026-08-28")
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
