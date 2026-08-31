"""对齐内部特征模型与 vFab 契约，并输出可机器消费的覆盖状态。"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEMI = "urn:pxai:semi:"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try: stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError): pass


def declared_terms() -> set[str]:
    terms: set[str] = set()
    pattern = re.compile(r"\bsemi:([A-Za-z][A-Za-z0-9_-]*)\s+a\s+(?:owl:Class|owl:ObjectProperty|owl:DatatypeProperty)")
    for path in sorted((ROOT / "ontology" / "modules").glob("*.ttl")):
        terms.update(SEMI + value for value in pattern.findall(path.read_text(encoding="utf-8")))
    return terms


def main() -> int:
    setup_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    catalog_path = ROOT / "build" / "source" / "feature-model-catalog.json"
    if not catalog_path.is_file():
        print("缺少标准化特征目录；先运行 scripts/source_ingest.py。", file=sys.stderr)
        return 1

    catalog = load(catalog_path)
    mapping_doc = load(ROOT / "mappings" / "feature-model" / "entity-map.json")
    property_doc = load(ROOT / "mappings" / "feature-model" / "property-map.json")
    source_codes = {str(x.get("code")) for x in catalog.get("entities") or [] if x.get("code")}
    rows = mapping_doc.get("mappings") or []
    mapped_codes = {str(x.get("source_code")) for x in rows if x.get("source_code")}
    terms = declared_terms()
    missing_mapping = sorted(source_codes - mapped_codes)
    stale_mapping = sorted(mapped_codes - source_codes)
    missing_target = sorted({str(x.get("target_class")) for x in rows if x.get("target_class") not in terms})
    property_lookup: dict[str, str] = {}
    duplicate_property_codes: list[str] = []
    for mapping in property_doc.get("mappings") or []:
        for code in mapping.get("feature_codes") or []:
            normalized = str(code).strip().lower()
            if normalized in property_lookup and property_lookup[normalized] != mapping.get("target_property"):
                duplicate_property_codes.append(normalized)
            property_lookup[normalized] = mapping.get("target_property")
    missing_property_targets = sorted({target for target in property_lookup.values() if target not in terms})

    vfab_contract = load(ROOT / "sources" / "internal" / "vfab" / "contract" / "schema-contract.json")
    vfab_catalog_path = ROOT / "build" / "source" / "vfab-catalog.json"
    vfab_catalog = load(vfab_catalog_path) if vfab_catalog_path.is_file() else {"status": "awaiting_source", "datasets": []}
    vfab_state = vfab_catalog.get("status", "awaiting_source")
    if vfab_state == "fail":
        print("vFab 标准化失败；先运行 scripts/vfab_ingest.py 查看错误。", file=sys.stderr)
        return 1
    vfab_targets = {x.get("target_class") for x in vfab_catalog.get("datasets") or []}
    invalid_vfab_targets = sorted(x for x in vfab_targets if x and x not in terms)

    mapped_feature_rows = 0
    property_mapped_rows = 0
    feature_rows = 0
    unmapped_features: collections.Counter[str] = collections.Counter()
    internal_feature_codes: set[str] = set()
    aliases: dict[str, str] = {}
    for entity in catalog.get("entities") or []:
        code = str(entity.get("code") or "").strip().lower()
        for raw in (entity.get("code"), entity.get("name")):
            key = " ".join(str(raw or "").strip().lower().split())
            if key:
                aliases[key] = code
    for raw, code in (mapping_doc.get("sheet_aliases") or {}).items():
        aliases[" ".join(str(raw).strip().lower().split())] = str(code)
    supplementary_sheets: list[dict] = []
    for sheet in catalog.get("sheets") or []:
        count = len(sheet.get("features") or [])
        feature_rows += count
        name = " ".join(str(sheet.get("name") or "").strip().lower().split())
        code = aliases.get(name)
        if code in mapped_codes:
            mapped_feature_rows += count
        elif count:
            supplementary_sheets.append({"sheet": sheet.get("name"), "feature_rows": count})
        for feature in sheet.get("features") or []:
            feature_code = str(feature.get("feature_code") or "").strip().lower()
            if feature_code: internal_feature_codes.add(feature_code)
            if feature_code in property_lookup:
                property_mapped_rows += 1
            else:
                unmapped_features[feature_code or "<missing_feature_code>"] += 1

    status = "fail" if (missing_mapping or stale_mapping or missing_target or invalid_vfab_targets
                         or missing_property_targets or duplicate_property_codes) else "pass"
    vfab_headers = {str(header).strip().lower() for dataset in vfab_catalog.get("datasets") or []
                    for header in dataset.get("headers") or [] if str(header).strip()}
    shared_vfab_features = sorted(internal_feature_codes & vfab_headers)
    alignments = []
    for row in rows:
        alignments.append({
            "source": catalog.get("source_id"),
            "source_code": row["source_code"],
            "target_class": row["target_class"],
            "internal_feature_state": "single_source_confirmed",
            "vfab_state": vfab_state,
            "combined_state": "confirmed" if row["target_class"] in vfab_targets else "partially_supported",
        })
    report = {
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "internal_feature_model": {
            "source_sha256": catalog.get("source_sha256"),
            "themes": len(catalog.get("themes") or []),
            "entities": len(source_codes),
            "tables": len(catalog.get("tables") or []),
            "feature_rows": feature_rows,
            "mapped_entities": len(source_codes & mapped_codes),
            "entity_aligned_feature_rows": mapped_feature_rows,
            "property_mapped_feature_rows": property_mapped_rows,
            "property_unmapped_feature_rows": feature_rows - property_mapped_rows,
            "property_mapping_coverage": round(property_mapped_rows / feature_rows, 6) if feature_rows else 1.0,
        },
        "vfab": {
            "state": vfab_state,
            "contract_id": vfab_contract.get("$id"),
            "datasets": len(vfab_catalog.get("datasets") or []),
            "fields": len(vfab_headers),
            "fields_matching_internal_feature": len(shared_vfab_features),
            "matching_feature_codes": shared_vfab_features,
            "note": "等待 vFab 实际资料，当前不作双源确认。" if vfab_state == "awaiting_source" else "已发现 vFab 源清单，进入字段级校验。",
        },
        "issues": {
            "unmapped_source_entities": missing_mapping,
            "stale_mappings": stale_mapping,
            "undeclared_target_terms": missing_target,
            "undeclared_vfab_targets": invalid_vfab_targets,
            "undeclared_property_targets": missing_property_targets,
            "duplicate_property_codes": sorted(set(duplicate_property_codes)),
            "supplementary_feature_sheets": supplementary_sheets,
            "unmapped_source_features": [{"feature_code": code, "rows": count}
                                           for code, count in unmapped_features.most_common()],
        },
        "alignments": alignments,
    }
    out = ROOT / "build" / "reports" / "source-alignment.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if status == "pass":
        print(f"来源对齐通过：内部实体 {len(source_codes)} 全覆盖 | vFab {vfab_state}")
        return 0
    print("来源对齐失败：" + json.dumps(report["issues"], ensure_ascii=False), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
