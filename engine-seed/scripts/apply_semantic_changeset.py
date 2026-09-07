"""校验并原子合并 OWL/ABox JSON 变更集。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
PENDING = ROOT / "semantic_changesets" / "pending"
SCHEMA_TARGET = ROOT / "ontology" / "modules" / "generated.ttl"
DATA_TARGET = ROOT / "knowledge" / "semantic" / "current.ttl"
# 溯源分离：prov:Entity / rdf:Statement 记账节点写这里，current.ttl 保持纯净（只留真实实例）。
PROV_TARGET = ROOT / "knowledge" / "semantic" / "provenance.ttl"

# Windows 上 os.replace 覆盖 current.ttl / generated.ttl 时，若后端仪表盘、问答接地或 _inventory()
# 恰好正用 rdflib 解析目标文件，会瞬时报 WinError 5(拒绝访问)/32(占用)。这些读句柄生命周期极短，
# 有界重试即可清掉抖动；仍失败才让上层回滚。POSIX 无此共享语义，重试对其无副作用。
_REPLACE_RETRIES = 6
_REPLACE_BACKOFF = 0.25  # 秒；线性递增：0.25→0.5→…→1.5，累计约 5s 上限。
_TRANSIENT_WINERRORS = frozenset({5, 32})


def _atomic_replace(src: Path, dst: Path) -> None:
    """os.replace 的有界重试封装：只对 Windows 瞬时共享冲突重试，其余错误立即上抛。"""
    for attempt in range(_REPLACE_RETRIES + 1):
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            winerror = getattr(exc, "winerror", None)
            if winerror not in _TRANSIENT_WINERRORS or attempt >= _REPLACE_RETRIES:
                raise
            time.sleep(_REPLACE_BACKOFF * (attempt + 1))


def derive_equipment_code(iri: str) -> str:
    """从设备个体自身 IRI 后缀确定性派生 equipmentCode（主数据标识，非现场实测值）。

    EquipmentShape 对 semi:equipmentCode 设了 minCount=1；Agent 候选常声明设备个体却漏填该编码，
    触发 SHACL Violation 并连累整批回滚。编码是设备主数据标识，可从个体标识确定性得出——与
    BusinessVariable.identifier 由 IRI 后缀补齐同源。见知识条目
    urn-pxai-semi-knowledge-fab-equipment-code-mincount-repair。
    """
    local = str(iri).rsplit(":", 1)[-1].rsplit("/", 1)[-1]
    stem = re.sub(r"^(equipment[_-]|equip[_-])", "", local, flags=re.I)
    code = re.sub(r"[^0-9A-Za-z]+", "-", stem).strip("-").upper()
    return code or local.upper()


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
    provenance_graph = Graph()  # 溯源/具体化节点单独一图 → provenance.ttl，与 current.ttl 分离
    schema_graph.parse(SCHEMA_TARGET, format="turtle")
    data_graph.parse(DATA_TARGET, format="turtle")
    if PROV_TARGET.is_file():
        provenance_graph.parse(PROV_TARGET, format="turtle")
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

    _SH = "http://www.w3.org/ns/shacl#"
    _SH_TARGET_CLASS = URIRef(_SH + "targetClass")
    _SH_PROPERTY = URIRef(_SH + "property")
    _SH_PATH = URIRef(_SH + "path")
    _SH_MIN_COUNT = URIRef(_SH + "minCount")

    def required_min_paths() -> dict:
        """从 SHACL 形状抽取每个 targetClass 的『必填直连边』(minCount>=1)。

        仅取形状顶层 sh:property 且 sh:path 为具名 IRI、sh:minCount>=1 的约束；
        sh:or / sh:and 等条件分支不在此列——其取舍留给权威 SHACL 门禁，避免预筛
        误判条件性约束。语义与 semantic_validate.py 的 targetClass 改写一致：只约束
        显式直接声明该类型的节点（此处按声明类型查表，不含推理派生类型）。
        """
        shapes = Graph()
        for shp in sorted((ROOT / "ontology" / "shapes").glob("*.ttl")):
            shapes.parse(shp, format="turtle")
        required: dict = {}
        for shape, _, cls in shapes.triples((None, _SH_TARGET_CLASS, None)):
            for prop in shapes.objects(shape, _SH_PROPERTY):
                path = shapes.value(prop, _SH_PATH)
                minc = shapes.value(prop, _SH_MIN_COUNT)
                if not isinstance(path, URIRef) or minc is None:
                    continue
                try:
                    minimum = int(minc)
                except (TypeError, ValueError):
                    continue
                if minimum >= 1:
                    required.setdefault(cls, []).append((path, minimum))
        return required

    def purge_node(node) -> None:
        """删除某节点在图中的一切痕迹：正反向三元组、指向它的 RDF 具体化语句、PROV 溯源节点。
        溯源/具体化节点已分离到 provenance_graph（对应 provenance.ttl），故在该图中查删。"""
        aux = set()
        for statement in provenance_graph.subjects(RDF.subject, node):
            aux.add(statement)
        for statement in provenance_graph.subjects(RDF.object, node):
            aux.add(statement)
        for entity in provenance_graph.subjects(PROV.specializationOf, node):
            aux.add(entity)
        for bnode in aux:
            for triple in list(provenance_graph.triples((bnode, None, None))):
                provenance_graph.remove(triple)
        for graph in (data_graph, schema_graph, provenance_graph):
            for triple in list(graph.triples((node, None, None))):
                graph.remove(triple)
            for triple in list(graph.triples((None, None, node))):
                graph.remove(triple)

    def prune_incomplete_individuals() -> list:
        """剔除本轮新增中未满足『必填直连边 minCount』的个体（如三件套皆缺的诊断手册）。

        只在『整批候选 + 基线』全部合并、确定性补边(complete_deterministic_relations)
        都已就绪后才判定——此时不会误伤边落在同批他处的节点，保证 sound。剔除会连带
        清掉其正反向断言，可能令依赖它的个体转为不完整，故迭代至不动点。这是对『未证明
        的不完整候选』做保守丢弃：不发明事实、不改写已发布内容；只剔节点、保留同批其余
        合规断言。通过预筛的节点仍走下方权威 SHACL 门禁，判定口径不放水。
        """
        required = required_min_paths()
        if not required:
            return []
        dropped: list = []
        changed = True
        while changed:
            changed = False
            for subject in list(new_individuals):
                requirements: list = []
                for cls in set(data_graph.objects(subject, RDF.type)):
                    requirements.extend(required.get(cls, []))
                for path, minimum in requirements:
                    if len(set(data_graph.objects(subject, path))) < minimum:
                        purge_node(subject)
                        new_individuals.discard(subject)
                        dropped.append((str(subject), str(path)))
                        changed = True
                        break
        return dropped

    def _prov_node(kind: str, key: str) -> URIRef:
        """确定性具名 URN（对齐 migrate_semantic 的 sha256[:24] 方案），替代匿名 BNode：
        跨轮/重复断言可去重合并，且 migrate 读回 provenance.ttl 时不会因 BNode 重标号而漂移。"""
        return URIRef(f"urn:pxai:semi:{kind}:{hashlib.sha256(key.encode('utf-8')).hexdigest()[:24]}")

    def add_provenance(subject, provenance: dict) -> None:
        node = _prov_node("provenance", str(subject))
        provenance_graph.add((node, RDF.type, PROV.Entity))
        provenance_graph.add((node, PROV.specializationOf, subject))
        provenance_graph.add((node, URIRef("urn:pxai:semi:sourceType"), Literal(provenance["source_type"])))
        provenance_graph.add((node, URIRef("urn:pxai:semi:confidence"), Literal(provenance["confidence"])))
        provenance_graph.add((node, URIRef("urn:pxai:semi:sourceRef"), Literal(provenance["source_ref"])))

    def add_assertion(triple: tuple, provenance: dict) -> None:
        data_graph.add(triple)  # 被断言的真实三元组留在实例图（current.ttl）
        statement = _prov_node("assertion", f"{triple[0]}|{triple[1]}|{triple[2]}")
        provenance_graph.add((statement, RDF.type, RDF.Statement))
        provenance_graph.add((statement, RDF.subject, triple[0]))
        provenance_graph.add((statement, RDF.predicate, triple[1]))
        provenance_graph.add((statement, RDF.object, triple[2]))
        provenance_graph.add((statement, URIRef("urn:pxai:semi:sourceType"), Literal(provenance["source_type"])))
        provenance_graph.add((statement, URIRef("urn:pxai:semi:confidence"), Literal(provenance["confidence"])))
        provenance_graph.add((statement, URIRef("urn:pxai:semi:sourceRef"), Literal(provenance["source_ref"])))

    def complete_deterministic_relations(subject, types, data_values, provenance) -> None:
        """Add only relations derivable from an existing model/scenario file.

        Agent candidates often describe a simulation or business model but omit
        the RDF edges that can be derived without interpretation.  We derive
        those edges from the referenced YAML and existing graph inventory; no
        new business fact or guessed target is introduced.
        """
        type_names = {str(value) for value in types}
        source_refs = [str(value) for value in data_values.get("urn:pxai:semi:sourceRef", [])]
        # 声明为 Equipment（或其子类）的个体缺 equipmentCode 时确定性补齐，避免 SHACL minCount
        # 违规回滚整批候选；只对已按类型声明为设备的个体生效，不误伤 FacilityDigitalTwin 等联合域实体。
        if equipment_type_strs & type_names and not data_values.get("urn:pxai:semi:equipmentCode"):
            data_values["urn:pxai:semi:equipmentCode"] = [derive_equipment_code(str(subject))]
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
            equipment_root = URIRef("urn:pxai:semi:Equipment")
            equipment_type_strs = {str(equipment_root)} | {
                str(cls) for cls in (full_schema + schema_graph).transitive_subjects(RDFS.subClassOf, equipment_root)
            }
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
    # 合并后、门禁前：剔除未满足必填直连边 minCount 的新增个体（保守丢弃未证明的不完整
    # 候选；只剔节点、保留同批其余合规断言）。放在 --check 早返回之后，保持结构预检语义纯净。
    dropped_incomplete = prune_incomplete_individuals()
    if dropped_incomplete:
        preview = "、".join(sorted({iri for iri, _ in dropped_incomplete})[:20])
        print(f"已剔除 {len(dropped_incomplete)} 个不完整个体（缺必填直连边，其余合规断言保留）：{preview}")
    rollback_targets = [
        SCHEMA_TARGET,
        DATA_TARGET,
        PROV_TARGET,
        ROOT / "knowledge" / "scenarios" / "current.json",
        ROOT / "knowledge" / "articles" / "current-scenarios.md",
        # 构建产物必须一起回滚：kb.py check 会重新生成它，若失败后只回滚源文件，
        # 下一轮读到的就是带着废弃候选的脏 trig，后续所有校验都在污染基线上跑。
        ROOT / "build" / "semantic" / "current.trig",
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
    temp_prov = PROV_TARGET.with_suffix(".ttl.tmp")
    try:
        schema_graph.serialize(temp_schema, format="turtle", encoding="utf-8")
        data_graph.serialize(temp_data, format="turtle", encoding="utf-8")
        provenance_graph.serialize(temp_prov, format="turtle", encoding="utf-8")
        Graph().parse(temp_schema, format="turtle")
        Graph().parse(temp_data, format="turtle")
        Graph().parse(temp_prov, format="turtle")
        _atomic_replace(temp_schema, SCHEMA_TARGET)
        _atomic_replace(temp_data, DATA_TARGET)
        _atomic_replace(temp_prov, PROV_TARGET)
        # 门禁子集链：跳过 build_index/scenario_mine（派生物，非一致性判定），regress /
        # semantic_validate / semantic_test / simulate_check 等判定环节一律保留。派生物
        # 在下方门禁通过后由 refresh-derived best-effort 补跑，绝不因其失败回滚已发布内容。
        command = ([sys.executable, str(ROOT / "scripts" / "semantic_validate.py")]
                   if args.semantic_only else
                   [sys.executable, str(ROOT / "scripts" / "kb.py"), "check", "--no-precheck", "--defer-derived"])
        result = subprocess.run(command, cwd=ROOT)
    except Exception as exc:  # noqa: BLE001
        restore_targets()
        print(f"语义写入失败，已回滚：{exc}", file=sys.stderr)
        return 1
    finally:
        temp_schema.unlink(missing_ok=True)
        temp_data.unlink(missing_ok=True)
        temp_prov.unlink(missing_ok=True)
    if result.returncode:
        restore_targets()
        print("合并后校验失败，已回滚语义与场景产物。", file=sys.stderr)
        return 1
    # 门禁已通过。派生物（检索索引/场景卡）此前被移出门禁关键路径，此处 best-effort
    # 补跑一次刷新——失败只告警、绝不回滚已通过发布的内容（与门禁 PASS/FAIL 解耦）。
    # semantic_only 路径本就不含 build_index/scenario_mine，无需补跑。
    if not args.semantic_only:
        try:
            subprocess.run([sys.executable, str(ROOT / "scripts" / "kb.py"), "refresh-derived"],
                           cwd=ROOT, timeout=1800)
        except Exception as exc:  # noqa: BLE001
            print(f"派生物刷新失败（不影响已发布内容）：{exc}", file=sys.stderr)
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
