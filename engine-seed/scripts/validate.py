"""本体与知识库校验。逐条实现 meta-schema.yaml 的 validation_rules。

用法：
    python scripts/validate.py            # 全量校验
    python scripts/validate.py --quiet    # 只报 error
退出码：0 通过；1 存在 error。
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict

import common as C

ISSUES: list[tuple[str, str, str]] = []   # (severity, rule, message)


def add(sev: str, rule: str, msg: str) -> None:
    ISSUES.append((sev, rule, msg))


def check_entities(entities: dict, dup: list[str], meta: dict) -> None:
    pattern = re.compile(meta["id_rules"]["pattern"])
    types = meta["entity_types"]
    slug_of = {t: spec["type_slug"] for t, spec in types.items()}
    required = set(meta["entity_required_fields"])
    optional = set(meta["entity_optional_fields"])
    allowed = required | optional | {"_file"}
    prov_schema = meta["provenance_schema"]
    hook_allowed = set(meta["economic_hooks_schema"]["affects"]["allowed"])

    for item in dup:
        add("error", "R001", f"ID 重复：{item}")

    for eid, ent in entities.items():
        where = ent["_file"]

        # R002 ID 格式与 type 一致性
        if not pattern.match(eid):
            add("error", "R002", f"{eid} ID 不符合规范 ({where})")
        etype = ent.get("type")
        if etype not in types:
            add("error", "R003", f"{eid} type={etype!r} 不在允许的实体类型内 ({where})")
        else:
            mid = eid.split(".")[1] if eid.count(".") >= 2 else ""
            if mid != slug_of[etype]:
                add("error", "R002",
                    f"{eid} 中段 '{mid}' 与 type={etype} 的 type_slug '{slug_of[etype]}' 不一致 ({where})")

        # R003 字段齐全 / 无未知字段
        for field in required - set(ent):
            add("error", "R003", f"{eid} 缺少必填字段 '{field}' ({where})")
        for field in set(ent) - allowed:
            add("error", "R003", f"{eid} 含未知字段 '{field}' ({where})")

        # R007 domain 与 ID 前缀一致
        prefix = eid.split(".")[0] if "." in eid else ""
        if ent.get("domain") != prefix:
            add("error", "R007", f"{eid} domain={ent.get('domain')!r} 与 ID 前缀 '{prefix}' 不一致 ({where})")

        # R006 provenance
        prov = ent.get("provenance") or {}
        if not prov:
            add("error", "R006", f"{eid} 缺少 provenance ({where})")
        else:
            st = prov.get("source_type")
            if st not in prov_schema["source_type"]["allowed"]:
                add("error", "R006", f"{eid} provenance.source_type={st!r} 非法 ({where})")
            conf = prov.get("confidence")
            if conf not in prov_schema["confidence"]["allowed"]:
                add("error", "R006", f"{eid} provenance.confidence={conf!r} 非法 ({where})")
            if not prov.get("created_at"):
                add("error", "R006", f"{eid} provenance.created_at 为空 ({where})")
            if st == "web" and not prov.get("ref"):
                add("error", "R006", f"{eid} 来源为 web 但未记录 ref (URL) ({where})")

        # R011 economic_hooks
        hooks = ent.get("economic_hooks") or {}
        for aff in hooks.get("affects") or []:
            if aff not in hook_allowed:
                add("error", "R011", f"{eid} economic_hooks.affects 含非法项 '{aff}' ({where})")

        # R004 实体字段内的引用
        for field, target in C.entity_refs(ent):
            if C.is_external(target):
                continue
            if target not in entities:
                add("error", "R004", f"{eid}.{field} 引用不存在的实体 '{target}' ({where})")


def check_relations(entities: dict, relations: list[dict], meta: dict) -> None:
    rtypes = meta["relation_types"]
    req = set(meta["relation_required_fields"])
    opt = set(meta["relation_optional_fields"])
    allowed = req | opt | {"_file", "provenance"}
    prov_allowed = meta["provenance_schema"]["confidence"]["allowed"]

    for rel in relations:
        where = rel.get("_file", "?")
        for field in req - set(rel):
            add("error", "R005", f"关系缺少必填字段 '{field}': {rel} ({where})")
        for field in set(rel) - allowed:
            add("error", "R005", f"关系含未知字段 '{field}': {rel} ({where})")

        rtype = rel.get("type")
        a, b = rel.get("from"), rel.get("to")
        if rtype not in rtypes:
            add("error", "R005", f"关系类型 '{rtype}' 不在允许集合内：{a} -> {b} ({where})")
            continue

        # R004 两端可解析
        ends = []
        for side, eid in (("from", a), ("to", b)):
            if C.is_external(eid):
                ends.append(None)
                continue
            ent = entities.get(eid)
            if ent is None:
                add("error", "R004", f"关系 {rtype} 的 {side} 端 '{eid}' 不存在 ({where})")
            ends.append(ent)

        # R005 两端实体 type 满足约束
        spec = rtypes[rtype]
        for side, ent, key in (("from", ends[0], "from"), ("to", ends[1], "to")):
            if ent is None:
                continue
            if ent.get("type") not in spec.get(key, []):
                add("error", "R005",
                    f"{rtype}: {side} 端 {ent['id']} 类型 {ent.get('type')} 不满足约束 "
                    f"{spec.get(key)} ({where})")

        conf = rel.get("confidence")
        if conf is not None and conf not in prov_allowed:
            add("error", "R006", f"关系 confidence={conf!r} 非法：{a} -> {b} ({where})")

        # R017 边权只在有定义的关系类型上成立
        lik_schema = meta.get("likelihood_schema") or {}
        lik = rel.get("likelihood")
        if lik is not None:
            if lik not in (lik_schema.get("allowed") or []):
                add("error", "R017",
                    f"关系 likelihood={lik!r} 非法：{a} -> {b} ({where})")
            if rtype not in (lik_schema.get("applies_to") or []):
                add("error", "R017",
                    f"{rtype} 上不允许 likelihood（只对 "
                    f"{lik_schema.get('applies_to')} 有定义）：{a} -> {b} ({where})")


def check_derived(entities: dict, derived: list[dict], meta: dict) -> None:
    """只校验派生 belongs_to 的 to 端类型。

    build_index 把显式 relations 与 derived 一起入图，而 check_relations
    只吃显式关系，所以派生边需要单独看一眼。

    范围只限 belongs_to：它的语义是"归属于某道工序/阶段"，to 端必须是
    Stage 或 Process，这个约束对派生边同样成立。其他派生边（measured_by、
    performed_on 等）不套显式关系的白名单——派生规则用来补图的连通性，
    其端点类型本就不必满足显式关系的约束。
    """
    rtypes = meta["relation_types"]
    spec = rtypes.get("belongs_to") or {}
    allowed_to = spec.get("to", [])
    for rel in derived:
        if rel.get("type") != "belongs_to":
            continue
        eid = rel.get("to")
        if C.is_external(eid):
            continue
        ent = entities.get(eid)
        if ent is None:
            add("error", "R004", f"派生关系 belongs_to 的 to 端 '{eid}' 不存在")
            continue
        if ent.get("type") not in allowed_to:
            add("error", "R005",
                f"派生 belongs_to 的 to 端 {eid} 类型 {ent.get('type')} "
                f"不满足约束 {allowed_to}（from={rel.get('from')}）。"
                f"通常是给 Cause/Action 写了 equipment 字段——"
                f"设备关联应通过 Process 的 equipment 字段表达")


def check_graph(entities: dict, relations: list[dict], derived: list[dict],
                meta: dict, cfg: dict) -> None:
    # R008 孤立实体：既不参与任何关系，也未被任何实体字段引用
    touched: set[str] = set()
    for rel in relations + derived:
        for eid in (rel.get("from"), rel.get("to")):
            if isinstance(eid, str):
                touched.add(eid)
    for ent in entities.values():
        for _, target in C.entity_refs(ent):
            touched.add(target)
    for eid in entities:
        if eid not in touched:
            add("warn", "R008", f"{eid} 为孤立实体，未参与任何关系也未被引用 ({entities[eid]['_file']})")

    # R009 precedes 不成环
    graph: dict[str, list[str]] = defaultdict(list)
    for rel in relations:
        if rel.get("type") == "precedes":
            graph[rel["from"]].append(rel["to"])

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = defaultdict(int)

    def dfs(node: str, path: list[str]) -> None:
        color[node] = GRAY
        for nxt in graph.get(node, []):
            if color[nxt] == GRAY:
                cycle = " -> ".join(path[path.index(nxt):] + [nxt]) if nxt in path else f"{node} -> {nxt}"
                add("error", "R009", f"precedes 成环：{cycle}")
            elif color[nxt] == WHITE:
                dfs(nxt, path + [nxt])
        color[node] = BLACK

    for node in list(graph):
        if color[node] == WHITE:
            dfs(node, [node])

    # R010 Route.steps 相邻工序应存在 precedes 关系
    edge_set = {(rel["from"], rel["to"]) for rel in relations if rel.get("type") == "precedes"}
    for eid, ent in entities.items():
        if ent.get("type") != "Route":
            continue
        steps = ent.get("steps") or []
        for a, b in zip(steps, steps[1:]):
            if (a, b) not in edge_set:
                add("warn", "R010", f"{eid}: steps 中 {a} -> {b} 缺少对应 precedes 关系")

    # R012 低可信度占比预警
    threshold = (cfg.get("validate") or {}).get("low_confidence_ratio_warn", 0.4)
    if entities:
        low = sum(1 for e in entities.values()
                  if (e.get("provenance") or {}).get("confidence") == "low")
        ratio = low / len(entities)
        if ratio > threshold:
            add("warn", "R012",
                f"低可信度实体占比 {ratio:.0%}（{low}/{len(entities)}）超过阈值 {threshold:.0%}，"
                f"知识质量需复核")


def check_kb(entities: dict, cases: list[dict], meta: dict) -> None:
    kb = meta.get("kb_schema") or {}
    if not kb:
        return
    pattern = re.compile(kb["id_pattern"])
    required = set(kb["required_fields"])
    optional = set(kb.get("optional_fields") or [])
    allowed = required | optional | {"_file"}
    prov_schema = meta["provenance_schema"]
    seen: set[str] = set()

    for case in cases:
        cid = case.get("id", "<无 id>")
        where = case["_file"]

        if cid in seen:
            add("error", "R013", f"知识库实例 ID 重复：{cid} ({where})")
        seen.add(cid)
        if not pattern.match(str(cid)):
            add("error", "R013", f"{cid} 不符合 kb id 规范 {kb['id_pattern']} ({where})")

        for field in required - set(case):
            add("error", "R013", f"{cid} 缺少必填字段 '{field}' ({where})")
        for field in set(case) - allowed:
            add("error", "R013", f"{cid} 含未知字段 '{field}' ({where})")

        prov = case.get("provenance") or {}
        if prov.get("confidence") not in prov_schema["confidence"]["allowed"]:
            add("error", "R013", f"{cid} provenance.confidence 非法 ({where})")

        # R014 所有 *_ref 必须指向已存在的本体实体（知识库不得凭空引入概念）
        # R015 引用的域必须与实例自身的域一致，或为 core（共享概念）
        case_domain = case.get("domain")

        def check_ref(value, label: str, expect: str | tuple[str, ...] | None = None) -> None:
            if not isinstance(value, str) or C.is_external(value):
                return
            ent = entities.get(value)
            if ent is None:
                add("error", "R014", f"{cid}.{label} 引用不存在的本体实体 '{value}' ({where})")
                return
            if expect:
                allowed_types = (expect,) if isinstance(expect, str) else expect
                if ent.get("type") not in allowed_types:
                    add("error", "R014",
                        f"{cid}.{label} 期望 {'/'.join(allowed_types)} 类型，"
                        f"实际 {value} 为 {ent.get('type')} ({where})")
            ref_domain = value.split(".")[0]
            if case_domain and ref_domain not in (case_domain, "core"):
                add("error", "R015",
                    f"{cid}.{label} 跨域引用 '{value}'：{case_domain} 域的实例只能引用 "
                    f"{case_domain}.* 或 core.*（{where}）")

        check_ref(case.get("anomaly_ref"), "anomaly_ref", "Anomaly")
        check_ref(case.get("detected_at_ref"), "detected_at_ref", "Process")
        # cause_ref 允许的类型对齐 may_cause 关系的 from 端：
        # Anomaly 表示异常级联，State 表示状态阻塞导致的异常
        for i, pc in enumerate(case.get("possible_causes") or []):
            check_ref((pc or {}).get("cause_ref"), f"possible_causes[{i}].cause_ref",
                      ("Cause", "Parameter", "Process", "Anomaly", "State"))
        for i, ac in enumerate(case.get("actions") or []):
            check_ref((ac or {}).get("action_ref"), f"actions[{i}].action_ref", "Action")
        for i, m in enumerate(case.get("detection") or []):
            check_ref((m or {}).get("metric_ref"), f"detection[{i}].metric_ref", "Metric")
        impact = case.get("impact") or {}
        for i, p in enumerate(impact.get("blocked_processes") or []):
            check_ref(p, f"impact.blocked_processes[{i}]", "Process")


def check_competency(entities: dict, relations: list[dict], derived: list[dict],
                     meta: dict, cq: dict | None) -> None:
    """R016 能力问题清单：承诺能答的问题，其依赖结构必须真实存在且有实例。

    只查 requires 里声明的类型/关系是否有实例，不查语义。
    能挡的是"结构被删/被改名，而 CQ 还承诺着"这类静默失效。
    """
    if not cq:
        add("error", "R016", "缺少 ontology/competency-questions.yaml，库的边界未声明")
        return

    where = "ontology/competency-questions.yaml"
    declared_types = set(meta.get("entity_types") or {})
    declared_rels = set(meta.get("relation_types") or {})
    live_types = {e.get("type") for e in entities.values()}
    live_rels = {r.get("type") for r in relations} | {r.get("type") for r in derived}

    seen: set[str] = set()
    in_scope = cq.get("in_scope")
    if not in_scope:
        add("error", "R016", f"in_scope 为空：库必须至少声明一个承诺能答的问题 ({where})")
        in_scope = []

    for item in in_scope:
        cid = (item or {}).get("id", "<无 id>")
        if cid in seen:
            add("error", "R016", f"CQ ID 重复：{cid} ({where})")
        seen.add(cid)
        for field in ("q", "requires", "answered_via"):
            if not (item or {}).get(field):
                add("error", "R016", f"{cid} 缺少必填字段 '{field}' ({where})")

        req = (item or {}).get("requires") or {}
        if not req.get("entity_types") and not req.get("relations"):
            add("error", "R016",
                f"{cid}.requires 未声明任何 entity_types 或 relations，"
                f"该条目不可校验 ({where})")
        for et in req.get("entity_types") or []:
            if et not in declared_types:
                add("error", "R016", f"{cid} 依赖未声明的实体类型 '{et}' ({where})")
            elif et not in live_types:
                add("error", "R016",
                    f"{cid} 依赖实体类型 '{et}' 但该类型零实例，"
                    f"这个问题实际答不了 ({where})")
        for rt in req.get("relations") or []:
            if rt not in declared_rels:
                add("error", "R016", f"{cid} 依赖未声明的关系类型 '{rt}' ({where})")
            elif rt not in live_rels:
                add("error", "R016",
                    f"{cid} 依赖关系 '{rt}' 但该关系零实例，"
                    f"这个问题实际答不了 ({where})")

    for item in cq.get("out_of_scope") or []:
        oid = (item or {}).get("id", "<无 id>")
        if oid in seen:
            add("error", "R016", f"CQ ID 重复：{oid} ({where})")
        seen.add(oid)
        for field in ("q", "missing", "precondition"):
            if not (item or {}).get(field):
                add("error", "R016", f"{oid} 缺少必填字段 '{field}' ({where})")


def main() -> int:
    C.setup_console()
    quiet = "--quiet" in sys.argv

    meta = C.load_meta()
    cfg = C.load_config()
    entities, dup = C.load_entities()
    relations = C.load_relations()
    kb_cases = C.load_kb()
    derived = C.derived_relations(entities, meta)

    check_entities(entities, dup, meta)
    check_relations(entities, relations, meta)
    check_derived(entities, derived, meta)
    check_graph(entities, relations, derived, meta, cfg)
    check_kb(entities, kb_cases, meta)
    check_competency(entities, relations, derived, meta, C.load_competency())

    errors = [i for i in ISSUES if i[0] == "error"]
    warns = [i for i in ISSUES if i[0] == "warn"]

    print("=" * 68)
    print(f"实体 {len(entities)} | 显式关系 {len(relations)} | 派生关系 {len(derived)} | 知识库实例 {len(kb_cases)}")
    print("=" * 68)

    for sev, rule, msg in errors:
        print(f"[ERROR {rule}] {msg}")
    if not quiet:
        for sev, rule, msg in warns:
            print(f"[WARN  {rule}] {msg}")

    print("-" * 68)
    print(f"ERROR {len(errors)} | WARN {len(warns)}")
    if errors:
        print("校验未通过。")
        return 1
    print("校验通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
