from app.services.semi_kb import SemiKbAdapter, _balanced_quota, _evenly_sample


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

