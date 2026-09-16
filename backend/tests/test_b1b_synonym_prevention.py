"""B1b 生成时同义防增量——纯函数单测（不驱动 LLM）。"""
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services import semantic_pipeline as sp

SEMI = "urn:pxai:semi:"


def _empty_index():
    return {s: {"uri": defaultdict(set), "zh": defaultdict(set)} for s in sp._SYN_SECTIONS}


def _seed(index, section, iri, zh=None):
    fs = sp._fs()
    index[section]["uri"][fs._norm_localname(iri)].add(iri)
    if zh:
        index[section]["zh"][fs._norm_label(zh)].add(iri)


def test_uri_variant_redirect():
    idx = _empty_index()
    _seed(idx, "classes", SEMI + "RandomDefect", "随机缺陷")
    adds = {"classes": [{"iri": SEMI + "Random_Defect", "label_zh": "随机性缺陷", "subclass_of": [SEMI + "Anomaly"]}]}
    out, redir, warns = sp._dedupe_synonym_additions(adds, idx, {SEMI + "RandomDefect"})
    assert redir == {SEMI + "Random_Defect": SEMI + "RandomDefect"}, redir
    assert out["classes"] == [], out  # 声明被剔除
    assert any("重定向" in w for w in warns)


def test_same_zh_single_redirect_and_ref_rewrite():
    idx = _empty_index()
    _seed(idx, "object_properties", SEMI + "hasMaintenanceType", "维保计划类型")
    adds = {
        "object_properties": [{"iri": SEMI + "hasMaintenancePlanType", "label_zh": "维保计划类型",
                               "domain": [SEMI + "Equipment"], "range": [SEMI + "MaintPlan"]}],
        "individuals": [{"iri": SEMI + "pm1", "types": [SEMI + "MaintPlan"],
                         "objects": {SEMI + "hasMaintenancePlanType": [SEMI + "pmA"]}}],
        "object_assertions": [{"subject": SEMI + "x", "predicate": SEMI + "hasMaintenancePlanType", "object": SEMI + "y"}],
    }
    out, redir, warns = sp._dedupe_synonym_additions(adds, idx, {SEMI + "hasMaintenanceType"})
    assert redir == {SEMI + "hasMaintenancePlanType": SEMI + "hasMaintenanceType"}, redir
    assert out["object_properties"] == []
    # 引用被改指规范属性
    assert list(out["individuals"][0]["objects"].keys()) == [SEMI + "hasMaintenanceType"]
    assert out["object_assertions"][0]["predicate"] == SEMI + "hasMaintenanceType"


def test_ambiguous_zh_kept_with_warning():
    idx = _empty_index()
    _seed(idx, "classes", SEMI + "AlphaThing", "东西")
    _seed(idx, "classes", SEMI + "BetaThing", "东西")  # 两个既有规范同中文名
    adds = {"classes": [{"iri": SEMI + "GammaThing", "label_zh": "东西", "subclass_of": [SEMI + "Anomaly"]}]}
    out, redir, warns = sp._dedupe_synonym_additions(adds, idx, set())
    assert redir == {}, redir
    assert len(out["classes"]) == 1  # 保留
    assert any("待人工核" in w for w in warns)


def test_batch_internal_dedup():
    idx = _empty_index()
    adds = {"classes": [
        {"iri": SEMI + "NewThing", "label_zh": "新物", "subclass_of": [SEMI + "Anomaly"]},
        {"iri": SEMI + "New_Thing", "label_zh": "新物件", "subclass_of": [SEMI + "NewThing"]},  # 同 localname 变体
    ]}
    out, redir, warns = sp._dedupe_synonym_additions(adds, idx, set())
    assert redir == {SEMI + "New_Thing": SEMI + "NewThing"}, redir
    assert len(out["classes"]) == 1 and out["classes"][0]["iri"] == SEMI + "NewThing"


def test_cross_bucket_same_zh_not_merged():
    idx = _empty_index()
    _seed(idx, "object_properties", SEMI + "hasFoo", "福")  # 对象属性桶
    adds = {"datatype_properties": [{"iri": SEMI + "fooValue", "label_zh": "福",
                                     "domain": [SEMI + "Equipment"], "datatype": "http://www.w3.org/2001/XMLSchema#string"}]}
    out, redir, warns = sp._dedupe_synonym_additions(adds, idx, set())
    assert redir == {}, redir  # 跨 O/D 桶绝不自动并
    assert len(out["datatype_properties"]) == 1
    assert any("O/D 人工定夺" in w for w in warns)


def test_inventory_smoke():
    inv = sp._inventory()
    assert "syn_index" in inv and set(inv["syn_index"]) == set(sp._SYN_SECTIONS)
    assert inv["classes"] and inv["curated"]
    # 每桶结构完好
    for sec in sp._SYN_SECTIONS:
        assert "uri" in inv["syn_index"][sec] and "zh" in inv["syn_index"][sec]
    assert "depth" in inv and inv["depth"], "缺 subClassOf 深度图"
    assert inv["depth"].get(SEMI + "Anomaly") == 2, "Anomaly hub 应在 depth 2"
    assert inv["depth"].get(SEMI + "EquipmentAnomaly") == 3, "领域桶应在 depth 3"
    print("inventory: classes=%d objprop=%d dataprop=%d curated=%d maxdepth=%d"
          % (len(inv["classes"]), len(inv["object_properties"]), len(inv["datatype_properties"]),
             len(inv["curated"]), max(inv["depth"].values())))


# ---------------- B2 落位防扁平 ----------------

def _min_inventory(depth=None, classes=None):
    """给 _enforce_placement 用的最小 inventory（真库桶集合，可注入 depth）。"""
    inv = sp._inventory()
    if depth is not None:
        inv["depth"] = depth
    if classes is not None:
        inv["classes"] = classes
    return inv


def test_b2_autoroute_flat_hub_to_bucket():
    inv = sp._inventory()
    adds = {"classes": [{"iri": SEMI + "VacuumPumpSeizureAnomaly", "label_zh": "真空泵卡死异常",
                         "subclass_of": [SEMI + "Anomaly"]}]}
    out, warns = sp._enforce_placement(adds, inv)
    parents = out["classes"][0]["subclass_of"]
    assert parents == [SEMI + "EquipmentAnomaly"], parents  # 直挂 hub → 改挂设备异常桶
    assert any("改挂领域桶" in w for w in warns)


def test_b2_unmapped_stays_under_hub_with_warning():
    inv = sp._inventory()
    # Camp-A 机具词（分类器刻意不归域）
    adds = {"classes": [{"iri": SEMI + "SomeValidationFindingAnomaly", "label_zh": "某校验发现异常",
                         "subclass_of": [SEMI + "Anomaly"]}]}
    out, warns = sp._enforce_placement(adds, inv)
    assert out["classes"][0]["subclass_of"] == [SEMI + "Anomaly"]  # 留 hub
    assert any("无领域命中" in w for w in warns)


def test_b2_deep_parent_untouched():
    inv = sp._inventory()
    # 已挂到深处（非 hub）→ 不动
    adds = {"classes": [{"iri": SEMI + "NewChildX", "label_zh": "新子类",
                         "subclass_of": [SEMI + "EquipmentAnomaly"]}]}
    out, warns = sp._enforce_placement(adds, inv)
    assert out["classes"][0]["subclass_of"] == [SEMI + "EquipmentAnomaly"]
    assert not any("改挂" in w for w in warns)


def test_b2_shallow_parent_depth_guard_warns():
    # 挂到 depth0 根 Entity → depth 守卫告警（不改父、不硬拒）
    inv = sp._inventory()
    adds = {"classes": [{"iri": SEMI + "FloatyNewTopClass", "label_zh": "漂浮新类",
                         "subclass_of": [SEMI + "Entity"]}]}
    out, warns = sp._enforce_placement(adds, inv)
    assert any("过扁" in w or "浅父" in w for w in warns), warns
