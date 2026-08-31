from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import jsonschema
import yaml
from rdflib import Graph, RDF, URIRef
from rdflib.namespace import OWL

from ..config import settings


def round_directory(run_id: str, round_number: int) -> Path:
    path = settings.data_dir / "runs" / run_id / f"round-{round_number:05d}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _slug(value: str, fallback: str) -> str:
    value = re.sub(r"[^a-z0-9._-]+", "-", value.casefold()).strip("-.")
    return value[:70] or fallback


def _as_list(value: Any) -> list:
    """Normalize common model JSON shape drift without weakening the contract."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _normalize_semantic_candidate(candidate: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Repair harmless scalar-vs-array drift before strict JSON Schema validation."""
    normalized = dict(candidate)
    additions = dict(normalized.get("additions") or {})
    warnings: list[str] = []
    for section in ("classes", "object_properties", "datatype_properties", "individuals", "object_assertions", "data_assertions"):
        original = additions.get(section)
        additions[section] = [item for item in _as_list(original) if isinstance(item, dict)]
        if original is not None and not isinstance(original, list):
            warnings.append(f"已将 {section} 从单对象规范化为数组")
    for item in additions["classes"]:
        for field in ("subclass_of", "equivalent_to", "disjoint_with", "restrictions"):
            if field in item and not isinstance(item[field], list):
                item[field] = _as_list(item[field])
                warnings.append(f"已将类 {item.get('iri')} 的 {field} 规范化为数组")
    for item in additions["object_properties"]:
        for field in ("domain", "range"):
            if field in item and not isinstance(item[field], list):
                item[field] = _as_list(item[field])
                warnings.append(f"已将对象属性 {item.get('iri')} 的 {field} 规范化为数组")
    for item in additions["datatype_properties"]:
        if "domain" in item and not isinstance(item["domain"], list):
            item["domain"] = _as_list(item["domain"])
            warnings.append(f"已将数据属性 {item.get('iri')} 的 domain 规范化为数组")
    for item in additions["individuals"]:
        if "types" in item and not isinstance(item["types"], list):
            item["types"] = _as_list(item["types"])
            warnings.append(f"已将实例 {item.get('iri')} 的 types 规范化为数组")
        for field in ("objects", "data"):
            values = item.get(field)
            if not isinstance(values, dict):
                item[field] = {}
                if values is not None:
                    warnings.append(f"已丢弃实例 {item.get('iri')} 的非法 {field} 映射")
                continue
            repaired: dict[str, list] = {}
            for predicate, raw_values in values.items():
                repaired[str(predicate)] = _as_list(raw_values)
                if not isinstance(raw_values, list):
                    warnings.append(f"已将实例 {item.get('iri')} 的 {predicate} 值规范化为数组")
            item[field] = repaired
    warnings.extend(_enforce_playbook_trio(additions))
    normalized["additions"] = additions
    return normalized, warnings


_PLAYBOOK_TRIO = {
    "urn:pxai:semi:diagnosesAnomaly": "urn:pxai:semi:Anomaly",
    "urn:pxai:semi:hasPossibleCause": None,
    "urn:pxai:semi:hasDiagnosticAction": "urn:pxai:semi:Action",
}


def _enforce_playbook_trio(additions: dict[str, Any]) -> list[str]:
    """Strip half-formed diagnostic playbooks before the expensive SHACL gate.

    semi:DiagnosticPlaybookShape requires all three properties together.  A model
    that asserts only one of them produces a MinCount violation that costs a full
    inference + validation cycle to discover and that no repair prompt can fix
    reliably.  Dropping the incomplete trio locally keeps the rest of the
    candidate publishable instead of failing the whole round.
    """
    warnings: list[str] = []
    individuals = additions.get("individuals") or []
    local_types = {str(item.get("iri")): {str(value) for value in item.get("types") or []} for item in individuals if item.get("iri")}
    for item in individuals:
        objects = item.get("objects") or {}
        present = [predicate for predicate in _PLAYBOOK_TRIO if objects.get(predicate)]
        if not present:
            continue
        reason = ""
        if len(present) != len(_PLAYBOOK_TRIO):
            reason = f"仅给出 {len(present)}/3 个诊断属性"
        elif len(objects.get("urn:pxai:semi:diagnosesAnomaly") or []) != 1:
            reason = "diagnosesAnomaly 必须恰好 1 个值"
        else:
            for predicate, required_type in _PLAYBOOK_TRIO.items():
                if not required_type:
                    continue
                for target in objects.get(predicate) or []:
                    types = local_types.get(str(target))
                    if types is not None and required_type not in types:
                        reason = f"{predicate} 的目标 {target} 未声明 {required_type}"
                        break
                if reason:
                    break
        if reason:
            for predicate in present:
                objects.pop(predicate, None)
            warnings.append(f"已移除实例 {item.get('iri')} 的诊断属性（{reason}），避免 DiagnosticPlaybook 闸门失败")
    return warnings


def _semantic_contract() -> dict:
    path = settings.engine_root / "output-contracts" / "semantic-changeset.schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _inventory() -> dict[str, set[str]]:
    schema, data = Graph(), Graph()
    for path in sorted((settings.engine_root / "ontology" / "modules").glob("*.ttl")):
        schema.parse(path, format="turtle")
    data_path = settings.engine_root / "knowledge" / "semantic" / "current.ttl"
    if data_path.is_file(): data.parse(data_path, format="turtle")
    classes = {str(item) for item in schema.subjects(RDF.type, OWL.Class)}
    object_properties = {str(item) for item in schema.subjects(RDF.type, OWL.ObjectProperty)}
    datatype_properties = {str(item) for item in schema.subjects(RDF.type, OWL.DatatypeProperty)}
    subjects = {str(item) for item in schema.subjects() if isinstance(item, URIRef)} | {str(item) for item in data.subjects() if isinstance(item, URIRef)}
    return {"classes": classes, "object_properties": object_properties, "datatype_properties": datatype_properties, "subjects": subjects}


def _sanitize_additions(additions: dict, allowed: dict[str, set[str]], claimed: set[str]) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    result = {key: [] for key in ("classes", "object_properties", "datatype_properties", "individuals", "object_assertions", "data_assertions")}
    # Build the accepted inventory before checking dependent sections.  A model can
    # mention a newly proposed class in a property domain, but a duplicate or invalid
    # class must not remain in the allow-list or it will create an OWL dangling ref.
    existing_classes = set(allowed["classes"] - {str(item.get("iri")) for item in additions.get("classes") or []})
    accepted_classes = set(existing_classes)
    pending_classes = [item for item in additions.get("classes") or [] if isinstance(item, dict)]
    while pending_classes:
        deferred: list[dict] = []
        accepted_this_pass = 0
        for item in pending_classes:
            iri = item.get("iri")
            if iri in claimed:
                warnings.append(f"跳过重复或已有类 {iri}"); continue
            parents = item.get("subclass_of") or []
            if any(parent not in accepted_classes for parent in parents):
                deferred.append(item); continue
            copy = dict(item)
            copy["equivalent_to"] = [value for value in copy.get("equivalent_to") or [] if value in accepted_classes]
            copy["disjoint_with"] = [value for value in copy.get("disjoint_with") or [] if value in accepted_classes]
            clean_restrictions = []
            for restriction in copy.get("restrictions") or []:
                prop = restriction.get("on_property")
                target = restriction.get("target_class")
                datatype = restriction.get("target_datatype")
                if prop not in allowed["object_properties"] | allowed["datatype_properties"]: continue
                if target and target not in accepted_classes: continue
                if datatype and not str(datatype).startswith("http://www.w3.org/2001/XMLSchema#"): continue
                clean_restrictions.append(restriction)
            if "restrictions" in copy: copy["restrictions"] = clean_restrictions
            result["classes"].append(copy); accepted_classes.add(iri); claimed.add(iri); accepted_this_pass += 1
        if not deferred or not accepted_this_pass:
            for item in deferred:
                warnings.append(f"跳过父类未声明的类 {item.get('iri')}")
            break
        pending_classes = deferred
    allowed_classes = accepted_classes
    for section, category in (("object_properties", "object_properties"), ("datatype_properties", "datatype_properties")):
        for item in additions.get(section) or []:
            iri = item.get("iri")
            if iri in claimed:
                warnings.append(f"跳过重复或已有属性 {iri}"); continue
            # schema 要求 domain（对象属性还要求 range）非空且成员均为已声明类。
            # 空数组虽能过 sanitizer 的成员检查，却会撞上 minItems:1 让整份输出校验失败，
            # 故在此显式丢弃缺失/空值的属性，只发警告，保住候选其余部分可发布。
            if not (item.get("domain") or []):
                warnings.append(f"跳过 domain 缺失或为空的属性 {iri}"); continue
            if any(value not in allowed_classes for value in item.get("domain") or []):
                warnings.append(f"跳过domain未声明的属性 {iri}"); continue
            if section == "object_properties":
                if not (item.get("range") or []):
                    warnings.append(f"跳过 range 缺失或为空的对象属性 {iri}"); continue
                if any(value not in allowed_classes for value in item.get("range") or []):
                    warnings.append(f"跳过range未声明的属性 {iri}"); continue
            result[section].append(item); claimed.add(iri)
    accepted_object_properties = set(allowed["object_properties"] - {str(item.get("iri")) for item in additions.get("object_properties") or []}) | {str(item.get("iri")) for item in result["object_properties"]}
    accepted_datatype_properties = set(allowed["datatype_properties"] - {str(item.get("iri")) for item in additions.get("datatype_properties") or []}) | {str(item.get("iri")) for item in result["datatype_properties"]}
    # inverse_of 是对象属性上唯一的公理字段，schema 允许但之前 sanitizer 原样透传。
    # 只放行指向"已存在或本批次新建"的对象属性，避免悬空 owl:inverseOf；此处已算好
    # accepted_object_properties，故 A inverse_of B 无论出现顺序都能通过。悬空时剥掉该
    # 字段而不是丢弃整条属性，保住属性本身可发布。
    for prop in result["object_properties"]:
        inverse = prop.get("inverse_of")
        if inverse and inverse not in accepted_object_properties:
            warnings.append(f"属性 {prop.get('iri')} 跳过悬空 inverse_of {inverse}")
            prop.pop("inverse_of", None)
    for cls in result["classes"]:
        if "restrictions" in cls:
            cls["restrictions"] = [item for item in cls.get("restrictions") or [] if item.get("on_property") in accepted_object_properties | accepted_datatype_properties]
    for item in additions.get("individuals") or []:
        iri = item.get("iri")
        if iri in claimed:
            warnings.append(f"跳过重复或已有实例 {iri}"); continue
        if any(value not in allowed_classes for value in item.get("types") or []):
            warnings.append(f"跳过类型未声明的实例 {iri}"); continue
        copy = dict(item)
        for predicate in (copy.get("objects") or {}):
            if predicate not in accepted_object_properties: warnings.append(f"实例 {iri} 跳过未声明对象属性 {predicate}")
        for predicate in (copy.get("data") or {}):
            if predicate not in accepted_datatype_properties: warnings.append(f"实例 {iri} 跳过未声明数据属性 {predicate}")
        copy["objects"] = {predicate: [value for value in _as_list(values) if value in allowed["subjects"]] for predicate, values in (copy.get("objects") or {}).items() if predicate in accepted_object_properties}
        copy["data"] = {predicate: [value for value in _as_list(values) if isinstance(value, (str, int, float, bool))] for predicate, values in (copy.get("data") or {}).items() if predicate in accepted_datatype_properties}
        result["individuals"].append(copy); claimed.add(iri)
    accepted_subjects = allowed["subjects"] | {str(item.get("iri")) for item in result["individuals"]}
    accepted_subjects |= {str(item.get("iri")) for item in result["classes"]}
    for individual in result["individuals"]:
        individual["objects"] = {predicate: [value for value in values if value in accepted_subjects] for predicate, values in (individual.get("objects") or {}).items()}
    for item in additions.get("object_assertions") or []:
        if item.get("subject") in accepted_subjects and item.get("object") in accepted_subjects and item.get("predicate") in accepted_object_properties:
            result["object_assertions"].append(item)
        else: warnings.append(f"跳过悬空对象关系 {item.get('predicate')}")
    for item in additions.get("data_assertions") or []:
        if item.get("subject") in accepted_subjects and item.get("predicate") in accepted_datatype_properties:
            result["data_assertions"].append(item)
        else: warnings.append(f"跳过未声明数据属性 {item.get('predicate')}")
    return {key: value for key, value in result.items() if value}, warnings


def prompt_contract() -> dict:
    """Compact, machine-oriented contract sent to the model."""
    return {
        "_hard_rules": [
            "个体若使用 semi:diagnosesAnomaly / semi:hasPossibleCause / semi:hasDiagnosticAction "
            "中的任意一个，就必须同时给出这三个属性：diagnosesAnomaly 恰好 1 个 semi:Anomaly 类型个体、"
            "hasPossibleCause 至少 1 个、hasDiagnosticAction 至少 1 个 semi:Action 类型个体。"
            "凑不齐就完全不要用这三个属性，改用 semi:mayCause / semi:hasEvidence / semi:relatedTo 表达。",
            "被 hasDiagnosticAction 指向的个体 types 必须包含 urn:pxai:semi:Action；"
            "被 diagnosesAnomaly 指向的个体 types 必须包含 urn:pxai:semi:Anomaly。"
            "这些目标个体要在同一份 additions.individuals 里一并定义，不能只写 IRI。",
            "新建 object_properties 必须给出非空 domain 与 range；新建 datatype_properties 必须给出非空 domain。"
            "domain/range 只能引用已存在的类，或本次 additions.classes 里新定义的类 IRI，指向语义上最贴切的类即可"
            "（可以指向 Equipment、ProcessingEvent、DiagnosticPlaybook 等约束类：校验已按显式声明类型治理，"
            "domain/range 推理不会再误触发这些类的必填校验）。若找不到合适的类，就不要新建该属性，"
            "绝不要输出空的 domain 或 range 数组。",
            "鼓励为新建对象属性补充 OWL 公理以增强推理：若属性有天然的反向关系（如 contains↔containedIn、"
            "causes↔causedBy），用 inverse_of 指向对应属性；若某个新类与既有类语义等价，用 equivalent_to 声明。"
            "inverse_of 只能指向已存在或本次一并新建的对象属性 IRI，equivalent_to 只能指向已存在的类 IRI，"
            "没有合适目标就省略这两个字段，绝不要凭空编造或指向不存在的 IRI。",
        ],
        "summary": "string",
        "customer_pains": ["string"],
        "evidence_notes": ["string"],
        "semantic_changesets": [{
            "provenance": {"source_type": "web|model_prior", "confidence": "high|medium|low", "source_ref": "URL or model reference"},
            "additions": {
                "classes": [{"iri": "urn:pxai:semi:...", "label_zh": "...", "subclass_of": ["existing/new class IRI"], "equivalent_to": ["可选：语义等价的既有类 IRI，没有就省略"]}],
                "object_properties": [{"iri": "urn:pxai:semi:...", "label_zh": "...", "domain": ["class IRI"], "range": ["class IRI"], "inverse_of": "可选：本属性天然反向的对象属性 IRI（既有或本次新建），没有就省略"}],
                "datatype_properties": [{"iri": "urn:pxai:semi:...", "label_zh": "...", "domain": ["class IRI"], "datatype": "http://www.w3.org/2001/XMLSchema#string"}],
                "individuals": [{"iri": "urn:pxai:semi:...", "types": ["class IRI"], "label_zh": "...", "objects": {"object property IRI": ["individual IRI"]}, "data": {"datatype property IRI": ["value"]}}],
                "object_assertions": [{"subject": "IRI", "predicate": "IRI", "object": "IRI"}],
                "data_assertions": [{"subject": "IRI", "predicate": "IRI", "value": "value"}],
            },
        }],
        "business_model_candidates": [{
            "model": {
                "name": "string", "domain": "semiconductor.fab|semiconductor.fac|semiconductor.eqp",
                "period": "month", "currency": "CNY",
                "template_ref": "business/templates/existing.yaml", "dataset_ref": "business/datasets/existing.yaml",
                "outputs": ["outputs already defined by the chosen template"],
            }
        }],
        "simulation_candidates": [{
            "scenario": {
                "name": "string", "model_ref": "business/models/existing.yaml", "description": "string",
                "interventions": [{"target_ref": "existing legacy ontology id", "variable": "existing model input", "operation": "set|add|multiply", "value": 1}],
                "assertions": [{"type": "nondecreasing|nonincreasing|unchanged", "variable": "existing model output"}],
                "investment": {"one_time": 0, "unit": "CNY"},
            }
        }],
        "knowledge_entries": [{
            "entry": {
                "id": "urn:pxai:semi:knowledge:...", "title": "string", "domain": "fab|fac|eqp",
                "summary": "string", "content": "evidence-grounded concise knowledge",
                "source_refs": ["URL or model reference"], "related_iris": ["existing or same-output IRI"],
                "confidence": "high|medium|low"
            }
        }],
        "rule_candidates": [{
            "rule": {
                "rule_id": "R-AUTO-...", "name": "string", "purpose": "string", "implementation": "sparql",
                "query": "required read-only SPARQL CONSTRUCT", "preconditions": ["string"], "conclusion": "string",
                "required_sources": ["string"], "tests": ["tests/semantic/..."], "confidence": "high|medium|low"
            }
        }],
        "scenario_article_markdown": "Markdown正文，包含场景、客户痛点、影响、证据边界和仿真含义",
    }


def _allowed_source(mode: str, requested: str) -> str:
    allowed = {"web"} if mode == "web" else ({"model_prior"} if mode == "model_prior" else {"web", "model_prior"})
    return requested if requested in allowed else ("web" if "web" in allowed else "model_prior")


def validate_and_store_agent_output(
    raw: dict[str, Any], *, run_id: str, round_number: int, agent_id: str,
    source_mode: str, model_id: str, evidence: list[dict],
) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("Agent 输出必须是 JSON 对象")
    output_dir = round_directory(run_id, round_number) / "agents" / agent_id
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    urls = {item.get("url") for item in evidence if item.get("fetch_status") == "ok" and item.get("url")}
    contract = _semantic_contract()
    inventory = _inventory()
    raw_candidates: list[dict[str, Any]] = []
    shape_warnings: list[str] = []
    for item in _as_list(raw.get("semantic_changesets")):
        if isinstance(item, dict):
            normalized_candidate, warnings = _normalize_semantic_candidate(item)
            raw_candidates.append(normalized_candidate)
            shape_warnings.extend(warnings)
    proposed = {
        "classes": {str(item.get("iri")) for candidate in raw_candidates for item in (candidate.get("additions") or {}).get("classes") or [] if item.get("iri")},
        "object_properties": {str(item.get("iri")) for candidate in raw_candidates for item in (candidate.get("additions") or {}).get("object_properties") or [] if item.get("iri")},
        "datatype_properties": {str(item.get("iri")) for candidate in raw_candidates for item in (candidate.get("additions") or {}).get("datatype_properties") or [] if item.get("iri")},
        "individuals": {str(item.get("iri")) for candidate in raw_candidates for item in (candidate.get("additions") or {}).get("individuals") or [] if item.get("iri")},
    }
    allowed = {
        "classes": inventory["classes"] | proposed["classes"],
        "object_properties": inventory["object_properties"] | proposed["object_properties"],
        "datatype_properties": inventory["datatype_properties"] | proposed["datatype_properties"],
        "subjects": inventory["subjects"] | proposed["classes"] | proposed["object_properties"] | proposed["datatype_properties"] | proposed["individuals"],
    }
    claimed = set(inventory["subjects"])
    sanitization_warnings: list[str] = list(shape_warnings)
    semantic_files: list[str] = []
    normalized_changesets: list[dict] = []
    for index, candidate in enumerate(raw_candidates, 1):
        provenance = dict(candidate.get("provenance") or {})
        source_type = _allowed_source(source_mode, str(provenance.get("source_type") or ""))
        source_ref = str(provenance.get("source_ref") or "").strip()
        if source_type == "web":
            if source_ref not in urls:
                if not urls:
                    raise ValueError("Web 语义提案没有可核查的正文证据 URL")
                source_ref = sorted(urls)[0]
        else:
            source_ref = f"model:{model_id}"
        confidence = str(provenance.get("confidence") or "low")
        if confidence not in {"high", "medium", "low"}:
            confidence = "low"
        if source_type == "model_prior" and confidence == "high":
            confidence = "medium"
        additions, warnings = _sanitize_additions(candidate.get("additions") or {}, allowed, claimed)
        sanitization_warnings.extend(warnings)
        if not isinstance(additions, dict) or not any(additions.get(key) for key in additions):
            continue
        document = {
            "id": f"scs.{_slug(run_id, 'run')}.r{round_number}.{_slug(agent_id, 'agent')}.{index}",
            "created_at": datetime.now(ZoneInfo(settings.timezone)).date().isoformat(),
            "provenance": {"source_type": source_type, "confidence": confidence, "source_ref": source_ref},
            "additions": additions,
        }
        jsonschema.validate(document, contract, format_checker=jsonschema.FormatChecker())
        path = output_dir / f"semantic-{index:02d}.json"
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        semantic_files.append(str(path))
        normalized_changesets.append(document)

    allowed_templates = {path.relative_to(settings.engine_root).as_posix() for path in (settings.engine_root / "business" / "templates").glob("*.yaml")}
    allowed_datasets = {path.relative_to(settings.engine_root).as_posix() for path in (settings.engine_root / "business" / "datasets").glob("*.yaml")}
    approved_pairs: dict[tuple[str, str], set[str]] = {}
    for approved_path in (settings.engine_root / "business" / "models").glob("*.yaml"):
        approved = (yaml.safe_load(approved_path.read_text(encoding="utf-8")) or {}).get("model") or {}
        if approved.get("template_ref") and approved.get("dataset_ref"):
            approved_pairs[(approved["template_ref"], approved["dataset_ref"])] = {str(item) for item in approved.get("outputs") or []}
    business_files: list[str] = []
    for index, wrapper in enumerate(_as_list(raw.get("business_model_candidates")), 1):
        if not isinstance(wrapper, dict):
            sanitization_warnings.append("跳过非法经营模型候选"); continue
        model = dict((wrapper or {}).get("model") or {})
        if not model: continue
        if model.get("template_ref") not in allowed_templates or model.get("dataset_ref") not in allowed_datasets:
            sanitization_warnings.append("跳过引用未批准模板或数据集的经营模型"); continue
        approved_outputs = approved_pairs.get((model["template_ref"], model["dataset_ref"]))
        if not approved_outputs:
            sanitization_warnings.append("跳过未经现有基线验证的模板/数据集组合"); continue
        outputs = [str(item) for item in (model.get("outputs") or []) if str(item) in approved_outputs]
        if not outputs:
            sanitization_warnings.append("跳过没有有效输出变量的经营模型"); continue
        normalized_model = {
            "id": f"business.agent.{_slug(run_id, 'run')}.r{round_number}.{_slug(agent_id, 'agent')}.{index}",
            "name": str(model.get("name") or "Agent经营模型"),
            "domain": str(model.get("domain") or "semiconductor"),
            "period": str(model.get("period") or "month"),
            "currency": str(model.get("currency") or "CNY"),
            "template_ref": model["template_ref"], "dataset_ref": model["dataset_ref"],
            "outputs": outputs,
        }
        path = output_dir / f"business-{index:02d}.yaml"
        path.write_text(yaml.safe_dump({"schema_version": "2.0", "model": normalized_model}, allow_unicode=True, sort_keys=False), encoding="utf-8")
        business_files.append(str(path))

    allowed_models = {path.relative_to(settings.engine_root).as_posix() for path in (settings.engine_root / "business" / "models").glob("*.yaml")}
    scenario_policies: dict[str, dict[str, set[str]]] = {}
    for approved_path in (settings.engine_root / "simulation" / "scenarios").glob("*.yaml"):
        approved = (yaml.safe_load(approved_path.read_text(encoding="utf-8")) or {}).get("scenario") or {}
        policy = scenario_policies.setdefault(str(approved.get("model_ref") or ""), {"inputs": set(), "outputs": set(), "targets": set()})
        policy["inputs"].update(str(item.get("variable")) for item in approved.get("interventions") or [] if item.get("variable"))
        policy["outputs"].update(str(item.get("variable")) for item in approved.get("assertions") or [] if item.get("variable"))
        policy["targets"].update(str(item.get("target_ref")) for item in approved.get("interventions") or [] if item.get("target_ref"))
    simulation_files: list[str] = []
    for index, wrapper in enumerate(_as_list(raw.get("simulation_candidates")), 1):
        if not isinstance(wrapper, dict):
            sanitization_warnings.append("跳过非法仿真候选"); continue
        scenario = dict((wrapper or {}).get("scenario") or {})
        if not scenario:
            continue
        model_ref = str(scenario.get("model_ref") or "")
        if model_ref not in allowed_models:
            raise ValueError(f"仿真候选引用未批准的经营模型：{model_ref}")
        policy = scenario_policies.get(model_ref, {"inputs": set(), "outputs": set(), "targets": set()})
        interventions = []
        for intervention in scenario.get("interventions") or []:
            if intervention.get("operation") not in {"set", "add", "multiply"}:
                sanitization_warnings.append("跳过包含非法 operation 的仿真干预"); continue
            if not isinstance(intervention.get("value"), (int, float)):
                sanitization_warnings.append("跳过 value 非数值的仿真干预"); continue
            if intervention.get("variable") not in policy["inputs"] or intervention.get("target_ref") not in policy["targets"]:
                sanitization_warnings.append(f"跳过未在基线验证的仿真干预变量 {intervention.get('variable')}"); continue
            interventions.append(intervention)
        assertions = []
        for assertion in scenario.get("assertions") or []:
            if assertion.get("type") not in {"nondecreasing", "nonincreasing", "unchanged"}:
                sanitization_warnings.append("跳过非法仿真断言"); continue
            if assertion.get("variable") not in policy["outputs"]:
                sanitization_warnings.append(f"跳过未在基线验证的输出断言 {assertion.get('variable')}"); continue
            assertions.append(assertion)
        if not interventions:
            sanitization_warnings.append("跳过没有有效干预变量的仿真候选"); continue
        scenario["interventions"] = interventions
        scenario["assertions"] = assertions
        scenario["id"] = f"sim.agent.{_slug(run_id, 'run')}.r{round_number}.{_slug(agent_id, 'agent')}.{index}"
        scenario.setdefault("description", "Agent 自动提出的待验证情景；结果不是经营承诺。")
        path = output_dir / f"simulation-{index:02d}.yaml"
        path.write_text(yaml.safe_dump({"schema_version": "2.0", "scenario": scenario}, allow_unicode=True, sort_keys=False), encoding="utf-8")
        simulation_files.append(str(path))

    # Knowledge entries and inference rules are separate governed artifacts.  They
    # are kept out of the OWL changeset so a bad rule/query cannot invalidate valid
    # ontology additions, while still using the same provenance and publish gate.
    knowledge_files: list[str] = []
    known_iris = inventory["subjects"] | proposed["classes"] | proposed["object_properties"] | proposed["datatype_properties"] | proposed["individuals"]
    for index, wrapper in enumerate(_as_list(raw.get("knowledge_entries")), 1):
        entry = dict((wrapper or {}).get("entry") or {}) if isinstance(wrapper, dict) else {}
        entry_id = str(entry.get("id") or "")
        title = str(entry.get("title") or "").strip()
        content = str(entry.get("content") or "").strip()
        if not entry_id or not re.match(r"^urn:pxai:semi:knowledge:[A-Za-z0-9._:%-]+$", entry_id) or not title or len(content) < 40:
            sanitization_warnings.append("跳过不完整知识条目候选"); continue
        related = [str(value) for value in _as_list(entry.get("related_iris")) if str(value) in known_iris]
        refs = [str(value) for value in _as_list(entry.get("source_refs")) if str(value)]
        if source_mode in {"web", "hybrid"}: refs = [value for value in refs if value in urls] or sorted(urls)[:1]
        if not refs and source_mode == "web":
            sanitization_warnings.append(f"跳过知识条目 {entry_id}：缺少网页证据"); continue
        confidence = str(entry.get("confidence") or "low")
        if confidence not in {"high", "medium", "low"}: confidence = "low"
        if source_mode == "model_prior" and confidence == "high": confidence = "medium"
        normalized_entry = {
            "id": entry_id, "title": title, "domain": str(entry.get("domain") or "semiconductor"),
            "summary": str(entry.get("summary") or content[:180]), "content": content,
            "source_refs": refs, "related_iris": related, "confidence": confidence,
            "provenance": {"source_type": "web" if refs else "model_prior", "source_ref": refs[0] if refs else f"model:{model_id}"},
        }
        path = output_dir / f"knowledge-{index:02d}.json"
        path.write_text(json.dumps(normalized_entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        knowledge_files.append(str(path))

    rule_files: list[str] = []
    for index, wrapper in enumerate(_as_list(raw.get("rule_candidates")), 1):
        rule = dict((wrapper or {}).get("rule") or {}) if isinstance(wrapper, dict) else {}
        rule_id = str(rule.get("rule_id") or "")
        implementation = str(rule.get("implementation") or "")
        query = str(rule.get("query") or "").strip()
        if not re.match(r"^R-AUTO-[A-Za-z0-9._-]+$", rule_id) or not rule.get("name") or implementation != "sparql":
            sanitization_warnings.append("跳过不完整推理规则候选"); continue
        if implementation == "sparql" and not re.search(r"(?is)\bconstruct\b", query):
            sanitization_warnings.append(f"跳过规则 {rule_id}：SPARQL 规则必须是 CONSTRUCT"); continue
        if query and re.search(r"\b(?:INSERT|DELETE|LOAD|CLEAR|DROP|CREATE|MOVE|COPY|ADD)\b", query, re.I):
            sanitization_warnings.append(f"跳过规则 {rule_id}：SPARQL 包含危险更新语句"); continue
        confidence = str(rule.get("confidence") or "low")
        if confidence not in {"high", "medium", "low"}: confidence = "low"
        if source_mode == "model_prior" and confidence == "high": confidence = "medium"
        valid_tests = []
        for test_ref in _as_list(rule.get("tests")):
            test_path = str(test_ref).split("::", 1)[0]
            candidate_test = settings.engine_root / test_path
            if test_path and ".." not in Path(test_path).parts and candidate_test.is_file():
                valid_tests.append(str(test_ref))
            elif test_path:
                sanitization_warnings.append(f"规则 {rule_id} 移除不存在的测试引用 {test_ref}")
        normalized_rule = {
            "rule_id": rule_id, "name": str(rule.get("name")), "purpose": str(rule.get("purpose") or ""),
            "implementation": implementation, "query": query, "preconditions": [str(v) for v in _as_list(rule.get("preconditions"))],
            "conclusion": str(rule.get("conclusion") or ""), "required_sources": [str(v) for v in _as_list(rule.get("required_sources"))],
            "tests": valid_tests, "confidence": confidence,
            "provenance": {"source_type": "web" if urls else "model_prior", "source_ref": sorted(urls)[0] if urls else f"model:{model_id}"},
        }
        path = output_dir / f"rule-{index:02d}.json"
        path.write_text(json.dumps(normalized_rule, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        rule_files.append(str(path))

    if not knowledge_files and str(raw.get("summary") or raw.get("customer_pains") or "").strip() and (source_mode != "web" or urls):
        # Keep a concise evidence-backed knowledge record even when a model omits
        # the optional structured section.  This is a normalization fallback, not
        # an invented domain fact: it stores exactly the model's stated summary and
        # pain points with the same provenance boundary.
        fallback_id = f"urn:pxai:semi:knowledge:{_slug(run_id, 'run')}-r{round_number}-{_slug(agent_id, 'agent')}"
        fallback_pains = "\n".join(f"- {item}" for item in _as_list(raw.get("customer_pains"))) or "- 尚需更多证据确认客户痛点。"
        fallback_content = "\n".join([str(raw.get("summary") or "").strip(), fallback_pains]).strip()
        if len(fallback_content) >= 40:
            fallback = {
                "id": fallback_id, "title": str(raw.get("summary") or "本轮研究知识").strip()[:120],
                "domain": "semiconductor", "summary": str(raw.get("summary") or "").strip()[:180],
                "content": fallback_content, "source_refs": sorted(urls)[:3], "related_iris": [],
                "confidence": "medium" if source_mode == "model_prior" else "low",
                "provenance": {"source_type": "web" if urls else "model_prior", "source_ref": sorted(urls)[0] if urls else f"model:{model_id}"},
            }
            path = output_dir / "knowledge-fallback.json"
            path.write_text(json.dumps(fallback, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            knowledge_files.append(str(path))

    article = str(raw.get("scenario_article_markdown") or "").strip()
    pains = "\n".join(f"- {item}" for item in _as_list(raw.get("customer_pains"))) or "- 尚需更多证据确认客户痛点。"
    if len(article) < 120 or "痛点" not in article or "场景" not in article:
        article = f"## 场景与客户痛点\n\n{raw.get('summary', '本轮研究结果')}\n\n{pains}\n\n## 经营与仿真含义\n\n本轮候选必须通过经营模型与仿真门禁后才可发布，不把情景推演描述成经营承诺。\n\n> 证据边界：结论以本轮来源与自动门禁结果为准。\n\n{article}"
    if re.search(r"Bearer\s+[A-Za-z0-9._-]+|webhook/send\?key=|授权码\s*[:：]\s*\S+", article, re.I):
        raise ValueError("场景文章可能包含敏感凭据")
    article_path = output_dir / "scenario-article.md"
    article_path.write_text(article + "\n", encoding="utf-8")
    normalized = {
        "summary": str(raw.get("summary") or ""),
        "customer_pains": [str(item) for item in _as_list(raw.get("customer_pains"))],
        "evidence_notes": [str(item) for item in _as_list(raw.get("evidence_notes"))],
        "semantic_changesets": normalized_changesets,
        "semantic_files": semantic_files,
        "business_files": business_files,
        "simulation_files": simulation_files,
        "knowledge_files": knowledge_files,
        "rule_files": rule_files,
        "article_file": str(article_path),
        "evidence_count": len([item for item in evidence if item.get("fetch_status") == "ok"]),
        "sanitization_warnings": sanitization_warnings,
    }
    normalized["content_sha256"] = hashlib.sha256(json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    (output_dir / "result.json").write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    return normalized


def round_candidate_files(outputs: list[dict]) -> dict[str, list[Path]]:
    return {
        "semantic": [Path(path) for output in outputs for path in output.get("semantic_files") or []],
        "business": [Path(path) for output in outputs for path in output.get("business_files") or []],
        "simulation": [Path(path) for output in outputs for path in output.get("simulation_files") or []],
        "knowledge": [Path(path) for output in outputs for path in output.get("knowledge_files") or []],
        "rules": [Path(path) for output in outputs for path in output.get("rule_files") or []],
        "articles": [Path(output["article_file"]) for output in outputs if output.get("article_file")],
    }
