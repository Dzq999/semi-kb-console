"""从本体与知识库生成派生检索产物。

build/ 下所有文件都是派生物：任何时候可删除重建，禁止手工编辑。
这是"本体为唯一真源"的落地方式——索引漂移时重建即可，不需要人工对账。

用法：
    python scripts/build_index.py
产出：
    build/index.json  扁平检索表（供 kb-query 关键词/ID 检索）
    build/graph.json  邻接表（供主流程走查与风险图谱遍历）
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone

import common as C


def keywords_of(ent: dict) -> list[str]:
    """检索关键词：名称、别名、标签，加中文描述首句的名词性片段。"""
    words: list[str] = []
    for field in ("name_zh", "name_en"):
        if ent.get(field):
            words.append(str(ent[field]))
    for field in ("aliases", "tags"):
        for v in ent.get(field) or []:
            words.append(str(v))
    # 英文名按词拆分，便于 CMP / Wire Bond 这类术语的分词命中
    for w in re.split(r"[\s/&()\-]+", str(ent.get("name_en") or "")):
        if len(w) > 1:
            words.append(w)
    seen, out = set(), []
    for w in words:
        k = w.lower()
        if k not in seen:
            seen.add(k)
            out.append(w)
    return out


def vfab_catalog_records() -> list[dict]:
    """把 vFab 数据集摊成检索记录（数据集级，非行级）。

    行级（8092 行 SECS 消息）灌进索引会淹没检索信号——单条消息不是有意义的
    检索单元。数据集级（93 条）才是对的粒度：检索"Aleris 的 Carrier ID Error"
    命中数据集，用户再下钻看原始 CSV。这落实"vFab 只进检索层不进推理层"的分层
    决策：这些记录只供关键词检索，不进 current.ttl、不参与 OWL/SHACL。

    从 CSV 首行数据读真实 equipment/phase（保留原始大小写），并数出行数。
    catalog 缺失或 status!=available 时返回空——vFab 未交付是显式状态，不是错误。
    """
    import csv

    catalog_path = C.ROOT / "build" / "source" / "vfab-catalog.json"
    if not catalog_path.is_file():
        return []
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if catalog.get("status") != "available":
        return []

    source_root = C.ROOT / "sources" / "internal" / "vfab"
    records: list[dict] = []
    for ds in catalog.get("datasets") or []:
        csv_path = (source_root / ds["path"]).resolve()
        equipment = phase = None
        row_count = 0
        if csv_path.is_file():
            with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
                reader = csv.DictReader(fh)
                for i, row in enumerate(reader):
                    row_count += 1
                    if i == 0:
                        equipment = (row.get("equipment") or "").strip() or None
                        phase = (row.get("phase") or "").strip() or None
        stem = csv_path.stem  # e.g. aleris__abnormal01_carrier_id_error
        target = str(ds.get("target_class") or "").replace("urn:pxai:semi:", "")
        keywords = [w for w in [equipment, phase, target, stem.replace("__", " ").replace("_", " ")] if w]
        records.append({
            "id": f"vfab.dataset.{stem}",
            "kind": "vfab_dataset",
            "type": target or "Dataset",
            "domain": None,
            "name_zh": None,
            "name_en": f"{equipment or stem} · {phase or ''}".strip(" ·"),
            "keywords": keywords,
            "equipment": equipment,
            "phase": phase,
            "target_class": ds.get("target_class"),
            "entity_keys": ds.get("entity_keys") or [],
            "field_count": ds.get("field_count") or len(ds.get("headers") or []),
            "row_count": row_count,
            "confidence": "high",
            "source_type": "vfab",
            "file": f"sources/internal/vfab/{ds['path']}",
        })
    return records


def build_index(entities: dict, kb_cases: list[dict], meta: dict,
                vfab_records: list[dict] | None = None) -> dict:
    records = []
    vfab_records = vfab_records or []
    for eid, ent in entities.items():
        prov = ent.get("provenance") or {}
        records.append({
            "id": eid,
            "kind": "entity",
            "type": ent.get("type"),
            "domain": ent.get("domain"),
            "name_zh": ent.get("name_zh"),
            "name_en": ent.get("name_en"),
            "keywords": keywords_of(ent),
            "stage": ent.get("stage"),
            "severity": ent.get("severity"),
            "unit": ent.get("unit"),
            "affects": (ent.get("economic_hooks") or {}).get("affects") or [],
            "confidence": prov.get("confidence"),
            "source_type": prov.get("source_type"),
            "file": ent["_file"],
        })

    for case in kb_cases:
        prov = case.get("provenance") or {}
        records.append({
            "id": case.get("id"),
            "kind": "kb_case",
            "type": "Playbook",
            "domain": case.get("domain"),
            "name_zh": case.get("title"),
            "name_en": None,
            "keywords": [str(case.get("title") or "")] + [str(t) for t in case.get("tags") or []],
            "anomaly_ref": case.get("anomaly_ref"),
            "detected_at_ref": case.get("detected_at_ref"),
            "severity": case.get("severity"),
            "cause_refs": [c.get("cause_ref") for c in case.get("possible_causes") or []],
            "action_refs": [a.get("action_ref") for a in case.get("actions") or []],
            "confidence": prov.get("confidence"),
            "source_type": prov.get("source_type"),
            "file": case["_file"],
        })

    records.extend(vfab_records)

    by_type: dict[str, int] = defaultdict(int)
    by_domain: dict[str, int] = defaultdict(int)
    by_conf: dict[str, int] = defaultdict(int)
    for r in records:
        by_type[str(r["type"])] += 1
        by_domain[str(r["domain"])] += 1
        by_conf[str(r["confidence"])] += 1

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "meta_version": meta.get("meta_version"),
        "warning": "派生产物，请勿手工编辑；修改本体后重新运行 build_index.py",
        "stats": {
            "total": len(records),
            "entities": len(entities),
            "kb_cases": len(kb_cases),
            "vfab_datasets": len(vfab_records),
            "by_type": dict(sorted(by_type.items())),
            "by_domain": dict(sorted(by_domain.items())),
            "by_confidence": dict(sorted(by_conf.items())),
        },
        "records": records,
    }


def build_graph(entities: dict, relations: list[dict], derived: list[dict], meta: dict) -> dict:
    out_adj: dict[str, list[dict]] = defaultdict(list)
    in_adj: dict[str, list[dict]] = defaultdict(list)
    edges = []

    for rel in relations + derived:
        a, b, t = rel.get("from"), rel.get("to"), rel.get("type")
        edge = {
            "from": a, "to": b, "type": t,
            "note": rel.get("note"),
            "derived": bool(rel.get("_derived")),
        }
        # 边权进邻接表，不只进 edges 列表：按概率排根因走的是 in_adj[异常]，
        # 只写在 edges 里的话下游得先全表扫一遍才能排序。
        lik = rel.get("likelihood")
        if lik is not None:
            edge["likelihood"] = lik
        edges.append(edge)
        out_hop = {"type": t, "to": b, "derived": edge["derived"]}
        in_hop = {"type": t, "from": a, "derived": edge["derived"]}
        if lik is not None:
            out_hop["likelihood"] = in_hop["likelihood"] = lik
        out_adj[a].append(out_hop)
        in_adj[b].append(in_hop)

    # 主流程链：Route.steps 即业务视角的有序路径，附上每步的异常挂载数便于风险定位
    routes = {}
    anomaly_at: dict[str, list[str]] = defaultdict(list)
    for rel in relations:
        if rel.get("type") == "detected_at":
            anomaly_at[rel["to"]].append(rel["from"])

    for eid, ent in entities.items():
        if ent.get("type") != "Route":
            continue
        steps = ent.get("steps") or []
        routes[eid] = {
            "name_zh": ent.get("name_zh"),
            "domain": ent.get("domain"),
            "length": len(steps),
            "steps": [
                {
                    "seq": i + 1,
                    "id": sid,
                    "name_zh": (entities.get(sid) or {}).get("name_zh"),
                    "stage": (entities.get(sid) or {}).get("stage"),
                    "anomalies_detected_here": anomaly_at.get(sid, []),
                }
                for i, sid in enumerate(steps)
            ],
        }

    by_type: dict[str, int] = defaultdict(int)
    for e in edges:
        by_type[str(e["type"])] += 1

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "warning": "派生产物，请勿手工编辑；修改本体后重新运行 build_index.py",
        "stats": {
            "nodes": len(set(list(out_adj) + list(in_adj))),
            "edges": len(edges),
            "explicit": len(relations),
            "derived": len(derived),
            "by_relation_type": dict(sorted(by_type.items())),
        },
        "routes": routes,
        "out_adjacency": {k: v for k, v in sorted(out_adj.items())},
        "in_adjacency": {k: v for k, v in sorted(in_adj.items())},
        "edges": edges,
    }


def main() -> int:
    C.setup_console()
    meta = C.load_meta()
    entities, dup = C.load_entities()
    if dup:
        print("存在重复 ID，请先运行 validate.py 修正：")
        for d in dup:
            print(f"  - {d}")
        return 1

    relations = C.load_relations()
    kb_cases = C.load_kb()
    derived = C.derived_relations(entities, meta)

    build_dir = C.ROOT / "build"
    build_dir.mkdir(exist_ok=True)

    vfab_records = vfab_catalog_records()
    index = build_index(entities, kb_cases, meta, vfab_records)
    graph = build_graph(entities, relations, derived, meta)

    for name, payload in (("index.json", index), ("graph.json", graph)):
        path = build_dir / name
        with path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print(f"已生成 build/{name}  ({path.stat().st_size / 1024:.1f} KB)")

    s, g = index["stats"], graph["stats"]
    print(f"  检索记录 {s['total']}（实体 {s['entities']} + 知识库实例 {s['kb_cases']}"
          f" + vFab 数据集 {s.get('vfab_datasets', 0)}）")
    print(f"  图节点 {g['nodes']} | 边 {g['edges']}（显式 {g['explicit']} + 派生 {g['derived']}）")
    print(f"  主流程 {len(graph['routes'])} 条：" +
          "，".join(f"{v['name_zh']}({v['length']}步)" for v in graph["routes"].values()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
