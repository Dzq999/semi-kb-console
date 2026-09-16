"""find_synonyms 跨 O/D 撞名守卫：对象属性与数据属性同名不得进自动合并通道。

背景：find_synonyms 把对象/数据属性都归 kind='property' 聚类，跨 O/D 同名会被并进一簇。
机械合并会造成类型错位（对象属性取 IRI、数据属性取字面量）。守卫把这类簇单列
od_conflicts（人工定夺），绝不进 classes/properties（merge_synonyms/prune_synonyms 只读后两者）。
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

spec = importlib.util.spec_from_file_location("find_synonyms", SCRIPTS / "find_synonyms.py")
FS = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(FS)

SEMI = "urn:pxai:semi:"

_TTL = """@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix semi: <urn:pxai:semi:> .

# 跨 O/D 撞名：对象属性 + 数据属性共享中文 label「计划保养类型」
semi:hasPlannedMaintenanceType a owl:ObjectProperty ; rdfs:label "计划保养类型"@zh .
semi:pmScheduleType a owl:DatatypeProperty ; rdfs:label "计划保养类型"@zh .

# 同 O/D 纯属性同义（都数据属性，中文名撞名）→ 应正常进 properties 可合并
semi:processStepCount a owl:DatatypeProperty ; rdfs:label "工序步骤数"@zh .
semi:hasProcessStepCountValue a owl:DatatypeProperty ; rdfs:label "工序步骤数"@zh .

# 类同义（URI 变体）→ 应正常进 classes
semi:RandomDefect a owl:Class ; rdfs:label "随机缺陷"@zh .
semi:Random_Defect a owl:Class ; rdfs:label "随机性缺陷"@zh .
"""


class OdGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="syn-od-"))
        (self.tmp / "m.ttl").write_text(_TTL, encoding="utf-8")
        self._orig_modules = FS.MODULES
        FS.MODULES = self.tmp  # scan() 用 MODULES.glob("*.ttl")

    def tearDown(self):
        FS.MODULES = self._orig_modules

    def test_cross_od_routed_to_conflicts_not_merge(self):
        result = FS.scan()
        # 跨 O/D 撞名单列，绝不进 properties
        prop_canons = {c["canonical"] for c in result["properties"]}
        self.assertNotIn("pmScheduleType", prop_canons)
        self.assertNotIn("hasPlannedMaintenanceType", prop_canons)
        self.assertEqual(len(result["od_conflicts"]), 1, result["od_conflicts"])
        conflict = result["od_conflicts"][0]
        self.assertEqual(conflict["object_properties"], ["hasPlannedMaintenanceType"])
        self.assertEqual(conflict["datatype_properties"], ["pmScheduleType"])
        self.assertEqual(conflict.get("label_zh"), "计划保养类型")

    def test_same_od_property_still_auto_mergeable(self):
        result = FS.scan()
        # 同为数据属性的「工序步骤数」应正常成可合并簇
        step = [c for c in result["properties"] if c.get("label_zh") == "工序步骤数"]
        self.assertEqual(len(step), 1, result["properties"])
        members = {step[0]["canonical"], *step[0]["merge"]}
        self.assertEqual(members, {"processStepCount", "hasProcessStepCountValue"})

    def test_class_synonym_unaffected(self):
        result = FS.scan()
        cls = [c for c in result["classes"]]
        self.assertEqual(len(cls), 1, cls)
        members = {cls[0]["canonical"], *cls[0]["merge"]}
        self.assertEqual(members, {"RandomDefect", "Random_Defect"})
        # 类簇不产生 od_conflicts
        self.assertTrue(all(c["kind"] == "property" for c in result["od_conflicts"]))


if __name__ == "__main__":
    unittest.main()
