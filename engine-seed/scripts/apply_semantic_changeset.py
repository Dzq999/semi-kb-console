"""校验并原子合并 OWL/ABox JSON 变更集。"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
PENDING = ROOT / "semantic_changesets" / "pending"
SCHEMA_TARGET = ROOT / "ontology" / "modules" / "generated.ttl"
DATA_TARGET = ROOT / "knowledge" / "semantic" / "current.ttl"


def fail_dependency(name: str) -> int:
    print(f"缺少语义变更依赖 {name}；请安装 requirements.txt。", file=sys.stderr)
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(description="校验并合并 OWL/ABox 语义变更集")
    parser.add_argument("--check", action="store_true", help="只预检，不写入")
    parser.add_argument("--defer-archive", action="store_true", help="合并后保留在 pending，由全链入口归档")
    parser.add_argument("--semantic-only", action="store_true", help="只跑语义校验；仅供随后执行全链的事务入口使用")
    args = parser.parse_args()
    try:
        import jsonschema
        import yaml
        from rdflib import BNode, Dataset, Graph, Literal, RDF, RDFS, URIRef
        from rdflib.namespace import OWL, PROV, XSD
    except ModuleNotFoundError as exc:
        return fail_dependency(exc.name or "semantic-runtime")
    check_only = args.check
    files = sorted(PENDING.glob("*.json"))
    if not files:
        print("semantic_changesets/pending/ 下没有待处理语义提案。")
        return 0
    contract = json.loads((ROOT / "output-contracts" / "semantic-changeset.schema.json").read_text(encoding="utf-8"))
    schema_graph, data_graph, full_schema = Graph(), Graph(), Graph()
    schema_graph.parse(SCHEMA_TARGET, format="turtle")
    data_graph.parse(DATA_TARGET, format="turtle")
    for module in sorted((ROOT / "ontology" / "modules").glob("*.ttl")):
        full_schema.parse(module, format="turtle")
    seen = set(full_schema.subjects()) | set(data_graph.subjects())
    baseline_path = ROOT / "build" / "semantic" / "current.trig"
    if baseline_path.is_file():
        baseline = Dataset()
        baseline.parse(baseline_path, format="trig")
        seen.update(subject for subject, _, _, _ in baseline.quads((None, None, None, None)))
    new_individuals: set = set()
    object_assertions: list[tuple] = []
    data_assertions: list[tuple] = []
    addition_count = 0
    duplicate_iris: list[str] = []

    def add_provenance(subject, provenance: dict) -> None:
        node = BNode()
        data_graph.add((node, RDF.type, PROV.Entity))
        data_graph.add((node, PROV.specializationOf, subject))
        data_graph.add((node, URIRef("urn:pxai:semi:sourceType"), Literal(provenance["source_type"])))
        data_graph.add((node, URIRef("urn:pxai:semi:confidence"), Literal(provenance["confidence"])))
        data_graph.add((node, URIRef("urn:pxai:semi:sourceRef"), Literal(provenance["source_ref"])))

    def add_assertion(triple: tuple, provenance: dict) -> None:
        data_graph.add(triple)
        statement = BNode()
        data_graph.add((statement, RDF.type, RDF.Statement))
        data_graph.add((statement, RDF.subject, triple[0]))
        data_graph.add((statement, RDF.predicate, triple[1]))
        data_graph.add((statement, RDF.object, triple[2]))
        data_graph.add((statement, URIRef("urn:pxai:semi:sourceType"), Literal(provenance["source_type"])))
        data_graph.add((statement, URIRef("urn:pxai:semi:confidence"), Literal(provenance["confidence"])))
        data_graph.add((statement, URIRef("urn:pxai:semi:sourceRef"), Literal(provenance["source_ref"])))

    def complete_deterministic_relations(subject, types, data_values, provenance) -> None:
        """Add only relations derivable from an existing model/scenario file.

        Agent candidates often describe a simulation or business model but omit
        the RDF edges that can be derived without interpretation.  We derive
        those edges from the referenced YAML and existing graph inventory; no
        new business fact or guessed target is introduced.
        """
        type_names = {str(value) for value in types}
        source_refs = [str(value) for value in data_values.get("urn:pxai:semi:sourceRef", [])]
        if "urn:pxai:semi:BusinessVariable" in type_names:
            # identifier and unitCode are deterministic metadata when the
            # candidate already names a model variable.  Prefer nameEn, then
            # the IRI suffix, and resolve the unit from referenced model,
            # scenario, dataset, or inherited template YAML.
            if not data_values.get("urn:pxai:semi:identifier"):
                iri_identifier = str(subject).rsplit(":", 1)[-1]
                data_values["urn:pxai:semi:identifier"] = [str((data_values.get("urn:pxai:semi:nameEn") or [iri_identifier])[0])]
            if not data_values.get("urn:pxai:semi:unitCode"):
                model_refs: list[str] = []
                for source_ref in source_refs:
                    if source_ref.startswith("business/models/"):
                        model_refs.append(source_ref)
                    elif source_ref.startswith("simulation/scenarios/"):
                        scenario_path = ROOT / source_ref
                        try:
                            scenario = (yaml.safe_load(scenario_path.read_text(encoding="utf-8")) or {}).get("scenario") or {}
                            if scenario.get("model_ref"):
                                model_refs.append(str(scenario["model_ref"]))
                        except (OSError, yaml.YAMLError):
                            continue
                variable_id = str(data_values["urn:pxai:semi:identifier"][0])
                units: list[str] = []
                visited_templates: set[str] = set()

                def collect_template(ref: str) -> None:
                    if ref in visited_templates:
                        return
                    visited_templates.add(ref)
                    path = ROOT / ref
                    try:
                        template = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("template") or {}
                    except (OSError, yaml.YAMLError):
                        return
                    for item in (template.get("variables") or []) + (template.get("calculations") or []):
                        if str(item.get("id")) == variable_id and item.get("unit"):
                            units.append(str(item["unit"]))
                    for parent in template.get("extends") or []:
                        collect_template(str(parent))

                for model_ref in model_refs:
                    model_path = ROOT / model_ref
                    try:
                        model = (yaml.safe_load(model_path.read_text(encoding="utf-8")) or {}).get("model") or {}
                    except (OSError, yaml.YAMLError):
                        continue
                    dataset_ref = str(model.get("dataset_ref") or "")
                    if dataset_ref:
                        dataset_path = ROOT / dataset_ref
                        try:
                            dataset = ((yaml.safe_load(dataset_path.read_text(encoding="utf-8")) or {}).get("dataset") or {})
                            for item in dataset.get("values") or []:
                                if str(item.get("id")) == variable_id and item.get("unit"):
                                    units.append(str(item["unit"]))
                        except (OSError, yaml.YAMLError):
                            pass
                    if model.get("template_ref"):
                        collect_template(str(model["template_ref"]))
                if units:
                    data_values["urn:pxai:semi:unitCode"] = [units[0]]
        if "urn:pxai:semi:SimulationScenario" in type_names:
            model_refs: set[str] = set()
            for source_ref in source_refs:
                if not source_ref.startswith("simulation/scenarios/"):
                    continue
                path = ROOT / source_ref
                if not path.is_file():
                    continue
                try:
                    scenario = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("scenario") or {}
                except (OSError, yaml.YAMLError):
                    continue
                model_ref = str(scenario.get("model_ref") or "")
                if model_ref:
                    model_refs.add(model_ref)
            if model_refs and (subject, URIRef("urn:pxai:semi:usesBusinessModel"), None) not in data_graph:
                for model_ref in sorted(model_refs):
                    # The semantic migration script derives business-model IRIs from
                    # the model's stable ``id`` (not its relative file path).
                    # Resolve that same identifier so the deterministic edge
                    # points at the existing baseline node.
                    model_node = None
                    try:
                        model_doc = (yaml.safe_load((ROOT / model_ref).read_text(encoding="utf-8")) or {}).get("model") or {}
                        model_id = str(model_doc.get("id") or "").strip()
                        if model_id:
                            model_node = URIRef(f"urn:pxai:semi:business-model:{quote(model_id, safe='._-')}")
                    except (OSError, yaml.YAMLError):
                        model_node = None
                    if model_node is None:
                        continue
                    if model_node in seen:
                        add_assertion((subject, URIRef("urn:pxai:semi:usesBusinessModel"), model_node), provenance)

        if "urn:pxai:semi:BusinessModel" in type_names:
            for source_ref in source_refs:
                if not source_ref.startswith("business/models/"):
                    continue
                path = ROOT / source_ref
                if not path.is_file():
                    continue
                try:
                    model = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("model") or {}
                    dataset_ref = str(model.get("dataset_ref") or "")
                    dataset_path = ROOT / dataset_ref if dataset_ref else None
                    dataset = ((yaml.safe_load(dataset_path.read_text(encoding="utf-8")) or {}).get("dataset") or {}) if dataset_path and dataset_path.is_file() else {}
                except (OSError, yaml.YAMLError):
                    continue
                identifiers = {str(item.get("id")) for item in dataset.get("values") or [] if item.get("id")}
                for variable in sorted(seen):
                    if not isinstance(variable, URIRef):
                        continue
                    if str(next(data_graph.objects(variable, URIRef("urn:pxai:semi:identifier")), "")) not in identifiers:
                        continue
                    triple = (subject, URIRef("urn:pxai:semi:hasBusinessVariable"), variable)
                    if triple not in data_graph:
                        add_assertion(triple, provenance)
                break

    def validate_provenance(path: Path, provenance: dict) -> None:
        source_type = provenance["source_type"]
        source_ref = provenance["source_ref"]
        if source_type == "web" and not source_ref.startswith(("https://", "http://")):
            raise ValueError(f"{path.name}: web source_ref 必须是 URL")
        if source_type == "vfab" and not (ROOT / "sources" / "internal" / "vfab" / "manifest.json").is_file():
            raise ValueError(f"{path.name}: vFab 正式 manifest 未接入，不能生成 vfab 已验证语义")
        if source_type in {"internal_feature", "vfab"}:
            file_ref = source_ref.split("#", 1)[0]
            source_path = Path(file_ref)
            if source_path.is_absolute() or ".." in source_path.parts:
                raise ValueError(f"{path.name}: 内部来源必须使用项目内相对路径")
            if not (ROOT / source_path).is_file():
                raise ValueError(f"{path.name}: 内部来源不存在：{file_ref}")

    try:
        for path in files:
            doc = json.loads(path.read_text(encoding="utf-8"))
            jsonschema.validate(doc, contract, format_checker=jsonschema.FormatChecker())
            provenance = doc["provenance"]
            validate_provenance(path, provenance)
            additions = doc.get("additions") or {}
            addition_count += sum(len(additions.get(key) or []) for key in
                                  ("classes", "object_properties", "datatype_properties", "individuals",
                                   "object_assertions", "data_assertions"))
            for item in additions.get("classes") or []:
                subject = URIRef(item["iri"])
                # Parallel agents may independently discover the same concept.
                # Keep the first deterministic definition and ignore later ones;
                # RDF assertions from later proposals can still enrich it.
                if subject in seen:
                    duplicate_iris.append(str(subject))
                    continue
                schema_graph.add((subject, RDF.type, OWL.Class))
                schema_graph.add((subject, RDFS.label, Literal(item["label_zh"], lang="zh")))
                for parent in item["subclass_of"]: schema_graph.add((subject, RDFS.subClassOf, URIRef(parent)))
                for target in item.get("equivalent_to") or []: schema_graph.add((subject, OWL.equivalentClass, URIRef(target)))
                for target in item.get("disjoint_with") or []: schema_graph.add((subject, OWL.disjointWith, URIRef(target)))
                for restriction in item.get("restrictions") or []:
                    node = BNode()
                    kind = restriction["kind"]
                    schema_graph.add((node, RDF.type, OWL.Restriction))
                    schema_graph.add((node, OWL.onProperty, URIRef(restriction["on_property"])))
                    if kind in {"exact_cardinality", "min_cardinality", "max_cardinality"}:
                        if "cardinality" not in restriction: raise ValueError(f"{subject}: {kind} 缺少 cardinality")
                        predicate = {"exact_cardinality": OWL.qualifiedCardinality,
                                     "min_cardinality": OWL.minQualifiedCardinality,
                                     "max_cardinality": OWL.maxQualifiedCardinality}[kind]
                        schema_graph.add((node, predicate, Literal(restriction["cardinality"], datatype=XSD.nonNegativeInteger)))
                        target_key = "target_class" if restriction.get("target_class") else "target_datatype"
                        target_predicate = OWL.onClass if target_key == "target_class" else OWL.onDataRange
                        schema_graph.add((node, target_predicate, URIRef(restriction[target_key])))
                    else:
                        predicate = OWL.someValuesFrom if kind == "some_values_from" else OWL.allValuesFrom
                        target = restriction.get("target_class") or restriction.get("target_datatype")
                        schema_graph.add((node, predicate, URIRef(target)))
                    schema_graph.add((subject, RDFS.subClassOf, node))
                add_provenance(subject, provenance); seen.add(subject)
            for section, kind in (("object_properties", OWL.ObjectProperty), ("datatype_properties", OWL.DatatypeProperty)):
                for item in additions.get(section) or []:
                    subject = URIRef(item["iri"])
                    if subject in seen:
                        duplicate_iris.append(str(subject))
                        continue
                    schema_graph.add((subject, RDF.type, kind))
                    schema_graph.add((subject, RDFS.label, Literal(item["label_zh"], lang="zh")))
                    for domain in item["domain"]: schema_graph.add((subject, RDFS.domain, URIRef(domain)))
                    if section == "object_properties":
                        for target in item["range"]: schema_graph.add((subject, RDFS.range, URIRef(target)))
                        if item.get("inverse_of"): schema_graph.add((subject, OWL.inverseOf, URIRef(item["inverse_of"])))
                        if item.get("transitive"): schema_graph.add((subject, RDF.type, OWL.TransitiveProperty))
                    else: schema_graph.add((subject, RDFS.range, URIRef(item["datatype"])))
                    if item.get("functional"): schema_graph.add((subject, RDF.type, OWL.FunctionalProperty))
                    add_provenance(subject, provenance); seen.add(subject)
            for item in additions.get("individuals") or []:
                subject = URIRef(item["iri"])
                if subject in seen:
                    duplicate_iris.append(str(subject))
                    continue
                for cls in item["types"]: data_graph.add((subject, RDF.type, URIRef(cls)))
                data_graph.add((subject, RDFS.label, Literal(item["label_zh"], lang="zh")))
                for predicate, values in (item.get("objects") or {}).items():
                    for value in values:
                        triple = (subject, URIRef(predicate), URIRef(value))
                        add_assertion(triple, provenance); object_assertions.append(triple)
                data_values = dict(item.get("data") or {})
                # BusinessVariableShape requires a direct sourceRef on every
                # variable.  Agent outputs historically relied only on the
                # PROV provenance node, which is not visible to SHACL.  Inherit
                # the changeset provenance when the individual omits it; this
                # preserves the evidence boundary and keeps the variable
                # publishable without inventing a source.
                if "urn:pxai:semi:BusinessVariable" in {str(value) for value in item.get("types") or []} and not data_values.get("urn:pxai:semi:sourceRef"):
                    data_values["urn:pxai:semi:sourceRef"] = [provenance["source_ref"]]
                complete_deterministic_relations(subject, item.get("types") or [], data_values, provenance)
                for predicate, values in data_values.items():
                    for value in values:
                        triple = (subject, URIRef(predicate), Literal(value))
                        add_assertion(triple, provenance); data_assertions.append(triple)
                add_provenance(subject, provenance); seen.add(subject)
                new_individuals.add(subject)
            for item in additions.get("object_assertions") or []:
                triple = (URIRef(item["subject"]), URIRef(item["predicate"]), URIRef(item["object"]))
                add_assertion(triple, provenance); object_assertions.append(triple)
            for item in additions.get("data_assertions") or []:
                datatype = URIRef(item["datatype"]) if item.get("datatype") else None
                triple = (URIRef(item["subject"]), URIRef(item["predicate"]),
                          Literal(item["value"], datatype=datatype))
                add_assertion(triple, provenance); data_assertions.append(triple)
        if addition_count == 0:
            raise ValueError("语义变更集没有任何 additions")
        validation_schema = full_schema + schema_graph
        classes = set(validation_schema.subjects(RDF.type, OWL.Class))
        object_properties = set(validation_schema.subjects(RDF.type, OWL.ObjectProperty))
        datatype_properties = set(validation_schema.subjects(RDF.type, OWL.DatatypeProperty))
        for subject, parent in schema_graph.subject_objects(RDFS.subClassOf):
            if isinstance(parent, URIRef) and parent not in classes:
                raise ValueError(f"父类未声明：{subject} -> {parent}")
        new_properties = (object_properties | datatype_properties) & set(schema_graph.subjects())
        for prop in new_properties:
            for cls in schema_graph.objects(prop, RDFS.domain):
                if cls not in classes:
                    raise ValueError(f"属性 domain 未声明：{prop} -> {cls}")
            for cls in schema_graph.objects(prop, RDFS.range):
                if prop in object_properties and cls not in classes:
                    raise ValueError(f"对象属性 range 未声明：{prop} -> {cls}")
                if prop in datatype_properties and not str(cls).startswith(str(XSD)):
                    raise ValueError(f"数据属性 range 不是 XSD 类型：{prop} -> {cls}")
            for inverse in schema_graph.objects(prop, OWL.inverseOf):
                if inverse not in object_properties:
                    raise ValueError(f"逆属性未声明：{prop} -> {inverse}")
        for subject in schema_graph.subjects(RDF.type, OWL.Class):
            for predicate in (OWL.equivalentClass, OWL.disjointWith):
                for target in schema_graph.objects(subject, predicate):
                    if target not in classes:
                        raise ValueError(f"类公理引用未声明类：{subject} -> {target}")
        for restriction in schema_graph.subjects(RDF.type, OWL.Restriction):
            for prop in schema_graph.objects(restriction, OWL.onProperty):
                if prop not in object_properties | datatype_properties:
                    raise ValueError(f"限制引用未声明属性：{prop}")
                if prop in datatype_properties and next(schema_graph.objects(restriction, OWL.onClass), None):
                    raise ValueError(f"数据属性限制不能使用 owl:onClass：{prop}")
                if prop in object_properties and next(schema_graph.objects(restriction, OWL.onDataRange), None):
                    raise ValueError(f"对象属性限制不能使用 owl:onDataRange：{prop}")
            for cls in schema_graph.objects(restriction, OWL.onClass):
                if cls not in classes:
                    raise ValueError(f"限制引用未声明类：{cls}")
            for datatype in schema_graph.objects(restriction, OWL.onDataRange):
                if not str(datatype).startswith(str(XSD)):
                    raise ValueError(f"限制引用非法数据类型：{datatype}")
            for target in list(schema_graph.objects(restriction, OWL.someValuesFrom)) + list(schema_graph.objects(restriction, OWL.allValuesFrom)):
                prop = next(schema_graph.objects(restriction, OWL.onProperty), None)
                if prop in object_properties and target not in classes:
                    raise ValueError(f"对象属性值域限制引用未声明类：{target}")
                if prop in datatype_properties and not str(target).startswith(str(XSD)):
                    raise ValueError(f"数据属性值域限制不是 XSD 类型：{target}")
        for subject in new_individuals:
            for cls in data_graph.objects(subject, RDF.type):
                if cls not in classes:
                    raise ValueError(f"实例类型未声明为 OWL Class：{subject} -> {cls}")
        for subject, predicate, target in object_assertions:
            if subject not in seen:
                raise ValueError(f"对象关系主体不存在：{subject}")
            if predicate not in object_properties:
                raise ValueError(f"对象关系未声明为 OWL ObjectProperty：{predicate}")
            if target not in seen:
                raise ValueError(f"对象关系目标不存在：{subject} -> {target}")
        for subject, predicate, _ in data_assertions:
            if subject not in seen:
                raise ValueError(f"数据断言主体不存在：{subject}")
            if predicate not in datatype_properties:
                raise ValueError(f"数据属性未声明为 OWL DatatypeProperty：{predicate}")
    except (OSError, ValueError, json.JSONDecodeError, jsonschema.ValidationError) as exc:
        print(f"语义变更集校验失败：{exc}", file=sys.stderr)
        return 1
    if check_only:
        if duplicate_iris:
            unique_duplicates = sorted(set(duplicate_iris))
            print(f"语义变更集预检通过：{len(files)} 个提案；已忽略 {len(unique_duplicates)} 个重复 IRI：" + "、".join(unique_duplicates[:20]))
        else:
            print(f"语义变更集预检通过：{len(files)} 个提案")
        return 0
    rollback_targets = [
        SCHEMA_TARGET,
        DATA_TARGET,
        ROOT / "knowledge" / "scenarios" / "current.json",
        ROOT / "knowledge" / "articles" / "current-scenarios.md",
    ]
    backups = {path: path.read_bytes() if path.is_file() else None for path in rollback_targets}

    def restore_targets() -> None:
        for path, content in backups.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)

    temp_schema = SCHEMA_TARGET.with_suffix(".ttl.tmp")
    temp_data = DATA_TARGET.with_suffix(".ttl.tmp")
    try:
        schema_graph.serialize(temp_schema, format="turtle", encoding="utf-8")
        data_graph.serialize(temp_data, format="turtle", encoding="utf-8")
        Graph().parse(temp_schema, format="turtle")
        Graph().parse(temp_data, format="turtle")
        os.replace(temp_schema, SCHEMA_TARGET)
        os.replace(temp_data, DATA_TARGET)
        command = ([sys.executable, str(ROOT / "scripts" / "semantic_validate.py")]
                   if args.semantic_only else
                   [sys.executable, str(ROOT / "scripts" / "kb.py"), "check", "--no-precheck"])
        result = subprocess.run(command, cwd=ROOT)
    except Exception as exc:  # noqa: BLE001
        restore_targets()
        print(f"语义写入失败，已回滚：{exc}", file=sys.stderr)
        return 1
    finally:
        temp_schema.unlink(missing_ok=True)
        temp_data.unlink(missing_ok=True)
    if result.returncode:
        restore_targets()
        print("合并后校验失败，已回滚语义与场景产物。", file=sys.stderr)
        return 1
    if args.defer_archive:
        print(f"语义变更集合并通过：{len(files)} 个提案；等待全链通过后归档")
    else:
        applied = ROOT / "semantic_changesets" / "applied"
        applied.mkdir(parents=True, exist_ok=True)
        for path in files: shutil.move(str(path), str(applied / path.name))
        print(f"语义变更集合并通过：{len(files)} 个提案")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
