"""校验 OWL、SHACL、推理规则、生成数据集和来源对齐。"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def dependency_error(name: str) -> int:
    report = {
        "status": "blocked",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "issues": [{"severity": "error", "code": "SEM-DEP", "message": f"缺少依赖 {name}；请安装 requirements.txt"}],
    }
    out = ROOT / "build" / "reports" / "semantic-validation.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(report["issues"][0]["message"], file=sys.stderr)
    return 2


def main() -> int:
    setup_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-inference", action="store_true")
    args = ap.parse_args()
    try:
        from rdflib import Dataset, Graph, URIRef
        from owlrl import DeductiveClosure, OWLRL_Semantics
        from pyshacl import validate as shacl_validate
    except ModuleNotFoundError as exc:
        return dependency_error(exc.name or "semantic-runtime")

    issues: list[dict] = []
    schema = Graph()
    module_files = sorted((ROOT / "ontology" / "modules").glob("*.ttl"))
    for path in module_files:
        try:
            schema.parse(path, format="turtle")
        except Exception as exc:  # noqa: BLE001
            issues.append({"severity": "error", "code": "SEM-TTL", "file": path.relative_to(ROOT).as_posix(), "message": str(exc)})

    shapes = Graph()
    for path in sorted((ROOT / "ontology" / "shapes").glob("*.ttl")):
        try:
            shapes.parse(path, format="turtle")
        except Exception as exc:  # noqa: BLE001
            issues.append({"severity": "error", "code": "SEM-SHAPE", "file": path.relative_to(ROOT).as_posix(), "message": str(exc)})

    dataset_path = ROOT / "build" / "semantic" / "current.trig"
    dataset = Dataset()
    if dataset_path.is_file():
        try:
            dataset.parse(dataset_path, format="trig")
        except Exception as exc:  # noqa: BLE001
            issues.append({"severity": "error", "code": "SEM-DATA", "file": dataset_path.relative_to(ROOT).as_posix(), "message": str(exc)})
    else:
        issues.append({"severity": "error", "code": "SEM-DATA", "message": "缺少 build/semantic/current.trig；先运行 migrate_semantic.py"})

    registry_path = ROOT / "ontology" / "rules" / "registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    ids: set[str] = set()
    executable_rules: list[tuple[str, Path]] = []
    for rule in registry.get("rules") or []:
        rid = rule.get("rule_id")
        if not rid or rid in ids:
            issues.append({"severity": "error", "code": "SEM-RULE-ID", "message": f"规则 ID 缺失或重复：{rid}"})
        ids.add(rid)
        impl = rule.get("implementation")
        if isinstance(impl, str) and impl.endswith(".rq"):
            path = ROOT / impl
            if not path.is_file():
                issues.append({"severity": "error", "code": "SEM-RULE-PATH", "message": f"规则文件不存在：{impl}"})
            else:
                try:
                    Graph().query(path.read_text(encoding="utf-8"))
                    executable_rules.append((rid, path))
                except Exception as exc:  # noqa: BLE001
                    issues.append({"severity": "error", "code": "SEM-RULE-PARSE", "file": impl, "message": str(exc)})
        for test_ref in rule.get("tests") or []:
            test_path = ROOT / str(test_ref).split("::", 1)[0]
            if not test_path.is_file():
                issues.append({"severity": "error", "code": "SEM-RULE-TEST", "message": f"规则测试不存在：{test_ref}"})

    for manifest_path in sorted((ROOT / "sources" / "internal").glob("*/manifest.json")):
        if manifest_path.parent.name == "vfab":
            continue  # vFab 为多数据集 manifest，由 vfab_ingest.py 按交付契约校验。
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        canonical_file = manifest.get("canonical_file")
        expected_hash = manifest.get("sha256") or manifest.get("source_sha256")
        if not canonical_file or not expected_hash:
            issues.append({"severity": "error", "code": "SEM-SOURCE-MANIFEST",
                           "file": manifest_path.relative_to(ROOT).as_posix(),
                           "message": "内部来源 manifest 缺少 canonical_file 或 SHA-256"})
            continue
        source_path = ROOT / canonical_file
        actual_hash = hashlib.sha256(source_path.read_bytes()).hexdigest() if source_path.is_file() else None
        if actual_hash != expected_hash:
            issues.append({"severity": "error", "code": "SEM-SOURCE-HASH",
                           "file": manifest_path.relative_to(ROOT).as_posix(),
                           "message": f"内部来源缺失或哈希不匹配：{canonical_file}"})

    data = Graph()
    for quad in dataset.quads((None, None, None, None)):
        data.add(quad[:3])
    abox_triples = 0
    for path in sorted((ROOT / "knowledge" / "semantic").glob("*.ttl")):
        try:
            before = len(data)
            data.parse(path, format="turtle")
            abox_triples += len(data) - before
        except Exception as exc:  # noqa: BLE001
            issues.append({"severity": "error", "code": "SEM-ABOX", "file": path.relative_to(ROOT).as_posix(), "message": str(exc)})
    data += schema
    inferred = 0
    if not args.no_inference and not issues:
        before = len(data)
        DeductiveClosure(OWLRL_Semantics).expand(data)
        inferred = len(data) - before
    rule_triples = 0
    rule_counts: dict[str, int] = {}
    if not issues:
        for rid, path in executable_rules:
            try:
                result = data.query(path.read_text(encoding="utf-8"))
                constructed = getattr(result, "graph", None)
                count = 0
                if constructed is not None:
                    for triple in constructed:
                        if triple not in data:
                            data.add(triple); count += 1
                rule_counts[rid] = count
                rule_triples += count
            except Exception as exc:  # noqa: BLE001
                issues.append({"severity": "error", "code": "SEM-RULE-RUN", "file": path.relative_to(ROOT).as_posix(), "message": str(exc)})
    if not issues:
        try:
            conforms, _, results_text = shacl_validate(data_graph=data, shacl_graph=shapes, ont_graph=schema, inference="none", advanced=True)
            if not conforms:
                issues.append({"severity": "error", "code": "SEM-SHACL", "message": results_text})
        except Exception as exc:  # noqa: BLE001
            issues.append({"severity": "error", "code": "SEM-SHACL-RUN", "message": str(exc)})

    # 任何自动根因输出都必须仍是 Hypothesis，不能成为确认根因类型。
    forbidden = URIRef("urn:pxai:semi:ConfirmedRootCause")
    from rdflib import RDF
    if any(True for _ in data.triples((None, RDF.type, forbidden))):
        issues.append({"severity": "error", "code": "SEM-SAFETY", "message": "检测到自动生成的 ConfirmedRootCause"})

    report = {
        "status": "pass" if not issues else "fail",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "modules": len(module_files),
        "schema_triples": len(schema),
        "data_quads": len(dataset),
        "abox_triples": abox_triples,
        "inferred_triples": inferred,
        "rule_generated_triples": rule_triples,
        "rule_counts": rule_counts,
        "rules": len(ids),
        "issues": issues,
    }
    out = ROOT / "build" / "reports" / "semantic-validation.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if issues:
        for issue in issues:
            print(f"ERROR {issue['code']}: {issue['message']}", file=sys.stderr)
        return 1
    print(f"语义校验通过：模块 {len(module_files)} | 规则 {len(ids)} | 推理新增 {inferred}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
