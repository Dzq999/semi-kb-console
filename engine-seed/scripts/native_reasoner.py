"""最小 semi-naive OWL RL 物化器：替换 owlrl 全图闭包中随库超线性的那部分。

只覆盖本体**实际用到**的构造（真实基线实测：见 docs/incremental-reasoning-spike.md
文末与 semi-kb-gate-per-pass-cost 记忆）：

  - RDFS：rdfs:subClassOf（传递闭包 + rdf:type 沿子类上溯）、rdfs:subPropertyOf
    （传递闭包）、rdfs:domain、rdfs:range（仅 URI 宾语）；
  - OWL RL 属性公理：owl:inverseOf（双向）、owl:TransitiveProperty、
    owl:propertyChainAxiom（2 元）。

**刻意不覆盖**（本体里各 ≤1 次、且经静态核查无任何 SHACL shape / SPARQL 规则依赖其
entailment；见 `equivalence_precondition`）：owl:sameAs（FunctionalProperty / 基数 1 →
相等）、equivalentClass/Property、disjointWith/AllDisjointClasses、Restriction/
someValuesFrom/allValuesFrom/hasValue、unionOf/intersectionOf/oneOf。native 闭包因此是
owlrl 闭包的**严格子集**（真实基线 only_in_native=0）：只少产、不多产，故绝不会造出
owlrl 没有的三元组去触发假 FAIL；唯一风险是「少产了某条 verdict 相关三元组」，由
`equivalence_precondition`（runtime 依赖护栏，非空即回退 owlrl）+ 真实基线对拍认证
（tests/semantic/test_native_equivalence_realbaseline.py）双重兜住。

治理红线：本模块只改「如何物化 entailment」，不改发布 PASS/FAIL 判定，也不改「未证明
候选一律隔离、永不发布」的隔离边界。默认走 native（semantic_validate.py 的
SEMANTIC_REASONER 门控，2026-09-06 Cert A 真实基线对拍通过后切为默认）；依赖护栏遇越界
构造或显式 SEMANTIC_REASONER=owlrl 时回退 owlrl。
"""
from __future__ import annotations

from typing import Iterable

from rdflib import Graph, RDF, RDFS, OWL, URIRef

# --------------------------------------------------------------------------- #
# 依赖护栏用到的「native 不覆盖」词表。命中即表示当前 shape/规则/本体依赖了 native
# 未物化的 entailment，等价性无法保证 —— 分派侧据此回退 owlrl（绝不静默弱化门禁）。
# --------------------------------------------------------------------------- #
# 规则 WHERE 里出现这些 OWL 词 = 规则在读 native 跳过的 entailment。
_SKIPPED_OWL_LOCAL = (
    "sameAs", "equivalentClass", "equivalentProperty", "disjointWith",
    "AllDisjointClasses", "AllDifferent", "differentFrom", "Restriction",
    "onProperty", "someValuesFrom", "allValuesFrom", "hasValue",
    "unionOf", "intersectionOf", "complementOf", "oneOf", "disjointUnionOf",
    "Nothing", "FunctionalProperty", "InverseFunctionalProperty",
)
# SHACL 里这些约束的语义可能依赖跳过的 entailment，或内嵌任意 SPARQL：一律保守拦。
_RISKY_SHACL_LOCAL = (
    "not", "sparql", "hasValue", "qualifiedValueShape",
    "qualifiedMinCount", "qualifiedMaxCount", "disjoint", "closed", "xone",
)
_SH = "http://www.w3.org/ns/shacl#"
_OWL_NS = str(OWL)


class TBox:
    """从三元组集里抽取本 profile 的静态公理（本 profile 无规则再生成这些公理，故一次抽取即可）。"""

    def __init__(self, triples: Iterable[tuple]) -> None:
        self.subclass: dict = {}       # sub -> {super}（直接；传递闭包在物化循环里补全）
        self.subprop: dict = {}        # p -> {q}（直接 subPropertyOf）
        self.dom: dict = {}            # p -> {class}
        self.rng: dict = {}            # p -> {class}
        self.inv: dict = {}            # p -> {q}（双向）
        self.trans: set = set()        # transitive properties
        self.chains: list = []         # 2 元 (prop, p1, p2)
        self.oversized_chains: list = []  # 长度 != 2 的链：native 不覆盖，护栏据此拦
        first, rest, nil = RDF.first, RDF.rest, RDF.nil
        list_first: dict = {}
        list_rest: dict = {}
        chain_head: dict = {}          # prop -> 列表头节点
        for s, p, o in triples:
            if p == RDFS.subClassOf and isinstance(o, URIRef):
                self.subclass.setdefault(s, set()).add(o)
            elif p == RDFS.subPropertyOf:
                self.subprop.setdefault(s, set()).add(o)
            elif p == RDFS.domain:
                self.dom.setdefault(s, set()).add(o)
            elif p == RDFS.range:
                self.rng.setdefault(s, set()).add(o)
            elif p == OWL.inverseOf:
                self.inv.setdefault(s, set()).add(o)
                self.inv.setdefault(o, set()).add(s)
            elif p == RDF.type and o == OWL.TransitiveProperty:
                self.trans.add(s)
            elif p == OWL.propertyChainAxiom:
                chain_head[s] = o
            elif p == first:
                list_first[s] = o
            elif p == rest:
                list_rest[s] = o
        # subPropertyOf 传递闭包（真超属性集合）
        self.subprop_star: dict = {}
        for p in list(self.subprop):
            seen, stack = set(), [p]
            while stack:
                x = stack.pop()
                for q in self.subprop.get(x, ()):
                    if q not in seen:
                        seen.add(q)
                        stack.append(q)
            self.subprop_star[p] = seen
        # 解析 property chain 列表；只接受 2 元，其余记入 oversized_chains 供护栏拦截
        for prop, head in chain_head.items():
            items, node = [], head
            while node is not None and node != nil:
                if node in list_first:
                    items.append(list_first[node])
                node = list_rest.get(node)
            if len(items) == 2:
                self.chains.append((prop, items[0], items[1]))
            else:
                self.oversized_chains.append((prop, tuple(items)))


def extract_tbox(triples: Iterable[tuple]) -> TBox:
    return TBox(triples)


def materialize_triples(base: set, *, tbox: TBox | None = None,
                        seed: set | None = None, start: set | None = None) -> set:
    """半朴素前向物化到不动点，返回闭包三元组集（不修改入参）。

    - 全量：materialize_triples(G) —— tbox 由 G 抽取，start=seed=G。
    - 增量（合流性测试用）：materialize_triples(base, tbox=tb, seed=Δ, start=prior∪Δ)
      —— 从已物化的 prior 出发、只以 Δ 为初始 delta 驱动。datalog 半朴素合流 ⇒ 与
      全量结果逐三元组相等（这正是 owlrl 复用式增量做不到、导致上阶段流产的性质）。
    """
    tb = tbox if tbox is not None else TBox(base)
    all_t: set = set(start if start is not None else base)

    out_idx: dict = {}   # p -> {s -> {o}}
    in_idx: dict = {}    # p -> {o -> {s}}
    types: dict = {}     # class -> {inst}
    sco_sup: dict = {}   # sub -> {super}（含派生）
    sco_sub: dict = {}   # super -> {sub}

    def index(t: tuple) -> None:
        s, p, o = t
        out_idx.setdefault(p, {}).setdefault(s, set()).add(o)
        in_idx.setdefault(p, {}).setdefault(o, set()).add(s)
        if p == RDF.type:
            types.setdefault(o, set()).add(s)
        elif p == RDFS.subClassOf:
            sco_sup.setdefault(s, set()).add(o)
            sco_sub.setdefault(o, set()).add(s)

    for t in all_t:
        index(t)

    delta: set = set(seed if seed is not None else base)
    trans, inv, dom, rng, subprop_star = tb.trans, tb.inv, tb.dom, tb.rng, tb.subprop_star
    chains_by_first: dict = {}
    chains_by_second: dict = {}
    for prop, p1, p2 in tb.chains:
        chains_by_first.setdefault(p1, []).append((prop, p2))
        chains_by_second.setdefault(p2, []).append((prop, p1))

    while delta:
        new: set = set()

        def emit(t: tuple) -> None:
            if t not in all_t:
                new.add(t)

        for (s, p, o) in delta:
            # subPropertyOf: (x p y) -> (x q y)
            if p in subprop_star:
                for q in subprop_star[p]:
                    emit((s, q, o))
            # domain: (x p y) -> (x a C)
            if p in dom:
                for c in dom[p]:
                    emit((s, RDF.type, c))
            # range: (x p y) -> (y a C)（仅 URI 宾语）
            if p in rng and isinstance(o, URIRef):
                for c in rng[p]:
                    emit((o, RDF.type, c))
            # inverseOf: (x p y) -> (y q x)
            if p in inv and isinstance(o, URIRef):
                for q in inv[p]:
                    emit((o, q, s))
            # TransitiveProperty：左右两侧扩展
            if p in trans and isinstance(o, URIRef):
                for z in out_idx.get(p, {}).get(o, ()):     # (s p o)+(o p z)
                    emit((s, p, z))
                for w in in_idx.get(p, {}).get(s, ()):      # (w p s)+(s p o)
                    emit((w, p, o))
            # propertyChainAxiom（2 元）
            if p in chains_by_first and isinstance(o, URIRef):
                for prop, p2 in chains_by_first[p]:         # (s p1 o)+(o p2 z)->(s prop z)
                    for z in out_idx.get(p2, {}).get(o, ()):
                        emit((s, prop, z))
            if p in chains_by_second and isinstance(o, URIRef):
                for prop, p1 in chains_by_second[p]:        # (x p1 s)+(s p2 o)->(x prop o)
                    for x in in_idx.get(p1, {}).get(s, ()):
                        emit((x, prop, o))
            # rdf:type + subClassOf 双向传播
            if p == RDF.type:
                for sup in sco_sup.get(o, ()):              # (s type o)+(o sco sup)
                    emit((s, RDF.type, sup))
            elif p == RDFS.subClassOf:
                for inst in types.get(s, ()):               # (inst type s)+(s sco o)
                    emit((inst, RDF.type, o))
                for sup in sco_sup.get(o, ()):              # (s sco o)+(o sco sup) 传递
                    emit((s, RDFS.subClassOf, sup))
                for sub in sco_sub.get(s, ()):              # (sub sco s)+(s sco o) 传递
                    emit((sub, RDFS.subClassOf, o))

        new -= all_t
        for t in new:
            index(t)
        all_t |= new
        delta = new
    return all_t


def materialize(data: Graph) -> int:
    """就地把本 profile 的 entailment 物化进 rdflib Graph；返回新增三元组数。

    语义对齐 owlrl 的 `DeductiveClosure(OWLRL_Semantics).expand(data)`：调用后 `data`
    含闭包。假定 schema（TBox）已并入 `data`（semantic_validate.py 在闭包前 `data +=
    schema`）。
    """
    base = set(data.triples((None, None, None)))
    closed = materialize_triples(base)
    added = 0
    for t in closed - base:
        data.add(t)
        added += 1
    return added


def equivalence_precondition(shapes: Graph, rule_texts: Iterable[str],
                             schema: Graph | None = None) -> list[str]:
    """runtime 依赖护栏：返回会破坏「native 与 owlrl 裁决等价」假设的依赖清单。

    非空即表示当前配置下 native 可能少产某条 verdict 相关三元组 —— 分派侧据此回退
    owlrl（门禁绝不弱化）。当前本体经静态核查返回空（core-shapes.ttl 只用
    targetClass/property/path/min|maxCount/class/datatype/or；271 条规则 WHERE 无一
    引用跳过构造）。日后有人给 shape 加 sh:not/sh:sparql，或给规则加 owl:sameAs/
    equivalentClass/disjointWith/Restriction 等，或给本体加 >2 元 propertyChainAxiom，
    此函数立即报出 blocker、native 自动让位 owlrl。
    """
    blockers: list[str] = []

    # 规则 WHERE 引用跳过的 OWL entailment
    for name in _SKIPPED_OWL_LOCAL:
        prefixed = f"owl:{name}"
        full = f"owl#{name}"  # http://www.w3.org/2002/07/owl#<name> 的子串
        for text in rule_texts:
            if prefixed in text or full in text:
                blockers.append(f"规则引用了 native 不覆盖的构造 owl:{name}")
                break

    # SHACL 高级约束：语义可能依赖跳过的 entailment，或内嵌任意 SPARQL
    for name in _RISKY_SHACL_LOCAL:
        pred = URIRef(_SH + name)
        if any(True for _ in shapes.triples((None, pred, None))):
            blockers.append(f"SHACL 使用了 native 无法保证等价的约束 sh:{name}")

    # 本体含 >2 元 propertyChainAxiom（native 只物化 2 元）
    if schema is not None:
        tb = TBox(schema.triples((None, None, None)))
        for prop, items in tb.oversized_chains:
            blockers.append(
                f"本体含 native 不覆盖的 {len(items)} 元 owl:propertyChainAxiom（{prop}）")

    return blockers
