"""同义类合并执行器（A1 存量治理 / C 常态修剪复用）。

读入合并映射 JSON（默认 build/reports/_synonym_merge_map.json），对三处落库文件执行：
- generated.ttl：删除每个待删类『自身声明三元组』（a owl:Class / label / subClassOf），
  再把所有『指向待删类的入边』(X subClassOf D) 改指规范类 C。不改写待删类作主语的边——
  否则 C 会继承 D 的 label/父类造成污染。可选修正规范类中文 label。
- current.ttl / provenance.ttl：把待删类作主语或宾语的引用整体改指 C（实例 rdf:type、
  关系目标、溯源记账）。这两处不含类声明，改写不会污染 label。

只改 IRI 引用，不新增语义。写前全量备份，写后每个文件 rdflib 复解析自检；任一步失败
立即回滚并非零退出。产出 build/reports/synonym-merge-<ts>.json 审计日志。

用法：
    python scripts/merge_synonyms.py            # 执行合并
    python scripts/merge_synonyms.py --dry-run  # 只报将改动，不落盘
退出码：0 成功；1 失败（已回滚）；2 用法/文件错误。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from rdflib import Graph, RDF, RDFS, OWL, URIRef, Literal
except ModuleNotFoundError as exc:
    print(f"缺少依赖 {exc.name}；请先安装 requirements.txt。", file=sys.stderr)
    raise SystemExit(2) from exc

ROOT = Path(__file__).resolve().parent.parent
SEMI = "urn:pxai:semi:"
SCHEMA = ROOT / "ontology" / "modules" / "generated.ttl"
DATA = ROOT / "knowledge" / "semantic" / "current.ttl"
PROV = ROOT / "knowledge" / "semantic" / "provenance.ttl"
DEFAULT_MAP = ROOT / "build" / "reports" / "_synonym_merge_map.json"


def iri(local: str) -> URIRef:
    return URIRef(SEMI + local)


def load_map(path: Path) -> list[dict]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, list):
        raise ValueError("合并映射必须是簇列表")
    for c in doc:
        if not c.get("canonical") or not c.get("merge"):
            raise ValueError(f"簇缺 canonical/merge：{c}")
        c.setdefault("kind", "class")  # 向后兼容：无 kind 默认类
        if c["kind"] not in ("class", "property"):
            raise ValueError(f"kind 非法（须 class/property）：{c}")
    return doc


def rewrite_schema(clusters: list[dict]) -> tuple[Graph, int, int]:
    g = Graph()
    g.parse(SCHEMA, format="turtle")
    removed = redirected = 0
    for c in clusters:
        canon = iri(c["canonical"])
        for d_local in c["merge"]:
            d = iri(d_local)
            # 删除待删项自身声明的所有出边（类：a owl:Class/label/subClassOf；属性：类型/label/domain/range/subPropertyOf/inverseOf）
            for t in list(g.triples((d, None, None))):
                g.remove(t)
                removed += 1
            # 入边（X ? D）改指 C（类：X subClassOf D；属性：X subPropertyOf/inverseOf D、restriction onProperty D）
            for s, p, _ in list(g.triples((None, None, d))):
                g.remove((s, p, d))
                g.add((s, p, canon))
                redirected += 1
            # 谓词位（S D O）改指 C——属性作谓词才有；类恒无、为空操作，故对两类通用安全
            for s, _, o in list(g.triples((None, d, None))):
                g.remove((s, d, o))
                g.add((s, canon, o))
                redirected += 1
        # 属性发散簇：删 canonical 的 domain/range，避免 OWL RL 过度推理触发不相交回滚
        if c["kind"] == "property" and c.get("drop_domain_range"):
            for pred in (RDFS.domain, RDFS.range):
                for t in list(g.triples((canon, pred, None))):
                    g.remove(t); removed += 1
        # 可选：修正规范项中文 label
        if c.get("label_zh"):
            for t in list(g.triples((canon, RDFS.label, None))):
                if isinstance(t[2], Literal) and (t[2].language == "zh" or t[2].language is None):
                    g.remove(t)
            g.add((canon, RDFS.label, Literal(c["label_zh"], lang="zh")))
    return g, removed, redirected


def rewrite_data(path: Path, clusters: list[dict]) -> tuple[Graph, int]:
    g = Graph()
    g.parse(path, format="turtle")
    changed = 0
    repl = {iri(d): iri(c["canonical"]) for c in clusters for d in c["merge"]}
    for old, new in repl.items():
        # 主语位（类实例 rdf:type old / old 作主语）
        for s, p, o in list(g.triples((old, None, None))):
            g.remove((s, p, o)); g.add((new, p, o)); changed += 1
        # 宾语位（关系目标、rdf:type 指向 old、溯源记账）
        for s, p, o in list(g.triples((None, None, old))):
            g.remove((s, p, o)); g.add((s, p, new)); changed += 1
        # 谓词位（实例三元组 S old V）——属性同义的主战场，A1 类合并时此项恒空
        for s, p, o in list(g.triples((None, old, None))):
            g.remove((s, old, o)); g.add((s, new, o)); changed += 1
    return g, changed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", default=str(DEFAULT_MAP))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    map_path = Path(args.map)
    if not map_path.is_file():
        print(f"找不到合并映射：{map_path}", file=sys.stderr)
        return 2
    clusters = load_map(map_path)
    total_del = sum(len(c["merge"]) for c in clusters)
    print(f"合并簇 {len(clusters)} 个，待删类 {total_del} 个")

    backups = {p: p.read_bytes() for p in (SCHEMA, DATA, PROV) if p.is_file()}
    try:
        sg, removed, redirected = rewrite_schema(clusters)
        dg, dchanged = rewrite_data(DATA, clusters)
        pg, pchanged = rewrite_data(PROV, clusters) if PROV.is_file() else (None, 0)
        print(f"generated.ttl：删声明 {removed} 边，重指入边 {redirected} 边")
        print(f"current.ttl：改写 {dchanged} 边；provenance.ttl：改写 {pchanged} 边")
        if args.dry_run:
            print("[dry-run] 未落盘")
            return 0
        for g, p in ((sg, SCHEMA), (dg, DATA)) + (((pg, PROV),) if pg is not None else ()):
            tmp = p.with_suffix(p.suffix + ".tmp")
            g.serialize(tmp, format="turtle", encoding="utf-8")
            Graph().parse(tmp, format="turtle")  # 复解析自检
            tmp.replace(p)
    except Exception as exc:  # noqa: BLE001
        for p, b in backups.items():
            p.write_bytes(b)
        print(f"合并失败，已回滚：{exc}", file=sys.stderr)
        return 1

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log = ROOT / "build" / "reports" / f"synonym-merge-{ts}.json"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "clusters": len(clusters), "terms_deleted": total_del,
        "kinds": sorted({c["kind"] for c in clusters}),
        "schema_decl_triples_removed": removed, "schema_inbound_redirected": redirected,
        "data_triples_rewritten": dchanged, "prov_triples_rewritten": pchanged,
        "map": clusters,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"合并完成。审计日志：{log.relative_to(ROOT)}")
    print("下一步请跑发布门禁：python scripts/kb.py check --no-precheck --defer-derived")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
