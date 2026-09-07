"""基线哈希跳过缓存的单元测试（快，不跑 owlrl 全图闭包）。

由 semantic_test.py 的 `unittest discover -s tests/semantic` 在每轮门禁内收集执行，
因此必须保持毫秒级：这里只验证「缓存键的完整性/敏感性 + 命中/写回逻辑」，不触发推理。
真正的两遍实测（首轮全量→次轮命中）见 kb.py check 端到端验证，不放进门禁内单测。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import semantic_validate as sv  # noqa: E402


class GateCacheKeyTest(unittest.TestCase):
    def setUp(self) -> None:
        self._root = sv.ROOT
        self._state = sv._CACHE_STATE_PATH
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        sv.ROOT = self.root
        sv._CACHE_STATE_PATH = self.root / "build" / "state" / "semantic-gate-cache.json"

    def tearDown(self) -> None:
        sv.ROOT = self._root
        sv._CACHE_STATE_PATH = self._state
        self.tmp.cleanup()

    def _write(self, rel: str, content: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_key_is_deterministic_and_flag_sensitive(self) -> None:
        self._write("ontology/modules/x.ttl", "# a")
        k1 = sv.compute_cache_key(False)
        k2 = sv.compute_cache_key(False)
        self.assertIsNotNone(k1)
        self.assertEqual(k1, k2)  # 相同输入 → 相同键
        self.assertNotEqual(k1, sv.compute_cache_key(True))  # --no-inference 裁决语义不同 → 键必不同

    def test_key_is_reasoner_sensitive(self) -> None:
        # native 与 owlrl 产量/报告不同 → 键必分，否则切换推理器会「该跑不跑」（复用旧裁决）。
        import os
        self._write("ontology/modules/x.ttl", "# a")
        prev = os.environ.get("SEMANTIC_REASONER")
        try:
            os.environ["SEMANTIC_REASONER"] = "owlrl"
            k_owlrl = sv.compute_cache_key(False)
            os.environ["SEMANTIC_REASONER"] = "native"
            k_native = sv.compute_cache_key(False)
            self.assertNotEqual(k_owlrl, k_native)
        finally:
            if prev is None:
                os.environ.pop("SEMANTIC_REASONER", None)
            else:
                os.environ["SEMANTIC_REASONER"] = prev

    def test_key_changes_when_any_input_changes(self) -> None:
        self._write("ontology/modules/x.ttl", "# a")
        base = sv.compute_cache_key(False)
        self._write("ontology/modules/x.ttl", "# a changed")
        self.assertNotEqual(base, sv.compute_cache_key(False))  # 改模块内容 → miss

    def test_key_changes_when_generated_rule_added(self) -> None:
        # 递归 rglob 覆盖 generated/：新增一条自动规则必须改键（否则缓存会漏掉新规则）。
        self._write("ontology/modules/x.ttl", "# a")
        base = sv.compute_cache_key(False)
        self._write("ontology/rules/generated/r999.rq", "CONSTRUCT {} WHERE {}")
        self.assertNotEqual(base, sv.compute_cache_key(False))

    def test_key_changes_when_new_abox_candidate_added(self) -> None:
        # 门禁完整性：新候选一旦落进受哈希的输入（ABox / generated.ttl / current.ttl），
        # 键必变、缓存必然 miss、必然走全量校验——缓存无法掩盖一个新的（可能违规的）候选。
        self._write("knowledge/semantic/current.ttl", "# baseline abox")
        base = sv.compute_cache_key(False)
        self._write("knowledge/semantic/candidate-new.ttl", "@prefix : <urn:pxai:semi:> . :A a :B .")
        self.assertNotEqual(base, sv.compute_cache_key(False))

    def test_kill_switch_bypasses_cache(self, ) -> None:
        import os
        prev = os.environ.get("SEMANTIC_GATE_CACHE")
        try:
            os.environ["SEMANTIC_GATE_CACHE"] = "0"
            self.assertFalse(sv._cache_enabled())
            os.environ["SEMANTIC_GATE_CACHE"] = "1"
            self.assertTrue(sv._cache_enabled())
            del os.environ["SEMANTIC_GATE_CACHE"]
            self.assertTrue(sv._cache_enabled())  # 默认开
        finally:
            if prev is None:
                os.environ.pop("SEMANTIC_GATE_CACHE", None)
            else:
                os.environ["SEMANTIC_GATE_CACHE"] = prev

    def test_write_then_read_roundtrip_only_stores_report_on_pass(self) -> None:
        sv._write_cache("KEY1", "pass", {"status": "pass", "rules": 7})
        stored = sv._read_cache()
        self.assertEqual(stored["key"], "KEY1")
        self.assertEqual(stored["status"], "pass")
        self.assertEqual(stored["report"]["rules"], 7)
        sv._write_cache("KEY2", "fail", None)
        stored = sv._read_cache()
        self.assertEqual(stored["status"], "fail")
        self.assertNotIn("report", stored)  # FAIL 不存报告（只对 PASS 跳过）

    def test_emit_cache_hit_writes_pass_report_preserving_shape(self) -> None:
        rc = sv._emit_cache_hit("KEYABC", {"status": "pass", "rules": 42, "issues": []})
        self.assertEqual(rc, 0)
        report = json.loads((self.root / "build" / "reports" / "semantic-validation.json").read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "pass")
        self.assertTrue(report["cache_hit"])
        self.assertEqual(report["rules"], 42)  # 原报告字段形状保留


if __name__ == "__main__":
    unittest.main()
