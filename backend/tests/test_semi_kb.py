from app.services.semi_kb import SemiKbAdapter, SemiKbError, _balanced_quota, _evenly_sample

import asyncio

import pytest


def _first_letter(term: dict) -> str:
    return term["iri"].split(":")[-1].split("#")[-1].split("/")[-1][0].upper()


def test_semantic_counts_are_real():
    counts = SemiKbAdapter().semantic_counts()
    assert counts["classes"] > 0
    assert counts["properties"] > 0
    assert counts["semantic_triples"] > 0


def test_artifact_counts_include_vfab_state():
    counts = SemiKbAdapter().artifact_counts()
    # vfab_ingest.py 只产出这两种状态：无资料 awaiting_source / 已接入 available。
    assert counts["vfab_state"] in {"awaiting_source", "available"}
    assert counts["simulation_scenarios"] >= 1


def test_evenly_sample_keeps_endpoints_and_no_dupes():
    items = list(range(100))
    picked = _evenly_sample(items, 10)
    assert len(picked) == 10
    assert picked[0] == 0 and picked[-1] == 99  # 含首尾
    assert len(set(picked)) == 10  # 无重复
    assert _evenly_sample(items, 0) == []
    assert _evenly_sample([1, 2], 5) == [1, 2]  # k>=n 原样返回


def test_balanced_quota_respects_capacity_and_sums_to_limit():
    sizes = {"class": 850, "object_property": 436, "datatype_property": 895}
    quota = _balanced_quota(sizes, 200)
    assert sum(quota.values()) == 200
    assert all(quota[k] <= sizes[k] for k in sizes)  # 不超容量
    assert all(quota[k] >= 1 for k in sizes)  # 非空桶都有代表
    assert quota["datatype_property"] >= quota["object_property"]  # 大桶名额更多（按占比）
    # 名额 >= 总量：全取，不采样
    assert _balanced_quota({"a": 3, "b": 4}, 100) == {"a": 3, "b": 4}
    # 容量夹取：小桶最多给到自身容量，余数流向大桶，总和仍等于 limit
    tight = _balanced_quota({"a": 2, "b": 100}, 50)
    assert tight["a"] <= 2 and sum(tight.values()) == 50


def test_ontology_context_default_is_class_first_and_unchanged():
    ctx = SemiKbAdapter().ontology_context(limit=200)
    assert ctx["total"] > 0
    assert set(ctx["by_kind"]) == {"class", "object_property", "datatype_property"}
    assert ctx["total"] == sum(ctx["by_kind"].values())
    assert ctx["strategy"] == "kind_iri_order"
    # 默认模式 class 优先：前 200 应全是 class（class 本身就超过 200）
    assert {t["kind"] for t in ctx["terms"]} == {"class"}


def test_ontology_context_balanced_spans_kinds_and_letters():
    ctx = SemiKbAdapter().ontology_context(limit=200, balanced=True)
    assert ctx["strategy"] == "balanced"
    kinds = {t["kind"] for t in ctx["terms"]}
    assert kinds == {"class", "object_property", "datatype_property"}  # 三类都入样
    letters = {_first_letter(t) for t in ctx["terms"]}
    assert len(letters) >= 10  # 跨字母段覆盖（旧行为仅 A-D 共 4 段）
    iris = [t["iri"] for t in ctx["terms"]]
    assert len(iris) == len(set(iris))  # 无重复
    assert len(ctx["terms"]) <= 200


# --- process_candidates 里只读 simulate.py 的有界并行（Fix 3）---------------

def _write_scenario(tmp_path, name: str, scenario_id: str):
    path = tmp_path / f"{name}.yaml"
    path.write_text(f"scenario:\n  id: {scenario_id}\n  name: {name}\n", encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_simulate_runs_execute_concurrently(tmp_path, monkeypatch):
    """多个只读 simulate.py 子进程有界并行（此前逐个串行）。"""
    adapter = SemiKbAdapter()
    scenarios = [_write_scenario(tmp_path, f"scn{i}", f"urn:pxai:test:conc:{i}") for i in range(4)]
    active = {"now": 0, "max": 0}
    calls = {"simulate": 0}

    async def fake_command(script, *args, **kwargs):
        if script == "simulate.py":
            calls["simulate"] += 1
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
            await asyncio.sleep(0.02)
            active["now"] -= 1
        return {"exit_code": 0, "output": "", "duration_seconds": 0.0}

    async def fake_cross_validate():
        return {"passed": True}

    monkeypatch.setattr(adapter, "command", fake_command)
    monkeypatch.setattr(adapter, "cross_validate", fake_cross_validate)
    monkeypatch.setattr(adapter, "candidate_alignment", lambda sources: {})

    result = await adapter.process_candidates(
        {"semantic": [], "business": [], "simulation": scenarios,
         "knowledge": [], "rules": [], "mappings": [], "articles": []},
        publish=False,
    )
    assert calls["simulate"] == 4
    assert active["max"] > 1  # 真实并发，而非串行
    assert result["accepted_candidates"]["simulation"] == 4


@pytest.mark.asyncio
async def test_simulate_first_failure_still_raises(tmp_path, monkeypatch):
    """任一场景失败即整批失败，并确定性报告最小 index 的失败场景；其余仍跑完。"""
    adapter = SemiKbAdapter()
    ids = ["urn:pxai:test:okA", "urn:pxai:test:badB", "urn:pxai:test:badC"]
    scenarios = [_write_scenario(tmp_path, f"s{i}", sid) for i, sid in enumerate(ids)]
    ran = []

    async def fake_command(script, *args, **kwargs):
        if script == "simulate.py":
            arg = args[0]
            ran.append(arg)
            bad = "bad" in arg.lower()
            return {"exit_code": 1 if bad else 0, "output": "boom" if bad else "", "duration_seconds": 0.0}
        return {"exit_code": 0, "output": "", "duration_seconds": 0.0}

    async def fake_cross_validate():
        return {"passed": True}

    monkeypatch.setattr(adapter, "command", fake_command)
    monkeypatch.setattr(adapter, "cross_validate", fake_cross_validate)
    monkeypatch.setattr(adapter, "candidate_alignment", lambda sources: {})

    with pytest.raises(SemiKbError) as excinfo:
        await adapter.process_candidates(
            {"semantic": [], "business": [], "simulation": scenarios,
             "knowledge": [], "rules": [], "mappings": [], "articles": []},
            publish=False,
        )
    assert "badB" in str(excinfo.value)  # 报告最小 index 的失败（badB 在 badC 之前）
    assert len(ran) == 3  # 有界并行：失败后其余场景仍跑完（等价放宽）


# --- existing_candidate_ids 去重清单 + 候选 ID 撞车幂等策略（P2）----------------

def test_existing_candidate_ids_lists_knowledge_and_rules(tmp_path):
    """助手从 knowledge/entries 与 rules/registry.json 汇总既有 ID，供 Agent 去重规避。"""
    root = tmp_path
    (root / "knowledge" / "entries").mkdir(parents=True)
    (root / "ontology" / "rules").mkdir(parents=True)
    for slug in ("urn_a", "urn_b", "urn_c"):
        (root / "knowledge" / "entries" / f"{slug}.json").write_text("{}", encoding="utf-8")
    (root / "ontology" / "rules" / "registry.json").write_text(
        '{"rules":[{"rule_id":"R-AUTO-x"},{"rule_id":"R-AUTO-y"}]}', encoding="utf-8")
    ctx = SemiKbAdapter(root=root).existing_candidate_ids()
    assert ctx["knowledge_total"] == 3 and ctx["rule_total"] == 2
    assert set(ctx["knowledge_ids"]) == {"urn_a", "urn_b", "urn_c"}
    assert set(ctx["rule_ids"]) == {"R-AUTO-x", "R-AUTO-y"}
    assert ctx["truncated"] is False
    # 空库不报错
    empty = SemiKbAdapter(root=tmp_path / "nope").existing_candidate_ids()
    assert empty["knowledge_total"] == 0 and empty["rule_ids"] == []


def test_existing_candidate_ids_truncates_and_splits_quota(tmp_path):
    """超出 limit 时按两类占比分配名额并等距采样，附全量计数与截断标记。"""
    root = tmp_path
    (root / "knowledge" / "entries").mkdir(parents=True)
    (root / "ontology" / "rules").mkdir(parents=True)
    for i in range(30):
        (root / "knowledge" / "entries" / f"urn_{i:03d}.json").write_text("{}", encoding="utf-8")
    (root / "ontology" / "rules" / "registry.json").write_text(
        '{"rules":[' + ",".join(f'{{"rule_id":"R-AUTO-{i:03d}"}}' for i in range(10)) + "]}",
        encoding="utf-8")
    ctx = SemiKbAdapter(root=root).existing_candidate_ids(limit=8)
    assert ctx["knowledge_total"] == 30 and ctx["rule_total"] == 10
    assert ctx["truncated"] is True
    assert len(ctx["knowledge_ids"]) + len(ctx["rule_ids"]) <= 8
    assert len(ctx["knowledge_ids"]) >= len(ctx["rule_ids"])  # 大桶名额更多（按占比）
    assert len(ctx["rule_ids"]) >= 1  # 非空桶留代表


async def _run_collision_case(adapter, monkeypatch, candidates):
    # _ensure_root 要求 scripts/kb.py 存在；命令本身被 mock，内容无关紧要。
    scripts = adapter.root / "scripts"; scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "kb.py").write_text("# stub\n", encoding="utf-8")

    async def fake_command(script, *args, **kwargs):
        return {"exit_code": 0, "output": "", "duration_seconds": 0.0}

    async def fake_cross_validate():
        return {"passed": True}

    monkeypatch.setattr(adapter, "command", fake_command)
    monkeypatch.setattr(adapter, "cross_validate", fake_cross_validate)
    monkeypatch.setattr(adapter, "candidate_alignment", lambda sources: {})
    payload = {"semantic": [], "business": [], "simulation": [],
               "knowledge": [], "rules": [], "mappings": [], "articles": []}
    payload.update(candidates)
    return await adapter.process_candidates(payload, publish=False)


@pytest.mark.asyncio
async def test_business_collision_identical_skips_different_quarantines(tmp_path, monkeypatch):
    """经营模型 ID 撞车：字节一致→静默跳过；不同→隔离；均不中止整批、不覆盖已入库。"""
    adapter = SemiKbAdapter(root=tmp_path)
    live = tmp_path / "business" / "models"; live.mkdir(parents=True)
    same_body = "model:\n  id: urn:pxai:biz:same\n  name: same\n"
    diff_body = "model:\n  id: urn:pxai:biz:diff\n  name: NEW\n"
    (live / "urn-pxai-biz-same.yaml").write_text(same_body, encoding="utf-8")
    (live / "urn-pxai-biz-diff.yaml").write_text("model:\n  id: urn:pxai:biz:diff\n  name: OLD\n", encoding="utf-8")
    src_dir = tmp_path / "cand"; src_dir.mkdir()
    same = src_dir / "same.yaml"; same.write_text(same_body, encoding="utf-8")
    diff = src_dir / "diff.yaml"; diff.write_text(diff_body, encoding="utf-8")
    result = await _run_collision_case(adapter, monkeypatch, {"business": [same, diff]})
    assert result["accepted_candidates"]["business"] == 0  # 无新增（都撞车）
    reasons = [q for q in result["quarantined_candidates"] if q["category"] == "business"]
    assert len(reasons) == 1 and "diff" in reasons[0]["reason"]  # 仅内容不同的被隔离
    # 已入库内容未被改写：不同内容的既有文件仍是 OLD
    assert "OLD" in (live / "urn-pxai-biz-diff.yaml").read_text(encoding="utf-8")


# --- business_domain_coverage 业务视角领域覆盖（与 feature_gap 同口径）---------

def test_business_domain_coverage_matches_feature_gap_unmapped():
    """业务域覆盖的『待映射合计』必须等于 feature_gap.business_relevant（同一 theme+去噪口径）。"""
    adapter = SemiKbAdapter()
    cov = adapter.business_domain_coverage()
    gap = adapter.feature_gap()
    # 数据缺失时两者都返回零结构，等式仍成立。
    assert cov["business_unmapped"] == gap["business_relevant"]
    # 每个业务域自洽：mapped + unmapped == total，且 mapped 非负。
    for d in cov["domains"]:
        assert d["mapped"] + d["unmapped"] == d["total"]
        assert d["mapped"] >= 0
    # 合计自洽。
    assert cov["business_mapped"] + cov["business_unmapped"] == cov["business_total"]
    if cov["business_total"]:
        assert cov["domains"], "有业务特征则必列出业务域"


# --- 头部计数：落纯实例（去记账）+ 排除引擎台账；领域归属经 subClassOf 祖先增强 -------------

def test_headline_individuals_excludes_governance_and_accounting():
    """头部 individuals 落到纯实例：先去 owl:*/owl:Ontology 与 prov:Entity/rdf:Statement 记账，
    再剔除引擎自证台账（全部类型命中 governance 词表且无一归入领域）。用 adapter 自身辅助方法
    独立重算，校验 semantic_counts 的组合逻辑；记账/台账节点仍在合并图（被排除、非缺失）。"""
    from rdflib import RDF

    adapter = SemiKbAdapter()
    counts = adapter.semantic_counts()
    data = adapter._load_data_graph()
    _, class_to_module, _ = adapter._load_schema_graph()

    subj_types: dict = {}
    for s, _, o in data.triples((None, RDF.type, None)):
        if o in adapter._INSTANCE_NOISE_TYPES:
            continue
        subj_types.setdefault(s, set()).add(o)
    upper = len(subj_types)  # 仅去噪的上界（未剔台账）
    governance = {
        s for s, types in subj_types.items()
        if not any(class_to_module.get(t) in adapter._DOMAIN_MODULE_LABELS for t in types)
        and all(adapter._is_governance_type(t) for t in types)
    }
    assert counts["individuals"] == upper - len(governance)
    assert len(governance) > 0            # 实库确有引擎台账被排除
    assert counts["individuals"] < upper  # 故头部严格小于"仅去噪"口径

    prov_entity = adapter._PROV_ENTITY
    accounting = {s for s, _, o in data.triples((None, RDF.type, None))
                  if o in {RDF.Statement, prov_entity}}
    assert len(accounting) > 0  # 记账节点仍在合并图里（被排除、非缺失）


def test_resolve_domain_modules_by_ancestry():
    """generated 里声明、但沿 rdfs:subClassOf 上溯可达某策展领域根的类，被重归属到该领域模块
    （一层/多层均可）；无领域祖先者保持声明模块；领域声明本身不动。纯结构推导、确定性。"""
    from rdflib import Graph, RDF, RDFS, URIRef
    from rdflib.namespace import OWL

    adapter = SemiKbAdapter()
    root = URIRef("urn:pxai:semi:EquipmentDowntimeAnomaly")    # 假装声明于 equipment 模块
    mid = URIRef("urn:pxai:semi:DryPumpDowntimeAnomaly")       # generated，父=root
    leaf = URIRef("urn:pxai:semi:DryPumpSealDowntimeAnomaly")  # generated，父=mid
    gov = URIRef("urn:pxai:semi:SHACLNodeShape")               # generated，无领域祖先
    g = Graph()
    for c in (root, mid, leaf, gov):
        g.add((c, RDF.type, OWL.Class))
    g.add((mid, RDFS.subClassOf, root))
    g.add((leaf, RDFS.subClassOf, mid))
    declared = {root: "equipment", mid: "generated", leaf: "generated", gov: "generated"}

    resolved = adapter._resolve_domain_modules(g, declared)
    assert resolved[mid] == "equipment"    # 一层上溯
    assert resolved[leaf] == "equipment"   # 多层上溯（自维持：领域根的深层子类自动计入）
    assert resolved[gov] == "generated"    # 无领域祖先 → 保持
    assert resolved[root] == "equipment"   # 领域声明不动
    assert declared[mid] == "generated"    # 不改入参


def test_is_governance_type_matches_engine_ledger_not_domain():
    """governance 分类器只命中引擎自证台账类，不误伤领域类（防止把真实实例算进台账而漏计）。"""
    from rdflib import URIRef

    adapter = SemiKbAdapter()
    for name in ("SHACLNodeShape", "OntologyValidationRun", "ReleaseGate",
                 "FeatureMappingCoverageMetric", "GoldenMetricBaselineRecord", "MetricGroup"):
        assert adapter._is_governance_type(URIRef(f"urn:pxai:semi:{name}")), name
    for name in ("EquipmentDowntimeAnomaly", "BinClassification", "CatalogConcept",
                 "DryPumpDegradationDowntimeAnomaly", "EquipmentFaultMode"):
        assert not adapter._is_governance_type(URIRef(f"urn:pxai:semi:{name}")), name


def test_source_split_survives_merged_load():
    """合并加载 provenance.ttl 后，来源拆分链（prov:Entity→specializationOf→sourceType）仍可达：
    knowledge 非零即证明溯源已被合并进 data 图（否则链断、三项塌成 0）。
    并锁两条口径不变量：领域实例按唯一主语计（≤ 头部总数）、三类来源之和恰等于领域总数。"""
    counts = SemiKbAdapter().semantic_counts()
    assert counts["individuals_knowledge"] > 0
    for key in ("individuals_knowledge", "individuals_operational", "individuals_untagged"):
        assert counts[key] >= 0
    # 领域实例是全部实例的子集（唯一主语计数，多类型主语不重复），故不得超过头部总数
    assert counts["individuals_domain"] <= counts["individuals"]
    # 三类来源互斥且穷尽领域实例
    assert (counts["individuals_knowledge"] + counts["individuals_operational"]
            + counts["individuals_untagged"]) == counts["individuals_domain"]
    # 底线：尚未导入真实产线数据（无 observed/internal_feature 来源）前，产线数据必须为 0。
    # 人工手写种子(human)、外部标准(vfab)、先验/检索/推定均属知识，不得误增产线数据。
    assert counts["individuals_operational"] == 0


def test_erp_modules_registered_as_domains():
    """ERP 两模块（财务会计/订单到收款）已登记为策展领域，且都归属 erp 源系统。"""
    adapter = SemiKbAdapter()
    labels = adapter._DOMAIN_MODULE_LABELS
    assert labels.get("erp-financial") == "ERP财务会计"
    assert labels.get("erp-sales-o2c") == "ERP订单到收款"
    # 级联"地基"配置：每个策展领域模块必须恰好归属一个源系统（否则 system_unmapped 会非零）
    all_system_modules = set()
    for modules in adapter._SOURCE_SYSTEM_MODULES.values():
        all_system_modules |= modules
    assert set(labels) == all_system_modules, "策展模块与源系统登记必须一一对齐"
    assert adapter._SOURCE_SYSTEM_MODULES["erp"] == {
        "erp-financial", "erp-sales-o2c",
        "erp-controlling", "erp-asset-accounting", "erp-inventory-valuation",
    }


def test_erp_domains_have_instances():
    """migrate 后 ERP 领域应有落地实例（财务主数据 + O2C 单据链），证明 YAML→current.ttl 通路打通。"""
    dc = SemiKbAdapter().domain_coverage()
    by_module = {d["module"]: d for d in dc["domains"]}
    assert by_module["erp-financial"]["instances"] > 0
    assert by_module["erp-sales-o2c"]["instances"] > 0
    # 两个 ERP 领域都应各有 class 声明
    assert by_module["erp-financial"]["classes"] > 0
    assert by_module["erp-sales-o2c"]["classes"] > 0


def test_source_system_cascade_partitions_domain_individuals():
    """级联"地基"不变式：源系统拆分是领域实例的一个划分——
    (1) system_unmapped 恒为 0（每个策展模块都登记了源系统）；
    (2) 各 system_<sys> 之和恰等于 individuals_domain（无遗漏、无重复）；
    (3) 制造与 ERP 两系统都非空。"""
    counts = SemiKbAdapter().semantic_counts()
    assert counts["system_unmapped"] == 0
    system_sum = sum(v for k, v in counts.items() if k.startswith("system_") and k != "system_unmapped")
    assert system_sum == counts["individuals_domain"]
    assert counts["system_manufacturing"] > 0
    assert counts["system_erp"] > 0


def test_source_system_orthogonal_to_segment():
    """源系统(一级)与工艺段(制造侧二级)是正交维度：制造侧实例数 ≥ 有工艺段标注的实例数，
    ERP 实例不带工艺段（跨段通用），故段拆分基数与源系统基数口径一致但切分维度不同。"""
    counts = SemiKbAdapter().semantic_counts()
    seg_total = (counts.get("segment_fab", 0) + counts.get("segment_ap", 0)
                 + counts.get("segment_cross", 0))
    # 段拆分与源系统拆分同基数（都基于策展领域个体唯一主语计）
    assert seg_total == counts["individuals_domain"]
    # 制造源系统至少覆盖所有带工艺段(fab/ap)标注的实例
    assert counts["system_manufacturing"] >= counts.get("segment_fab", 0) + counts.get("segment_ap", 0)


def test_export_scope_filter_partitions_by_source_system():
    """导出源系统子图不变式：manufacturing 与 erp 两个子图的主语集互不相交、
    并集恰等于 all(全部策展个体)——与读侧级联同口径，不重不漏。"""
    from app.services import exports, semi_kb
    from rdflib import Graph

    ttl = semi_kb.semi_kb.root / "knowledge" / "semantic" / "current.ttl"
    if not ttl.is_file():
        pytest.skip("engine current.ttl 不存在")

    def subjects(scope: str) -> set:
        graph = Graph()
        graph.parse(data=exports._filter_semantic_ttl(ttl, True, scope), format="turtle")
        return {s for s in graph.subjects()}

    all_subs, mfg_subs, erp_subs = subjects("all"), subjects("manufacturing"), subjects("erp")
    assert erp_subs, "ERP 子图不应为空"
    assert mfg_subs, "制造子图不应为空"
    assert mfg_subs.isdisjoint(erp_subs)  # 两源系统正交，主语不重叠
    assert mfg_subs | erp_subs == all_subs  # 并集=全部策展个体，无遗漏

