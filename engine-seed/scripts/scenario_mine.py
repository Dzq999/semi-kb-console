"""从当前索引确定性地产出业务场景卡与客户痛点文章。"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JSON_OUT = ROOT / "knowledge" / "scenarios" / "current.json"
ARTICLE_OUT = ROOT / "knowledge" / "articles" / "current-scenarios.md"


def setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try: stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError): pass


def clean(value: str) -> str:
    return value.strip().strip("'\"")


def fallback_sources() -> tuple[list[dict], dict, bytes]:
    """依赖未安装时也能从稳定字段生成场景；完整链仍由 build_index 校验。"""
    records: list[dict] = []
    raw_parts: list[bytes] = []
    entities: dict[str, dict] = {}
    for path in sorted((ROOT / "ontology").glob("*/entities/*.yaml")):
        raw_parts.append(path.read_bytes())
        lines = path.read_text(encoding="utf-8").splitlines()
        starts = [i for i, line in enumerate(lines) if re.match(r"^  - id:\s*", line)] + [len(lines)]
        for pos, end in zip(starts, starts[1:]):
            block = lines[pos:end]
            item = {"id": clean(block[0].split(":", 1)[1]), "kind": "entity", "affects": []}
            for line in block[1:]:
                match = re.match(r"^    (type|domain|name_zh|severity):\s*(.*)$", line)
                if match: item[match.group(1)] = clean(match.group(2))
                affects = re.search(r"affects:\s*\[([^]]+)\]", line)
                if affects: item["affects"] = [clean(x) for x in affects.group(1).split(",")]
            entities[item["id"]] = item; records.append(item)
    for path in sorted((ROOT / "kb").glob("*/*.yaml")):
        raw_parts.append(path.read_bytes())
        lines = path.read_text(encoding="utf-8").splitlines()
        starts = [i for i, line in enumerate(lines) if re.match(r"^  - id:\s*kb\.", line)] + [len(lines)]
        for pos, end in zip(starts, starts[1:]):
            block = lines[pos:end]
            item = {"id": clean(block[0].split(":", 1)[1]), "kind": "kb_case", "cause_refs": [], "action_refs": []}
            for line in block[1:]:
                match = re.match(r"^    (domain|title|anomaly_ref|detected_at_ref|severity):\s*(.*)$", line)
                if match: item["name_zh" if match.group(1) == "title" else match.group(1)] = clean(match.group(2))
                cause = re.search(r"cause_ref:\s*([^\s#]+)", line)
                action = re.search(r"action_ref:\s*([^\s#]+)", line)
                if cause: item["cause_refs"].append(clean(cause.group(1)))
                if action: item["action_refs"].append(clean(action.group(1)))
            records.append(item)
    edges: list[dict] = []
    for path in sorted((ROOT / "ontology").glob("*/relations/*.yaml")):
        raw_parts.append(path.read_bytes())
        lines = path.read_text(encoding="utf-8").splitlines()
        current: dict = {}
        for line in lines:
            inline = re.search(r"\{from:\s*([^,]+),\s*type:\s*([^,]+),\s*to:\s*([^,}]+)", line)
            if inline:
                edges.append({"from": clean(inline.group(1)), "type": clean(inline.group(2)), "to": clean(inline.group(3))})
                continue
            start = re.match(r"^  - from:\s*(.*)$", line)
            field = re.match(r"^    (type|to):\s*(.*)$", line)
            if start:
                if current.get("from") and current.get("type") and current.get("to"): edges.append(current)
                current = {"from": clean(start.group(1))}
            elif field and current:
                current[field.group(1)] = clean(field.group(2))
        if current.get("from") and current.get("type") and current.get("to"): edges.append(current)
    out: dict[str, list[dict]] = {}
    for edge in edges: out.setdefault(edge["from"], []).append({"type": edge["type"], "to": edge["to"]})
    return records, {"out_adjacency": out, "edges": edges, "routes": {}}, b"".join(raw_parts)


def append_semantic_playbooks(records: list[dict]) -> bytes:
    path = ROOT / "knowledge" / "semantic" / "current.ttl"
    if not path.is_file(): return b""
    try:
        from rdflib import Graph, Namespace, RDF, RDFS
    except ModuleNotFoundError:
        return path.read_bytes()
    semi = Namespace("urn:pxai:semi:")
    graph = Graph(); graph.parse(path, format="turtle")
    known = {x.get("id") for x in records}
    for subject in graph.subjects(RDF.type, semi.DiagnosticPlaybook):
        sid = str(subject)
        if sid in known: continue
        anomaly = next(graph.objects(subject, semi.diagnosesAnomaly), None)
        records.append({
            "id": sid, "kind": "kb_case",
            "name_zh": str(next(graph.objects(subject, RDFS.label), sid)),
            "domain": str(next(graph.objects(subject, semi.domainCode), "fab")),
            "anomaly_ref": str(anomaly) if anomaly else None,
            "cause_refs": [str(x) for x in graph.objects(subject, semi.hasPossibleCause)],
            "action_refs": [str(x) for x in graph.objects(subject, semi.hasDiagnosticAction)],
            "confidence": str(next(graph.objects(subject, semi.confidence), "medium")),
            "source_type": str(next(graph.objects(subject, semi.sourceType), "model_prior")),
        })
    return path.read_bytes()


def select_l2(case: dict, anomaly: dict) -> str:
    text = " ".join(str(case.get(key) or "") for key in ("id", "name_zh", "anomaly_ref"))
    text += " " + str(anomaly.get("name_zh") or "")
    lowered = text.lower()
    if "wip" in lowered or "堆积" in text or "qtime" in lowered:
        return "wafer_resume"
    if "_down" in lowered or "停机" in text or "设备异常" in text:
        return "auto_tool_matching"
    return "auto_dn"


def main() -> int:
    setup_console()
    index_path, graph_path = ROOT / "build" / "index.json", ROOT / "build" / "graph.json"
    if index_path.is_file() and graph_path.is_file():
        index = json.loads(index_path.read_text(encoding="utf-8"))
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
        records = index.get("records") or []
        source_bytes = b""
    else:
        records, graph, source_bytes = fallback_sources()
    source_bytes += append_semantic_playbooks(records)
    capability_path = ROOT / "ontology" / "application-capabilities.json"
    capability_doc = json.loads(capability_path.read_text(encoding="utf-8"))
    source_bytes += capability_path.read_bytes()
    l1_by_id = {item["id"]: item for item in capability_doc.get("l1_capabilities") or []}
    l2_by_id = {item["id"]: item for item in capability_doc.get("l2_scenarios") or []}
    entities = {x["id"]: x for x in records if x.get("kind") == "entity"}
    cases = [x for x in records if x.get("kind") == "kb_case"]
    out_edges = graph.get("out_adjacency") or {}
    canonical = json.dumps(
        {"records": records, "edges": graph.get("edges") or [], "routes": graph.get("routes") or {}},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8") + source_bytes
    fingerprint = hashlib.sha256(canonical).hexdigest()
    alignment_path = ROOT / "build" / "reports" / "source-alignment.json"
    vfab_state = "awaiting_source"
    if alignment_path.is_file():
        vfab_state = (json.loads(alignment_path.read_text(encoding="utf-8")).get("vfab") or {}).get("state", vfab_state)

    cards = []
    for case in sorted(cases, key=lambda x: x.get("id") or ""):
        anomaly = entities.get(case.get("anomaly_ref"), {})
        domain = case.get("domain") or anomaly.get("domain")
        causes = [entities.get(x, {}).get("name_zh") or x for x in case.get("cause_refs") or []]
        actions = [entities.get(x, {}).get("name_zh") or x for x in case.get("action_refs") or []]
        blocked = [entities.get(x.get("to"), {}).get("name_zh") or x.get("to")
                   for x in out_edges.get(case.get("anomaly_ref"), []) if x.get("type") == "blocks"]
        affects = anomaly.get("affects") or []
        model_ref = f"business/models/{'fab' if domain == 'fab' else 'ap'}-baseline.yaml"
        model_exists = (ROOT / model_ref).is_file()
        simulation_id = ("sim.fab.lithography_capacity_recovery" if domain == "fab"
                         else "sim.ap.bonding_yield_improvement")
        simulation_file = (ROOT / "simulation" / "scenarios" /
                           ("fab-capacity-recovery.yaml" if domain == "fab" else "ap-yield-improvement.yaml"))
        evidence = "supported" if case.get("confidence") == "high" else "candidate"
        l2_id = select_l2(case, anomaly)
        l2 = l2_by_id[l2_id]
        l1_ids = list(l2.get("composes") or [])
        cards.append({
            "id": "scenario." + str(case.get("id") or "").removeprefix("kb."),
            "title": case.get("name_zh"),
            "domain": domain,
            "l1_capabilities": l1_ids,
            "l1_capability_names": [l1_by_id[item]["name"] for item in l1_ids],
            "l2_scenario": l2_id,
            "l2_scenario_name": l2["name"],
            "scenario_subject": anomaly.get("name_zh") or case.get("anomaly_ref"),
            "customer_problem": f"生产现场出现{anomaly.get('name_zh') or case.get('name_zh')}，需要快速定位影响与处置。",
            "pain_point": f"候选原因分散，可能阻塞{('、'.join(blocked)) if blocked else '后续生产'}并影响{('、'.join(affects)) if affects else '良率、周期或产出'}。",
            "candidate_causes": causes,
            "candidate_actions": actions,
            "ontology_refs": [x for x in [case.get("anomaly_ref"), case.get("detected_at_ref")] if x],
            "playbook_ref": case.get("id"),
            "business_model_ref": model_ref if model_exists else None,
            "simulation_scenario_ref": simulation_id if simulation_file.is_file() else None,
            "simulation_readiness": "scenario_ready" if simulation_file.is_file() else ("model_ready_inputs_pending" if model_exists else "model_required"),
            "evidence_state": evidence,
            "internal_feature_validation": "structural_support",
            "vfab_validation": vfab_state,
        })

    payload = {"source_fingerprint": fingerprint, "scenario_count": len(cards), "scenarios": cards}
    json_text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    article = ["# 当前业务场景与客户痛点", "", f"共 {len(cards)} 个候选场景。数值结论须在现场输入与仿真校验通过后使用。", ""]
    for card in cards:
        article.extend([
            f"## {card['title']}", "",
            f"L2 场景：{card['l2_scenario_name']}；L1 能力：{'、'.join(card['l1_capability_names'])}。", "",
            card["customer_problem"], "",
            f"痛点：{card['pain_point']}", "",
            f"处置候选：{'、'.join(card['candidate_actions']) or '待补充'}。",
            f"校验状态：内部特征={card['internal_feature_validation']}；vFab={card['vfab_validation']}；仿真={card['simulation_readiness']}。",
            "",
        ])
    article_text = "\n".join(article).rstrip() + "\n"
    for path, text in ((JSON_OUT, json_text), (ARTICLE_OUT, article_text)):
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
    print(f"业务场景沉淀通过：场景 {len(cards)} | 文章 {ARTICLE_OUT.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
