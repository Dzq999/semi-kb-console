"""把现有 YAML 本体目录与知识库投影为当前 RDF 数据集。

YAML 在迁移期仍可编辑；生成的 TriG 是派生物，不手工修改。类型级条目映射为
CatalogConcept 的子类型，避免把工艺目录误当成现场设备、批次等真实实例。
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

try:
    import yaml  # noqa: F401  # common.py 的明确运行依赖
    from rdflib import BNode, Dataset, Graph, Literal, Namespace, RDF, RDFS, URIRef
    from rdflib.namespace import PROV, SKOS, XSD
except ModuleNotFoundError as exc:
    print(
        f"缺少语义迁移依赖 {exc.name}；请先安装 requirements.txt。",
        file=sys.stderr,
    )
    raise SystemExit(2) from exc

import common as C


SEMI = Namespace("urn:pxai:semi:")
CATALOG_GRAPH = URIRef("urn:pxai:semi:graph:catalog")
RELATION_GRAPH = URIRef("urn:pxai:semi:graph:relations")
PLAYBOOK_GRAPH = URIRef("urn:pxai:semi:graph:playbooks")
PROVENANCE_GRAPH = URIRef("urn:pxai:semi:graph:provenance")
SEMANTIC_ABOX_GRAPH = URIRef("urn:pxai:semi:graph:semantic-abox")
BUSINESS_GRAPH = URIRef("urn:pxai:semi:graph:business-simulation")

TYPE_MAP = {
    "Process": SEMI.Process,
    "Stage": SEMI.Stage,
    "Equipment": SEMI.EquipmentType,
    "Material": SEMI.MaterialType,
    "Parameter": SEMI.ProcessParameter,
    "Metric": SEMI.Metric,
    "State": SEMI.Status,
    "Anomaly": SEMI.Anomaly,
    "Cause": SEMI.Cause,
    "Action": SEMI.Action,
    "Route": SEMI.Route,
    "RiskNode": SEMI.RiskNode,
    # ERP 财务会计域（erp-financial）
    "CompanyCode": SEMI.CompanyCode,
    "ChartOfAccounts": SEMI.ChartOfAccounts,
    "GLAccount": SEMI.GLAccount,
    "AccountingDocument": SEMI.AccountingDocument,
    "JournalLineItem": SEMI.JournalLineItem,
    "CostCenter": SEMI.CostCenter,
    "ProfitCenter": SEMI.ProfitCenter,
    "BusinessPartner": SEMI.BusinessPartner,
    "FixedAsset": SEMI.FixedAsset,
    "BankAccount": SEMI.BankAccount,
    "TaxCode": SEMI.TaxCode,
    "FiscalPeriod": SEMI.FiscalPeriod,
    "AccountBalance": SEMI.AccountBalance,
    "LedgerSubsystem": SEMI.LedgerSubsystem,
    "PostingEvent": SEMI.PostingEvent,
    "CarryForwardEvent": SEMI.CarryForwardEvent,
    # ERP 订单到收款域（erp-sales-o2c）
    "Quotation": SEMI.Quotation,
    "SalesOrder": SEMI.SalesOrder,
    "SalesOrderItem": SEMI.SalesOrderItem,
    "DeliveryDocument": SEMI.DeliveryDocument,
    "BillingDocument": SEMI.BillingDocument,
    "CreditManagement": SEMI.CreditManagement,
    "PricingCondition": SEMI.PricingCondition,
    "AccountsReceivable": SEMI.AccountsReceivable,
    "ContractLiability": SEMI.ContractLiability,
    "RevenueRecognitionKey": SEMI.RevenueRecognitionKey,
    "QuantityContract": SEMI.QuantityContract,
    "GoodsIssueEvent": SEMI.GoodsIssueEvent,
    "RevenueRecognitionEvent": SEMI.RevenueRecognitionEvent,
    "ClearingEvent": SEMI.ClearingEvent,
}

RELATION_MAP = {
    "belongs_to": SEMI.partOf,
    "precedes": SEMI.precedesProcess,
    "performed_on": SEMI.performedOnMaterial,
    "controls": SEMI.controlsProcess,
    "measured_by": SEMI.measuredByMetric,
    "may_cause": SEMI.mayCause,
    "detected_at": SEMI.detectedAt,
    "mitigated_by": SEMI.mitigatedBy,
    "blocks": SEMI.blocks,
    "refines": SKOS.broader,
    "analogous_to": SEMI.analogousTo,
    # ERP 财务/O2C 关系（未列出的 type 自动落 iri("relation", type)，此处只登记语义已建模的桥接边）
    "uses_chart_of_accounts": SEMI.usesChartOfAccounts,
    "account_in_chart": SEMI.accountInChart,
    "parent_account": SEMI.parentAccount,
    "has_line_item": SEMI.hasLineItem,
    "line_item_account": SEMI.lineItemAccount,
    "document_in_company_code": SEMI.documentInCompanyCode,
    "document_in_period": SEMI.documentInPeriod,
    "document_for_partner": SEMI.documentForPartner,
    "reversal_of": SEMI.reversalOf,
    "allocates_to_cost_center": SEMI.allocatesToCostCenter,
    "cost_center_in_profit_center": SEMI.costCenterInProfitCenter,
    "carries_forward_to": SEMI.carriesForwardTo,
    "posts_document": SEMI.postsDocument,
    "quotation_for_customer": SEMI.quotationForCustomer,
    "order_from_quotation": SEMI.orderFromQuotation,
    "order_for_customer": SEMI.orderForCustomer,
    "has_order_item": SEMI.hasOrderItem,
    "order_item_for_product": SEMI.orderItemForProduct,
    "order_item_rev_rec_key": SEMI.orderItemRevRecKey,
    "delivery_for_order": SEMI.deliveryForOrder,
    "billing_for_delivery": SEMI.billingForDelivery,
    "priced_by_condition": SEMI.pricedByCondition,
    "order_under_credit_check": SEMI.orderUnderCreditCheck,
    "generates_receivable": SEMI.generatesReceivable,
    "billing_uses_tax": SEMI.billingUsesTax,
    "billing_in_period": SEMI.billingInPeriod,
    "receivable_settled_to_bank": SEMI.receivableSettledToBank,
    "outsources_to_vendor": SEMI.outsourcesToVendor,
    "contract_liability_posts_to": SEMI.contractLiabilityPostsTo,
    "issues_delivery": SEMI.issuesDelivery,
    "recognizes_for_billing": SEMI.recognizesForBilling,
    "clears_receivable": SEMI.clearsReceivable,
    "posts_accounting_document": SEMI.postsAccountingDocument,
}


def iri(kind: str, identifier: str) -> URIRef:
    return URIRef(f"urn:pxai:semi:{kind}:{quote(identifier, safe='._-')}")


def semantic_ref(value: str) -> URIRef:
    return URIRef(value) if value.startswith("urn:pxai:semi:") else iri("catalog", value)


def split_instances_provenance(dataset: Dataset) -> tuple[Graph, Graph]:
    """按【节点类型】把数据集拆成 (纯实例图, 溯源图)。

    prov:Entity / rdf:Statement 主语的全部三元组归溯源图，其余归实例图。按类型而非
    命名图判定，故无论溯源落在哪个图都能统一捕获（含 changeset 落在 SEMANTIC_ABOX_GRAPH
    的溯源），也能把历史混入实例投影的溯源一次性扫出（自愈）；对已分离输入再跑幂等。"""
    prov_subjects = {s for s, _, o, _ in dataset.quads((None, RDF.type, None, None))
                     if o in (PROV.Entity, RDF.Statement)}
    merged, provenance = Graph(), Graph()
    for s, p, o, _ in dataset.quads((None, None, None, None)):
        (provenance if s in prov_subjects else merged).add((s, p, o))
    return merged, provenance


def add_text(graph, subject: URIRef | BNode, predicate: URIRef, value, lang=None) -> None:
    if value is not None and str(value).strip():
        graph.add((subject, predicate, Literal(str(value), lang=lang)))


def add_provenance(dataset: Dataset, subject: URIRef | BNode, provenance: dict, source_file: str) -> None:
    graph = dataset.graph(PROVENANCE_GRAPH)
    assertion = iri("provenance", hashlib.sha256(str(subject).encode("utf-8")).hexdigest()[:24])
    graph.add((assertion, RDF.type, PROV.Entity))
    graph.add((assertion, PROV.specializationOf, subject))
    add_text(graph, assertion, SEMI.sourceRef, provenance.get("ref") or source_file)
    add_text(graph, assertion, SEMI.confidence, provenance.get("confidence"))
    add_text(graph, assertion, SEMI.sourceType, provenance.get("source_type"))
    add_text(graph, assertion, SEMI.createdAt, provenance.get("created_at"))


def migrate_entity(dataset: Dataset, entity: dict) -> URIRef:
    global _migrate_entity_debug_counter
    graph = dataset.graph(CATALOG_GRAPH)
    subject = iri("catalog", entity["id"])
    graph.add((subject, RDF.type, TYPE_MAP.get(entity.get("type"), SEMI.CatalogConcept)))
    graph.add((subject, RDF.type, SEMI.CatalogConcept))
    graph.add((subject, SEMI.legacyId, Literal(entity["id"])))
    graph.add((subject, SKOS.notation, Literal(entity["id"])))
    add_text(graph, subject, SKOS.prefLabel, entity.get("name_zh"), "zh")
    add_text(graph, subject, SKOS.prefLabel, entity.get("name_en"), "en")
    add_text(graph, subject, SKOS.definition, entity.get("description"), "zh")
    add_text(graph, subject, SEMI.unitCode, entity.get("unit"))
    add_text(graph, subject, SEMI.severity, entity.get("severity"))
    for alias in entity.get("aliases") or []:
        add_text(graph, subject, SKOS.altLabel, alias)
    # 把 attributes 字典中的键值对编译为 datatype property 断言（如 processSegment）
    attrs = entity.get("attributes") or {}
    # DEBUG: 检查 fab.stage.diffusion 是否有 processSegment
    if entity["id"] == "fab.stage.diffusion":
        print(f"DEBUG [fab.stage.diffusion]: attributes={attrs}, processSegment={attrs.get('processSegment')}")
    for key, value in attrs.items():
        if value is not None and str(value).strip():
            graph.add((subject, SEMI[key], Literal(str(value))))
            if entity["id"] == "fab.stage.diffusion" and key == "processSegment":
                print(f"  ✓ Added processSegment={value} to {entity['id']}")
    add_provenance(dataset, subject, entity.get("provenance") or {}, entity.get("_file", ""))
    return subject


def relation_statement(dataset: Dataset, row: dict, index: int) -> None:
    graph = dataset.graph(RELATION_GRAPH)
    provenance_graph = dataset.graph(PROVENANCE_GRAPH)
    source = iri("catalog", row["from"]) if not C.is_external(row["from"]) else iri("external", row["from"])
    target = iri("catalog", row["to"]) if not C.is_external(row["to"]) else iri("external", row["to"])
    predicate = RELATION_MAP.get(row["type"], iri("relation", row["type"]))
    graph.add((source, predicate, target))

    # RDF reification keeps edge-level provenance without depending on RDF-star support.
    key = (f"{row['from']}|{row['type']}|{row['to']}|"
           f"{row.get('_file', '')}|{row.get('_field', '')}")
    statement = iri("assertion", hashlib.sha256(key.encode("utf-8")).hexdigest()[:24])
    provenance_graph.add((statement, RDF.type, RDF.Statement))
    provenance_graph.add((statement, RDF.subject, source))
    provenance_graph.add((statement, RDF.predicate, predicate))
    provenance_graph.add((statement, RDF.object, target))
    add_text(provenance_graph, statement, SEMI.confidence, (row.get("provenance") or {}).get("confidence"))
    add_text(provenance_graph, statement, SEMI.likelihood, row.get("likelihood"))
    add_text(provenance_graph, statement, RDFS.comment, row.get("note"), "zh")
    add_text(provenance_graph, statement, SEMI.sourceRef,
             (row.get("provenance") or {}).get("ref") or row.get("_file"))


def migrate_playbook(dataset: Dataset, case: dict) -> None:
    graph = dataset.graph(PLAYBOOK_GRAPH)
    playbook = iri("playbook", case["id"])
    graph.add((playbook, RDF.type, SEMI.DiagnosticPlaybook))
    graph.add((playbook, SEMI.legacyId, Literal(case["id"])))
    add_text(graph, playbook, RDFS.label, case.get("title"), "zh")
    graph.add((playbook, SEMI.diagnosesAnomaly, iri("catalog", case["anomaly_ref"])))
    if case.get("detected_at_ref"):
        graph.add((playbook, SEMI.hasDetectionProcess, iri("catalog", case["detected_at_ref"])))
    for symptom in case.get("symptoms") or []:
        add_text(graph, playbook, SEMI.symptomText, symptom, "zh")

    for pos, item in enumerate(case.get("possible_causes") or [], 1):
        node = iri("cause-assertion", f"{case['id']}:{pos}")
        cause = iri("catalog", item["cause_ref"])
        graph.add((node, RDF.type, SEMI.PossibleCauseAssertion))
        graph.add((node, SEMI.assertsCause, cause))
        graph.add((playbook, SEMI.hasCauseAssertion, node))
        graph.add((playbook, SEMI.hasPossibleCause, cause))
        add_text(graph, node, SEMI.likelihood, item.get("likelihood"))
        add_text(graph, node, SEMI.discriminatorText, item.get("discriminator"), "zh")

    for pos, item in enumerate(case.get("detection") or [], 1):
        node = iri("detection", f"{case['id']}:{pos}")
        metric = iri("catalog", item["metric_ref"])
        graph.add((node, RDF.type, SEMI.DetectionInstruction))
        graph.add((node, SEMI.detectsWithMetric, metric))
        graph.add((playbook, SEMI.hasDetectionInstruction, node))
        graph.add((playbook, SEMI.hasDetectionMetric, metric))
        add_text(graph, node, SEMI.signalText, item.get("signal"), "zh")

    for pos, item in enumerate(case.get("actions") or [], 1):
        node = iri("action-instruction", f"{case['id']}:{pos}")
        action = iri("catalog", item["action_ref"])
        graph.add((node, RDF.type, SEMI.ActionInstruction))
        graph.add((node, SEMI.instructsAction, action))
        graph.add((playbook, SEMI.hasActionInstruction, node))
        graph.add((playbook, SEMI.hasDiagnosticAction, action))
        add_text(graph, node, SEMI.actionConditionText, item.get("condition"), "zh")
        if item.get("order") is not None:
            graph.add((node, SEMI.actionOrder, Literal(int(item["order"]), datatype=XSD.integer)))

    impact = case.get("impact") or {}
    add_text(graph, playbook, SEMI.economicImpactText, impact.get("economic"), "zh")
    add_provenance(dataset, playbook, case.get("provenance") or {}, case.get("_file", ""))


def migrate_scenarios(dataset: Dataset) -> int:
    path = C.ROOT / "knowledge" / "scenarios" / "current.json"
    if not path.is_file():
        return 0
    doc = json.loads(path.read_text(encoding="utf-8"))
    graph = dataset.graph(PLAYBOOK_GRAPH)
    capability_path = C.ROOT / "ontology" / "application-capabilities.json"
    capability_doc = json.loads(capability_path.read_text(encoding="utf-8"))
    for item in capability_doc.get("l1_capabilities") or []:
        node = iri("capability", item["id"])
        graph.add((node, RDF.type, SEMI.L1Capability))
        add_text(graph, node, RDFS.label, item.get("name"))
    for item in capability_doc.get("l2_scenarios") or []:
        node = iri("scenario", item["id"])
        graph.add((node, RDF.type, SEMI.L2Scenario))
        add_text(graph, node, RDFS.label, item.get("name"))
        for capability_id in item.get("composes") or []:
            graph.add((node, SEMI.composesCapability, iri("capability", capability_id)))
    for item in doc.get("scenarios") or []:
        card = iri("scenario-card", item["id"])
        problem = iri("customer-problem", item["id"])
        pain = iri("pain-point", item["id"])
        l1_ids = item.get("l1_capabilities") or [item.get("l1_capability")]
        l1_ids = [value for value in l1_ids if value]
        l1_names = item.get("l1_capability_names") or []
        l2 = iri("scenario", item["l2_scenario"])
        graph.add((card, RDF.type, SEMI.BusinessScenarioCard))
        graph.add((problem, RDF.type, SEMI.CustomerProblem))
        graph.add((pain, RDF.type, SEMI.PainPoint))
        graph.add((l2, RDF.type, SEMI.L2Scenario))
        graph.add((card, SEMI.addressesProblem, problem))
        graph.add((card, SEMI.describesPainPoint, pain))
        graph.add((card, SEMI.supportsCapability, l2))
        add_text(graph, l2, RDFS.label, item.get("l2_scenario_name"), "zh")
        for pos, l1_id in enumerate(l1_ids):
            l1 = iri("capability", l1_id)
            graph.add((l1, RDF.type, SEMI.L1Capability))
            graph.add((l2, SEMI.composesCapability, l1))
            if pos < len(l1_names):
                add_text(graph, l1, RDFS.label, l1_names[pos], "zh")
        add_text(graph, card, RDFS.label, item.get("title"), "zh")
        add_text(graph, problem, SEMI.problemText, item.get("customer_problem"), "zh")
        add_text(graph, pain, SEMI.painPointText, item.get("pain_point"), "zh")
        add_text(graph, card, SEMI.scenarioReadiness, item.get("simulation_readiness"))
        add_text(graph, card, SEMI.validationStatus, item.get("evidence_state"))
        for ref in item.get("ontology_refs") or []:
            graph.add((card, SEMI.usesOntologyEntity, semantic_ref(ref)))
        if item.get("business_model_ref"):
            model = iri("business-model", item["business_model_ref"])
            graph.add((model, RDF.type, SEMI.BusinessModel))
            add_text(graph, model, SEMI.sourceRef, item["business_model_ref"])
        if item.get("simulation_scenario_ref"):
            simulation = iri("simulation-scenario", item["simulation_scenario_ref"])
            graph.add((card, SEMI.usesSimulationScenario, simulation))
    return len(doc.get("scenarios") or [])


def migrate_business_simulation(dataset: Dataset) -> tuple[int, int]:
    graph = dataset.graph(BUSINESS_GRAPH)
    models: dict[str, URIRef] = {}
    variables: dict[tuple[str, str], URIRef] = {}
    for path in sorted((C.ROOT / "business" / "models").glob("*.yaml")):
        model = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("model") or {}
        if not model.get("id"): continue
        model_ref = path.relative_to(C.ROOT).as_posix()
        node = iri("business-model", model["id"]); models[model_ref] = node
        graph.add((node, RDF.type, SEMI.BusinessModel)); add_text(graph, node, RDFS.label, model.get("name"), "zh")
        add_text(graph, node, SEMI.sourceRef, model_ref)
        dataset_ref = model.get("dataset_ref")
        dataset_doc = (yaml.safe_load((C.ROOT / dataset_ref).read_text(encoding="utf-8")) or {}).get("dataset") or {} if dataset_ref else {}
        for item in dataset_doc.get("values") or []:
            variable = iri("business-variable", f"{model['id']}:{item['id']}")
            variables[(model_ref, item["id"])] = variable
            graph.add((variable, RDF.type, SEMI.BusinessVariable)); graph.add((node, SEMI.hasBusinessVariable, variable))
            graph.add((variable, SEMI.identifier, Literal(item["id"])))
            add_text(graph, variable, SEMI.unitCode, item.get("unit")); add_text(graph, variable, SEMI.sourceType, item.get("source"))
            add_text(graph, variable, SEMI.sourceRef, f"{dataset_ref}#{item['id']}")
            if isinstance(item.get("value"), (int, float)):
                graph.add((variable, SEMI.numericValue, Literal(str(item["value"]), datatype=XSD.decimal)))
    scenario_count = 0
    for path in sorted((C.ROOT / "simulation" / "scenarios").glob("*.yaml")):
        scenario = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("scenario") or {}
        if not scenario.get("id"): continue
        scenario_count += 1; node = iri("simulation-scenario", scenario["id"])
        graph.add((node, RDF.type, SEMI.SimulationScenario)); add_text(graph, node, RDFS.label, scenario.get("name"), "zh")
        model_ref = scenario.get("model_ref")
        if model_ref in models: graph.add((node, SEMI.usesBusinessModel, models[model_ref]))
        investment = (scenario.get("investment") or {}).get("one_time")
        if investment is not None: graph.add((node, SEMI.oneTimeInvestment, Literal(str(investment), datatype=XSD.decimal)))
        for pos, item in enumerate(scenario.get("interventions") or [], 1):
            intervention = iri("intervention", f"{scenario['id']}:{pos}")
            graph.add((intervention, RDF.type, SEMI.Intervention)); graph.add((node, SEMI.hasIntervention, intervention))
            add_text(graph, intervention, SEMI.interventionOperation, item.get("operation"))
            if item.get("value") is not None: graph.add((intervention, SEMI.interventionValue, Literal(str(item["value"]), datatype=XSD.decimal)))
            if item.get("target_ref"): graph.add((intervention, SEMI.targetsOntologyEntity, iri("catalog", item["target_ref"])))
            variable = variables.get((model_ref, item.get("variable")))
            if variable: graph.add((intervention, SEMI.changesVariable, variable))
    return len(models), scenario_count


def main() -> int:
    C.setup_console()
    entities, duplicates = C.load_entities()
    if duplicates:
        print("存在重复 ID，拒绝迁移：" + "; ".join(duplicates), file=sys.stderr)
        return 1
    relations = C.load_relations()
    derived = C.derived_relations(entities, C.load_meta())
    cases = C.load_kb()

    dataset = Dataset()
    dataset.bind("semi", SEMI)
    dataset.bind("prov", PROV)
    dataset.bind("skos", SKOS)
    for entity in entities.values():
        migrate_entity(dataset, entity)
    for index, row in enumerate(relations + derived, 1):
        relation_statement(dataset, row, index)
    for case in cases:
        migrate_playbook(dataset, case)
    scenario_count = migrate_scenarios(dataset)
    business_model_count, simulation_scenario_count = migrate_business_simulation(dataset)
    abox_graph = dataset.graph(SEMANTIC_ABOX_GRAPH)
    abox_before = len(abox_graph)
    semantic_dir = C.ROOT / "knowledge" / "semantic"
    provenance_ttl = semantic_dir / "provenance.ttl"
    for path in sorted(semantic_dir.glob("*.ttl")):
        if path.name == "provenance.ttl":
            continue  # 溯源单独读入 PROVENANCE_GRAPH，不混进实例 abox
        abox_graph.parse(path, format="turtle")
    semantic_abox_triples = len(abox_graph) - abox_before
    # 保住上一轮分离出的 changeset 溯源：读回 PROVENANCE_GRAPH，跨轮不丢、且回到正确的图身份
    if provenance_ttl.is_file():
        dataset.graph(PROVENANCE_GRAPH).parse(provenance_ttl, format="turtle")

    out = C.ROOT / "build" / "semantic" / "current.trig"
    out.parent.mkdir(parents=True, exist_ok=True)
    dataset.serialize(destination=out, format="trig", encoding="utf-8")

    # 溯源分离：把 prov:Entity / rdf:Statement 记账节点从实例投影中剥离，current.ttl 只留
    # 真实实例，溯源单独落 provenance.ttl。逻辑见 split_instances_provenance（按类型分离，
    # 首轮自愈历史混入、再跑幂等）。current.trig 全量权威 Dataset 已在上方写出，保持不变。
    merged, provenance = split_instances_provenance(dataset)
    semantic_dir.mkdir(parents=True, exist_ok=True)
    merged.serialize(destination=semantic_dir / "current.ttl", format="turtle", encoding="utf-8")
    provenance.serialize(destination=provenance_ttl, format="turtle", encoding="utf-8")

    report = {
        "status": "pass",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "output": out.relative_to(C.ROOT).as_posix(),
        "catalog_concepts": len(entities),
        "explicit_relations": len(relations),
        "derived_relations": len(derived),
        "diagnostic_playbooks": len(cases),
        "business_scenarios": scenario_count,
        "business_models": business_model_count,
        "simulation_scenarios": simulation_scenario_count,
        "semantic_abox_triples": semantic_abox_triples,
        "quads": len(dataset),
    }
    report_path = C.ROOT / "build" / "reports" / "semantic-migration.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"语义迁移通过：目录概念 {len(entities)} | 关系 {len(relations) + len(derived)} "
        f"| 诊断知识 {len(cases)} | 四元组 {len(dataset)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
