"""确认问题域依赖的 OWL 类均已声明，防止只有问题清单、没有本体支撑。"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try: stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError): pass
    doc = json.loads((ROOT / "ontology" / "capability-questions.json").read_text(encoding="utf-8"))
    application = json.loads((ROOT / "ontology" / "application-capabilities.json").read_text(encoding="utf-8"))
    class_pattern = re.compile(r"\bsemi:([A-Za-z][A-Za-z0-9_-]*)\s+a\s+owl:Class")
    relation_pattern = re.compile(r"\bsemi:([A-Za-z][A-Za-z0-9_-]*)\s+a\s+(?:owl:ObjectProperty|owl:TransitiveProperty|owl:FunctionalProperty)")
    declared: set[str] = set()
    relations: set[str] = set()
    for path in (ROOT / "ontology" / "modules").glob("*.ttl"):
        text = path.read_text(encoding="utf-8")
        declared.update(class_pattern.findall(text))
        relations.update(relation_pattern.findall(text))
    seen: set[str] = set()
    errors: list[str] = []
    for domain in doc.get("domains") or []:
        did = domain.get("id")
        if not did or did in seen:
            errors.append(f"问题域 ID 缺失或重复：{did}")
        seen.add(did)
        if not domain.get("question"):
            errors.append(f"{did} 缺少 question")
        missing = sorted(set(domain.get("classes") or []) - declared)
        if missing:
            errors.append(f"{did} 依赖未声明 OWL 类：{', '.join(missing)}")
        missing_relations = sorted(set(domain.get("relations") or []) - relations)
        if missing_relations:
            errors.append(f"{did} 依赖未声明 OWL 关系：{', '.join(missing_relations)}")
    if len(seen) < 11:
        errors.append(f"企业问题域不得少于 11 个，当前 {len(seen)} 个")
    l1_ids = [item.get("id") for item in application.get("l1_capabilities") or []]
    if len(l1_ids) != len(set(l1_ids)) or any(not item for item in l1_ids):
        errors.append("L1 能力 ID 缺失或重复")
    l2_ids: set[str] = set()
    for scenario in application.get("l2_scenarios") or []:
        sid = scenario.get("id")
        if not sid or sid in l2_ids:
            errors.append(f"L2 场景 ID 缺失或重复：{sid}")
        l2_ids.add(sid)
        missing_capabilities = sorted(set(scenario.get("composes") or []) - set(l1_ids))
        if missing_capabilities:
            errors.append(f"{sid} 引用不存在 L1 能力：{', '.join(missing_capabilities)}")
        if not scenario.get("composes"):
            errors.append(f"{sid} 未编排任何 L1 能力")
    if errors:
        for error in errors:
            print("ERROR: " + error, file=sys.stderr)
        return 1
    print(f"能力校验通过：问题域 {len(seen)} | L1 {len(l1_ids)} | L2 {len(l2_ids)} | OWL 依赖可解析")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
