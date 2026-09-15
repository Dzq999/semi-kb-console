"""同义/近重复 术语扫描器（类 + 属性）——B1 生成时去重 & C 轨定期修剪的共用检测器。

「同义重复类是本体最大垃圾」。本脚本只**检测并报告**，不改任何文件（修改交给
merge_synonyms.py）。三条同义判据（各带 reason，便于人工复核，宁可多报不误删）：

  1. uri_variant  ：规范化 localname 相同（大小写/下划线/连字符差异）——
                    如 RandomDefect / Random_Defect / random-defect。最高置信。
  2. same_label_en：规范化英文 rdfs:label 相同（大小写/空格/标点差异）。
  3. same_label_zh：规范化中文 rdfs:label@zh 相同。

按 kind（class / property）分组输出，canonical 选取规则：策展模块声明 > generated；
同层取 localname 最短（再字典序）。输出 build/reports/_synonym_scan.json，结构可直接
喂给 merge_synonyms.py（顶层 list of {kind, canonical, merge, reason, label_zh?}）。

用法：
  python scripts/find_synonyms.py                # 全扫，写报告
  python scripts/find_synonyms.py --kind property  # 只看属性（201 组那批）
  python scripts/find_synonyms.py --check        # 门禁模式：发现同义即退 1（B1/C 用）
"""
from __future__ import annotations
import json, re, sys, argparse
from collections import defaultdict
from pathlib import Path
from rdflib import Graph, RDF, RDFS, OWL, URIRef

ROOT = Path(__file__).resolve().parents[1]
MODULES = ROOT / "ontology" / "modules"
GENERATED = "generated.ttl"
SEMI = "urn:pxai:semi:"


def _norm_localname(iri: str) -> str:
    """URI 变体归一：去 semi: 前缀、小写、删下划线/连字符/空格。"""
    ln = iri[len(SEMI):] if iri.startswith(SEMI) else iri.rsplit(":", 1)[-1].rsplit("/", 1)[-1]
    return re.sub(r"[\s_\-]", "", ln).casefold()


def _norm_label(text: str) -> str:
    """标签归一：小写、非字母数字（含中日韩保留）折叠为空。"""
    return re.sub(r"[^\w]", "", str(text), flags=re.UNICODE).casefold()


def _local(iri: str) -> str:
    return iri[len(SEMI):] if iri.startswith(SEMI) else iri.rsplit(":", 1)[-1]


def scan() -> dict:
    # 解析每个模块，记录 IRI→声明模块、IRI→kind、IRI→(en_labels, zh_labels)
    declared: dict[str, str] = {}
    kind: dict[str, str] = {}
    # 属性 O/D 子型：object / datatype。跨子型撞名不可机械合并（对象属性取 IRI、数据属性取字面量），
    # 须单列人工定夺——对齐 backend B1b `_dedupe_synonym_additions` 的「跨 O/D 桶不自动并」策略。
    prop_type: dict[str, str] = {}
    labels_en: dict[str, set] = defaultdict(set)
    labels_zh: dict[str, set] = defaultdict(set)
    for ttl in sorted(MODULES.glob("*.ttl")):
        g = Graph(); g.parse(ttl, format="turtle")
        for s in g.subjects(RDF.type, OWL.Class):
            if isinstance(s, URIRef):
                declared.setdefault(str(s), ttl.name); kind[str(s)] = "class"
        for pt, tag in ((OWL.ObjectProperty, "object"), (OWL.DatatypeProperty, "datatype")):
            for s in g.subjects(RDF.type, pt):
                if isinstance(s, URIRef):
                    declared.setdefault(str(s), ttl.name); kind[str(s)] = "property"
                    prop_type[str(s)] = tag
        for s, _, o in g.triples((None, RDFS.label, None)):
            if not isinstance(s, URIRef):
                continue
            lang = getattr(o, "language", None)
            (labels_zh if lang == "zh" else labels_en)[str(s)].add(str(o))

    # 分 kind 建三种同义键 → IRI 集合
    def cluster(kind_want: str) -> list[dict]:
        by_uri: dict[str, set] = defaultdict(set)
        by_en: dict[str, set] = defaultdict(set)
        by_zh: dict[str, set] = defaultdict(set)
        for iri, k in kind.items():
            if k != kind_want:
                continue
            by_uri[_norm_localname(iri)].add(iri)
            for l in labels_en.get(iri, ()):
                nl = _norm_label(l)
                if nl:
                    by_en[nl].add(iri)
            for l in labels_zh.get(iri, ()):
                nl = _norm_label(l)
                if nl:
                    by_zh[nl].add(iri)

        # 合并三类判据到「连通分量」：任意两 IRI 只要在任一判据同组即视为同义连通
        parent: dict[str, str] = {}
        def find(x):
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]; x = parent[x]
            return x
        def union(a, b):
            parent[find(a)] = find(b)
        reasons: dict[frozenset, set] = defaultdict(set)
        for src, groups in (("uri_variant", by_uri), ("same_label_en", by_en), ("same_label_zh", by_zh)):
            for key, iris in groups.items():
                if len(iris) < 2:
                    continue
                il = sorted(iris)
                for x in il[1:]:
                    union(il[0], x)
                reasons[frozenset(il)].add(src)

        comps: dict[str, set] = defaultdict(set)
        for iri in list(parent):
            comps[find(iri)].add(iri)

        out: list[dict] = []
        od_conflicts: list[dict] = []
        for members in comps.values():
            if len(members) < 2:
                continue
            ml = sorted(members)
            merged_reasons = sorted({r for fs, rs in reasons.items() if fs & members for r in rs})
            # 属性簇跨 O/D 子型：绝不自动合并（对象属性取 IRI、数据属性取字面量，机械并会造成
            # 类型错位）。单列 od_conflicts 供人工定夺，不进 merge/prune 自动通道。
            if kind_want == "property":
                subtypes = {prop_type.get(i) for i in members}
                if "object" in subtypes and "datatype" in subtypes:
                    zh = sorted({l for i in members for l in labels_zh.get(i, ())})
                    od_conflicts.append({
                        "kind": "property",
                        "reason": merged_reasons,
                        "object_properties": sorted(_local(i) for i in ml if prop_type.get(i) == "object"),
                        "datatype_properties": sorted(_local(i) for i in ml if prop_type.get(i) == "datatype"),
                        **({"label_zh": zh[0]} if zh else {}),
                    })
                    continue
            # canonical：策展模块优先，其次 localname 最短，再字典序
            def rank(iri):
                curated = 0 if declared.get(iri, GENERATED) != GENERATED else 1
                return (curated, len(_local(iri)), iri)
            canon = min(ml, key=rank)
            entry = {
                "kind": kind_want,
                "canonical": _local(canon),
                "canonical_module": declared.get(canon, GENERATED),
                "merge": [_local(i) for i in ml if i != canon],
                "reason": merged_reasons,
            }
            zh = sorted({l for i in members for l in labels_zh.get(i, ())})
            if zh:
                entry["label_zh"] = zh[0]
            out.append(entry)
        out.sort(key=lambda e: (-len(e["merge"]), e["canonical"]))
        return out, od_conflicts

    classes, _ = cluster("class")  # 类无 O/D 子型，第二返回值恒空
    props, od_conflicts = cluster("property")
    return {"classes": classes, "properties": props, "od_conflicts": od_conflicts}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=["class", "property"], default=None)
    ap.add_argument("--check", action="store_true", help="发现同义即退 1（门禁）")
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):
        try: stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError): pass

    result = scan()
    n_cls = sum(len(c["merge"]) for c in result["classes"])
    n_prop = sum(len(c["merge"]) for c in result["properties"])
    od = result.get("od_conflicts", [])
    out = ROOT / "build" / "reports" / "_synonym_scan.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"同义扫描完成 → {out.relative_to(ROOT)}")
    print(f"  类同义组 {len(result['classes'])} 组（涉及冗余类 {n_cls} 个）")
    print(f"  属性同义组 {len(result['properties'])} 组（涉及冗余属性 {n_prop} 个）")
    if od:
        print(f"  ⚠ 跨 O/D 撞名 {len(od)} 组（不自动合并，须人工定夺）")
    show = result["classes"] if args.kind == "class" else result["properties"] if args.kind == "property" else result["classes"][:5] + result["properties"][:5]
    for c in show[:12]:
        print(f"  [{c['kind']}] {c['canonical']}  ← {', '.join(c['merge'][:4])}"
              f"{'...' if len(c['merge'])>4 else ''}  ({'/'.join(c['reason'])})")
    for c in od:
        print(f"  [O/D] {c.get('label_zh','')}  对象={c['object_properties']} | 数据={c['datatype_properties']}")

    if args.check:
        # 门禁只对「可自动合并」的同义（类 + O/D 纯属性）失败；跨 O/D 撞名单列告警、不阻断
        # （它需要人工建模决策，不能靠机械合并解决，硬拒会误伤合法的同名对象/数据属性）。
        total = len(result["classes"]) + len(result["properties"])
        if total:
            print(f"\n❌ 门禁：发现 {total} 组可合并同义术语，须先合并（见报告）")
            return 1
        if od:
            print(f"\n⚠ 门禁通过（有 {len(od)} 组跨 O/D 撞名待人工定夺，不阻断发布）")
        else:
            print("\n✅ 门禁：无同义术语")
    return 0


if __name__ == "__main__":
    sys.exit(main())
