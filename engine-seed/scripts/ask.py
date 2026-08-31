"""判断一个问题库能不能答，以及答得了的话沿哪条路径答。

    python scripts/ask.py "颗粒突增可能是什么原因"
    python scripts/ask.py "金属层1的层叠结构" --json
    python scripts/ask.py "扩散炉停机会引发什么异常" --probe

三层判定，逐层收紧：

  1. 匹配   问句 -> CQ 条目。意图词（match_terms，手写）为主，
            实体名（从本体自动派生）为辅并顺带定出探针锚点。
  2. 声明   命中 in_scope 还是 out_of_scope。这一层只看清单怎么说。
  3. 探测   --probe 时真的沿 probe 定义的边走一遍，看空不空。
            这层挡的是"承诺了但实际走不通"：R016 只查依赖类型有没有实例，
            查不了具体对象上的路径是否真通。

退出码：0 可答 | 2 明确超出范围 | 3 清单里没有这个问题
定时任务可以据此判断要不要把问题转人工。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

W_INTENT = 10.0   # 意图词权重：决定问的是哪类问题
W_ENTITY = 3.0    # 实体名权重：决定问的是哪个对象，不足以单独定类
MIN_SCORE = 6.0   # 低于此分视为没匹配上，宁可报"不在清单里"也不硬套


def norm(text: str) -> str:
    return re.sub(r"[\s，。、？?！!：:；;（）()【】\[\]\"'`~,.]+", "", str(text).lower())


def terms_of(text: str) -> set[str]:
    """中文取字符 bigram，英文数字取词。不引 jieba，避免为一个脚本加依赖。"""
    s = norm(text)
    out = {m.group() for m in re.finditer(r"[a-z0-9]+", s)}
    han = re.sub(r"[a-z0-9]+", "", s)
    out |= {han[i:i + 2] for i in range(len(han) - 1)}
    return out


def entity_terms(entities: dict) -> dict[str, list[tuple[str, str]]]:
    """type -> [(归一化名, 实体ID)]。从本体派生，不手工维护，改本体自动跟上。"""
    out: dict[str, list[tuple[str, str]]] = {}
    for eid, ent in entities.items():
        names = [ent.get("name_zh"), ent.get("name_en"), *(ent.get("aliases") or [])]
        for nm in names:
            if nm and len(norm(nm)) >= 2:
                out.setdefault(ent["type"], []).append((norm(nm), eid))
    return out


# 实例级信号：问的是"现在/昨天的具体那一批、那一台、多少片"，
# 而本体是类型级的——没有任何 Lot 实例、时间戳、计数。
# 下面几类词命中就直接拒，不进词表匹配：走匹配的话，锚点会定位到
# core.state.hold 之类的类型级概念、意图词又能命中 Hold/WIP 类 CQ，
# 分数就够了，于是答不了的问题被放行。
# 那是最坏方向的错——答不出来会促使人去补，答错了不会。
RE_TIME = re.compile(
    r"昨天|今天|今日|当前|现在|目前|近一周|近一个月|最近|过去|未来|上周|本周|上个月|"
    r"这个月|本月|上一个班次|班次|小时内|分钟内|即将|接下来|一段时间|历史记录|"
    # 数字+时间单位一律算实例级（"超过1小时"、"多了2小时"、"3天后"）。
    # 不要求后缀内/后/前——真实问法里经常没有。
    r"\d+\s*(小时|分钟|天|周|月|季度|年)")
RE_QTY = re.compile(
    r"多少|几个|几片|几台|几批|几张|top\s*\d*n?|前\s*\d+|最高的\s*\d|排名|"
    r"超过了|超过\s*\d|多了\s*\d|统计|总数|占比|list\b|清单输出")
# 复数指示词指向"具体那几批/那几台"，是实例级的强信号。
# 单数"这个"不算——"这个异常会引发哪些下游异常"是正常的类型级问法。
RE_DEIXIS = re.compile(r"这些|那些|这批|该批|这几|那几|这台|该台|这片|这张|这条 ?lot", re.I)
RE_INST = re.compile(
    r"\b(eqp|cvd|pvd|cmp|litho|etch)\s*\d+\b|\blot\s*[a-z0-9]{3,}|\bwo\d+|"
    r"\b[a-z]{3,}\d{3,}\b|客户\s*[a-z]\b|型号\s*[a-z]\b|recipe\s*[a-z]\b|"
    r"step\s*\d+|chamber\s*[a-z0-9]\b|\d+\s*号腔", re.I)


def instance_level(question: str) -> list[str]:
    """返回命中的实例级信号类别。空列表表示这是个类型级问题。"""
    q = question.lower()
    hits = []
    if RE_TIME.search(q):
        hits.append("时间限定")
    if RE_QTY.search(q):
        hits.append("计数/排名")
    if RE_INST.search(question):
        hits.append("具体实例 ID")
    if RE_DEIXIS.search(question):
        hits.append("指代具体对象")
    return hits


def score(question: str, item: dict, et: dict) -> tuple[float, list[str], list[str]]:
    """返回 (得分, 命中的意图词, 命中的实体ID)。"""
    qn, qt = norm(question), terms_of(question)
    intent = [t for t in (item.get("match_terms") or []) if norm(t) in qn]

    # 整词匹配精确但脆：意图词写"检出点"，问句说"哪道工序检出"就漏掉。
    # 补一层 bigram 重叠做部分命中，权重减半，避免为每种说法都手写一条。
    partial = 0.0
    if qt:
        for t in (item.get("match_terms") or []):
            if norm(t) in qn:
                continue
            tt = terms_of(t)
            # 只对 4 字以上的词做部分匹配。短词的 bigram 太少，0.5 阈值等于
            # "撞上一半就算"，"在哪台"撞"差在哪"就把问流程差异的问句拉到问机台。
            if len(tt) < 3:
                continue
            if len(tt & qt) / len(tt) >= 0.5:
                partial += 0.5

    anchors: list[str] = []
    req = (item.get("requires") or {}).get("entity_types") or []
    for etype in req:
        for name, eid in et.get(etype, []):
            if name in qn and eid not in anchors:
                anchors.append(eid)

    # 意图词一个字都没命中时，实体名不足以定类：问"颗粒突增"可以是问原因、
    # 问检出、问处置，光靠实体名分不出来，交给 MIN_SCORE 拦掉。
    s = W_INTENT * (len(intent) + partial) + W_ENTITY * len(anchors)
    # 关系名直接出现在问句里也算意图信号（例如有人直接问 "may_cause"）
    for rt in (item.get("requires") or {}).get("relations") or []:
        if rt in qn:
            s += W_INTENT
            intent.append(rt)
    return s, intent, anchors


def walk(graph: dict, entities: dict, start: str, steps: list[dict]) -> list[str]:
    """按 probe 的 steps 沿图走，返回终点集合。

    step: {via: 关系类型, dir: out|in, to_type: 可选的终点类型过滤}
    """
    frontier = {start}
    for st in steps:
        adj = graph["out_adjacency"] if st.get("dir", "out") == "out" else graph["in_adjacency"]
        key = "to" if st.get("dir", "out") == "out" else "from"
        nxt: set[str] = set()
        for node in frontier:
            for edge in adj.get(node, []):
                if edge["type"] != st["via"]:
                    continue
                tgt = edge.get(key) or edge.get("to") or edge.get("from")
                if st.get("to_type") and entities.get(tgt, {}).get("type") != st["to_type"]:
                    continue
                nxt.add(tgt)
        frontier = nxt
        if not frontier:
            return []
    return sorted(frontier)


def dig(obj, path: list[str]) -> list:
    """按点分路径取值，遇列表就展开。a.b[].c 写成 ['a','b','c']。"""
    cur = [obj]
    for seg in path:
        nxt = []
        for node in cur:
            if isinstance(node, list):
                nxt.extend(x.get(seg) for x in node if isinstance(x, dict))
            elif isinstance(node, dict):
                nxt.append(node.get(seg))
        cur = [x for x in nxt if x not in (None, "", [], {})]
    out = []
    for x in cur:
        out.extend(x) if isinstance(x, list) else out.append(x)
    return [x for x in out if x not in (None, "", [], {})]


def probe_field(spec: dict, entities: dict, kb, anchors: list[str]) -> dict:
    """字段型 probe：有些问句的答案不在边上，而在字段里。

    CQ02 的判别依据是 possible_causes[].discriminator，CQ08 的流程顺序是
    Route.steps，CQ19 的传导路径是 economic_hooks.affects。这些都不是图的边，
    走 walk() 永远返回空——但问题确实可答。硬把它们塞成关系反而会造出假边。
    """
    scope = spec.get("scope", "entity")            # entity | kb
    path = [p for p in spec["field"].replace("[]", "").split(".") if p]
    want = spec.get("contains")

    if scope == "kb":
        cases = kb if isinstance(kb, list) else (kb.get("cases") or [])
        pool = {c.get("id") or f"case#{i}": c for i, c in enumerate(cases)}
    else:
        pool = {i: e for i, e in entities.items()
                if not spec.get("from_type") or e.get("type") == spec["from_type"]}
    picked = {a: pool[a] for a in anchors if a in pool}
    scanned_all = not picked
    if scanned_all:
        picked = pool

    hits = {}
    for key, obj in picked.items():
        vals = dig(obj, path)
        if want:
            vals = [v for v in vals if want in (v if isinstance(v, (list, str)) else str(v))]
        hits[key] = [str(v)[:60] for v in vals]
    ok = {k: v for k, v in hits.items() if v}
    return {
        "from_type": spec.get("from_type") or f"{scope}:{spec['field']}",
        "scanned_all": scanned_all,
        "total": len(picked),
        "non_empty": len(ok),
        "samples": {k: v[:5] for k, v in list(ok.items())[:3]},
        "empty_samples": [k for k, v in hits.items() if not v][:3],
    }


def probe(item: dict, entities: dict, graph: dict, anchors: list[str],
          kb=None) -> dict | None:
    """真走一遍路径。有锚点就从锚点走，否则遍历该类型全部实体统计覆盖率。"""
    spec = item.get("probe")
    if not spec:
        return None
    # 先选 spec 再分派，不能反过来。CQ17 的 probe 是边型 + 字段型混排：
    # 锚点是 State 时走 blocks 边，否则退到 kb 实例的 possible_causes。
    # 若按 spec[0] 的形态先分派，字段型那条永远走不到。
    specs = spec if isinstance(spec, list) else [spec]
    atypes = {entities.get(a, {}).get("type") for a in anchors}
    picked = next((s for s in specs if s.get("from_type") in atypes), None)
    if picked is None:
        # 无锚点匹配：优先挑字段型，它不依赖锚点也能给出有意义的覆盖率
        picked = next((s for s in specs if s.get("field")), specs[0])
    if picked.get("field"):
        return probe_field(picked, entities, kb or {}, anchors)
    # 上面按锚点类型挑 spec 的逻辑同时服务双向问句（"某工序属于哪段" /
    # "某段有哪些工序"）：只写一个方向时锚点对不上 from_type 就会退化成全表扫描，
    # 起点白定位了。
    spec = picked
    steps, start_type = spec["steps"], spec["from_type"]
    starts = [a for a in anchors if entities.get(a, {}).get("type") == start_type]
    scanned_all = not starts
    if scanned_all:
        starts = [i for i, e in entities.items() if e["type"] == start_type]

    hits = {s: walk(graph, entities, s, steps) for s in starts}
    ok = {s: v for s, v in hits.items() if v}
    return {
        "from_type": start_type,
        "scanned_all": scanned_all,
        "total": len(starts),
        "non_empty": len(ok),
        "samples": {s: v[:5] for s, v in list(ok.items())[:3]},
        "empty_samples": [s for s, v in hits.items() if not v][:3],
    }


def rank(question: str, cq: dict, et: dict) -> list[dict]:
    out = []
    for bucket in ("in_scope", "out_of_scope"):
        for item in cq.get(bucket) or []:
            s, intent, anchors = score(question, item, et)
            if s > 0:
                out.append({"bucket": bucket, "item": item, "score": s,
                            "intent": intent, "anchors": anchors})
    # 同分时 out_of_scope 优先：错拒的代价是多查一次，错答的代价是信任。
    # "扩散炉停机会引发什么异常"曾因 CQ03 的"会引发"命中而被判可答，
    # 而它实际该落 OOS11——设备与异常之间没有边。
    out.sort(key=lambda x: (-x["score"], x["bucket"] != "out_of_scope", x["item"]["id"]))
    return out


def main() -> int:
    C.setup_console()
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    as_json = "--json" in sys.argv
    do_probe = "--probe" in sys.argv
    if not args:
        print(__doc__)
        return 3
    question = " ".join(args)

    cq = C.load_competency()
    entities, _ = C.load_entities()
    et = entity_terms(entities)
    graph_path = C.ROOT / "build" / "graph.json"
    graph = json.loads(graph_path.read_text(encoding="utf-8")) if graph_path.is_file() else None

    # 实例级问题先拦下来。词表匹配对这类问题只会给出似是而非的命中：
    # 问"今日报废多少片"会锚定 core.state.scrap 并命中 CQ18，
    # 而 CQ18 答的是"报废这个状态意味着什么"，不是今天的片数。
    inst = instance_level(question)
    cands = rank(question, cq, et)
    top = cands[0] if cands and cands[0]["score"] >= MIN_SCORE else None
    if inst:
        top = None
    pr = None
    if top and top["bucket"] == "in_scope" and do_probe and graph:
        # 字段型 probe 要读 kb 实例（判别依据、传导路径都在实例里），
        # 只有真要探测时才加载，免得每次问句都多读一遍全部 case。
        pr = probe(top["item"], entities, graph, top["anchors"], C.load_kb())

    if as_json:
        print(json.dumps({
            "question": question,
            "verdict": ("instance_level" if inst
                        else "answerable" if top and top["bucket"] == "in_scope"
                        else "out_of_scope" if top else "unlisted"),
            "instance_signals": inst,
            "matched": top and {"id": top["item"]["id"], "score": top["score"],
                                "intent": top["intent"], "anchors": top["anchors"]},
            "probe": pr,
            "runner_up": [{"id": c["item"]["id"], "score": c["score"]} for c in cands[1:4]],
        }, ensure_ascii=False, indent=2))
        if inst:
            return 2
        return 0 if top and top["bucket"] == "in_scope" else (2 if top else 3)

    n = lambda i: f"{i}（{entities[i]['name_zh']}）" if i in entities else i  # noqa: E731
    print(f"问题：{question}")
    print("-" * 68)

    if inst:
        print(f"判定：超出当前本体范围（实例级问题：{'、'.join(inst)}）")
        print("\n这个问题问的是具体那一批、那一台、多少片——需要 MES/EAP 的实时或"
              "历史数据。\n本体与知识库是类型级的：有"
              "\"Hold 意味着什么、会阻塞什么\"，没有任何 Lot 实例、时间戳、计数。")
        print("\n要答得了得先建：接入 MES/EAP 数据源，把本体当查询的语义层用"
              "（概念对到表和字段）。\n这属于第三层经营模型的职责，不是本体补几个实体能解决的。")
        if cands and cands[0]["score"] >= MIN_SCORE:
            c = cands[0]
            print(f"\n对应的类型级问题是 {c['item']['id']}：{c['item']['q']}")
            print("  ——它答的是概念，不是这个问句要的数据。别拿它的答案冒充。")
        print("\n不要用模型知识硬编答案，也不要编造数字。")
        return 2

    if not top:
        print("判定：清单里没有这个问题")
        print("\n这既不是承诺能答，也不是明确拒绝——属于边界未声明的区域。")
        print("回答前应先判断它是否值得进清单："
              "\n  能沿图走通  -> 加一条 in_scope（含 requires 与 answered_via）"
              "\n  走不通      -> 加一条 out_of_scope（含 missing 与 precondition）")
        if cands:
            print("\n分数不足的候选（未达阈值 %.0f）：" % MIN_SCORE)
            for c in cands[:3]:
                print(f"  {c['item']['id']} {c['score']:.0f} 分  {c['item']['q'][:38]}")
        return 3

    item = top["item"]
    if top["bucket"] == "out_of_scope":
        print(f"判定：超出当前本体范围（{item['id']}，{top['score']:.0f} 分）")
        print(f"\n对应清单问题：{item['q']}")
        print(f"\n缺什么：{str(item['missing']).strip()}")
        print(f"\n要答得了得先建：{str(item['precondition']).strip()}")
        if item.get("note"):
            print(f"\n补充：{str(item['note']).strip()}")
        # 拒绝但给出改写方向：问句常常只是措辞落在界外，换个角度库里有答案。
        # 例如"光刻机停机会波及哪些异常"走不通（设备无边），但
        # "光刻机非计划停机这个异常会引发哪些下游异常"走 CQ03 就通。
        near = [c for c in cands[1:]
                if c["bucket"] == "in_scope" and c["score"] >= MIN_SCORE][:2]
        if near:
            print("\n相近的可答问题（换成这个问法库里有答案）：")
            for c in near:
                print(f"  {c['item']['id']} {c['score']:.0f} 分  {c['item']['q']}")
        print("\n不要用模型知识硬编答案——库外知识伪装成库内知识，"
              "溯源链断掉且无人知晓。")
        return 2

    print(f"判定：可答（{item['id']}，{top['score']:.0f} 分）")
    print(f"\n对应清单问题：{item['q']}")
    print(f"答案路径：{str(item['answered_via']).strip()}")
    req = item.get("requires") or {}
    if req.get("entity_types"):
        print(f"依赖实体类型：{', '.join(req['entity_types'])}")
    if req.get("relations"):
        print(f"依赖关系：{', '.join(req['relations'])}")
    if top["anchors"]:
        print(f"问句里定位到的实体：{', '.join(n(a) for a in top['anchors'][:5])}")
    if item.get("caveat"):
        print(f"\n限制：{str(item['caveat']).strip()}")

    if pr:
        print("\n-- 路径探测 --")
        scope = f"全部 {pr['total']} 个 {pr['from_type']}" if pr["scanned_all"] \
            else f"问句锚点 {pr['total']} 个"
        print(f"范围：{scope}｜走通 {pr['non_empty']}/{pr['total']}")
        for s, v in pr["samples"].items():
            print(f"  {n(s)} -> {', '.join(n(x) for x in v)}")
        if pr["non_empty"] == 0:
            print("  路径全空：清单承诺了但实际走不通，应修正 CQ 或补结构")
            return 2
        if pr["scanned_all"] and pr["non_empty"] < pr["total"]:
            gap = pr["total"] - pr["non_empty"]
            print(f"  注意：{gap} 个走不通，"
                  f"例如 {', '.join(n(x) for x in pr['empty_samples'])}")
            # 覆盖率分档。44/45 和 9/35 是两回事，同一句提示会把噪音
            # 和真问题混在一起——CQ07 少一个（产线末端工序没有后继）是
            # 结构正确的表现，不该催人写 caveat。
            rate = pr["non_empty"] / pr["total"]
            if rate < 0.5 and item.get("caveat"):
                print(f"  覆盖 {rate:.0%}：低覆盖已由 caveat 说明，"
                      "核对 caveat 写的适用范围与这里走通的对象是否一致")
            elif rate < 0.5:
                print(f"  覆盖 {rate:.0%}：问句与路径很可能配错了。"
                      "要么收窄问句并写 caveat，要么这条该进 out_of_scope")
            elif rate < 0.9:
                print(f"  覆盖 {rate:.0%}：这条只对部分对象成立，"
                      "caveat 应写清适用范围")
            else:
                print(f"  覆盖 {rate:.0%}：属正常边界（如产线首尾工序无前驱/后继），"
                      "无需处理")
    elif do_probe:
        print("\n（该条目未定义 probe，无法机械验证路径）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
