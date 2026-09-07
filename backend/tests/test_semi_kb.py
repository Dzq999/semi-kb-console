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

