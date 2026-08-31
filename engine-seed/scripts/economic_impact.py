"""从新增本体经济影响钩子生成待人工审核的数据/模型提案。"""
from __future__ import annotations

from pathlib import Path
import yaml


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def generate_impact_proposal(root: Path, date: str) -> Path | None:
    root = Path(root)
    known_hooks = []
    for path in sorted((root / "ontology").glob("*/entities/*.yaml")):
        for entity in _load(path).get("entities", []):
            hooks = entity.get("economic_hooks") or {}
            affects = hooks.get("affects", []) if isinstance(hooks, dict) else []
            if affects:
                known_hooks.append((entity["id"], affects))
    models = []
    for path in sorted((root / "business" / "models").glob("*.yaml")):
        models.append(str(path.relative_to(root)))
    if not known_hooks or not models:
        return None
    pending = root / "business" / "changesets" / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    target = pending / f"{date.replace('-', '')}-economic-impact-candidates.yaml"
    if target.exists():
        return target
    items = []
    for ontology_ref, affects in known_hooks:
        items.append({
            "ontology_ref": ontology_ref,
            "model_refs": models,
            "economic_hooks": affects,
            "required_data": affects,
            "status": "needs_human_input",
            "note": "只提出需要补充或确认的数据，不自动编造量化值；确认后再写入 business/datasets/ 或 simulation/scenarios/。",
        })
    target.write_text(yaml.safe_dump({
        "schema_version": "1.0",
        "business_impact_proposal": {
            "id": f"economic-impact-candidates-{date}",
            "created_at": date,
            "items": items,
        },
    }, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return target
