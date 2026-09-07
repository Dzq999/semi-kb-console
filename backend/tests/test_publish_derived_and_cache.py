"""Part A/B 的后端可达测试（`cd backend && pytest tests` 直接覆盖）。

- Part A：`build_check_chain()` 的门禁子集不含 build_index/scenario_mine、仍含 regress
  等一致性判定；发布成功后 `kb.py refresh-derived` 被调用且**不参与** PASS/FAIL（失败也
  不把发布翻成失败），语义发布路径由引擎内刷新、后端不再重复调用；配置关时不调用。
- Part B：`compute_cache_key()` 完整性/敏感性/kill-switch（引擎内还有一份 in-gate 单测
  见 data/engine/tests/semantic/test_gate_cache.py，此处保证后端主测试套也覆盖）。

引擎脚本按仓库既有约定用 sys.path 引入；缺依赖时跳过而非报错。
"""
from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ENGINE_SCRIPTS = _PROJECT_ROOT / "data" / "engine" / "scripts"


def _load_engine(mod_name: str):
    if str(_ENGINE_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_ENGINE_SCRIPTS))
    try:
        return importlib.import_module(mod_name)
    except SystemExit as exc:  # kb.py 在缺 common 依赖时 raise SystemExit(2)
        pytest.skip(f"引擎依赖缺失，跳过：{exc}")
    except ModuleNotFoundError as exc:  # noqa: PERF203
        pytest.skip(f"引擎依赖缺失，跳过：{exc.name}")


# --------------------------------------------------------------------------- #
# Part A：build_check_chain 门禁子集
# --------------------------------------------------------------------------- #
def test_gate_subset_excludes_derived_keeps_consistency_steps() -> None:
    kb = _load_engine("kb")
    gate = kb.build_check_chain(has_pending=True, skip_derived=True)
    assert "build_index.py" not in gate  # 派生物移出关键路径
    assert "scenario_mine.py" not in gate
    for keep in ("validate.py", "migrate_semantic.py", "semantic_validate.py",
                 "semantic_test.py", "simulate_check.py", "regress.py"):
        assert keep in gate, f"门禁必须保留一致性判定步骤 {keep}"
    assert gate[0] == "precheck.py"  # has_pending=True → 链首为 precheck


def test_full_chain_includes_derived_and_orders_before_migrate() -> None:
    kb = _load_engine("kb")
    full = kb.build_check_chain(has_pending=False, skip_derived=False)
    assert "build_index.py" in full and "scenario_mine.py" in full
    assert "precheck.py" not in full  # 无 pending 不跑 precheck
    # 派生物必须在 migrate/semantic 之前（validate 先于 build_index 的固化顺序）
    assert full.index("build_index.py") < full.index("migrate_semantic.py")
    assert full.index("scenario_mine.py") < full.index("semantic_validate.py")


def test_refresh_derived_command_covers_derived_plus_migrate() -> None:
    kb = _load_engine("kb")
    assert kb.DERIVED_STEPS == ["build_index.py", "scenario_mine.py"]


# --------------------------------------------------------------------------- #
# Part B：基线哈希缓存键
# --------------------------------------------------------------------------- #
def test_cache_key_deterministic_and_flag_sensitive(tmp_path, monkeypatch) -> None:
    sv = _load_engine("semantic_validate")
    monkeypatch.setattr(sv, "ROOT", tmp_path)
    (tmp_path / "ontology" / "modules").mkdir(parents=True)
    (tmp_path / "ontology" / "modules" / "m.ttl").write_text("# x", encoding="utf-8")
    key = sv.compute_cache_key(False)
    assert key is not None
    assert key == sv.compute_cache_key(False)
    assert key != sv.compute_cache_key(True)  # --no-inference 裁决语义不同 → 键必不同


def test_cache_key_changes_on_input_and_recursive_rule(tmp_path, monkeypatch) -> None:
    sv = _load_engine("semantic_validate")
    monkeypatch.setattr(sv, "ROOT", tmp_path)
    modules = tmp_path / "ontology" / "modules"
    modules.mkdir(parents=True)
    module = modules / "m.ttl"
    module.write_text("# a", encoding="utf-8")
    base = sv.compute_cache_key(False)
    module.write_text("# b", encoding="utf-8")
    assert sv.compute_cache_key(False) != base  # 改模块内容 → miss

    base2 = sv.compute_cache_key(False)
    rules = tmp_path / "ontology" / "rules" / "generated"
    rules.mkdir(parents=True)
    (rules / "r1.rq").write_text("CONSTRUCT {} WHERE {}", encoding="utf-8")
    assert sv.compute_cache_key(False) != base2  # 递归 rglob 抓到新增 generated 规则 → miss


def test_cache_key_changes_on_new_abox_candidate(tmp_path, monkeypatch) -> None:
    # 门禁完整性：新候选一旦落进受哈希输入（ABox），键必变、缓存必然 miss、必走全量。
    sv = _load_engine("semantic_validate")
    monkeypatch.setattr(sv, "ROOT", tmp_path)
    abox = tmp_path / "knowledge" / "semantic"
    abox.mkdir(parents=True)
    (abox / "current.ttl").write_text("# baseline", encoding="utf-8")
    base = sv.compute_cache_key(False)
    (abox / "candidate.ttl").write_text("@prefix : <urn:pxai:semi:> . :A a :B .", encoding="utf-8")
    assert sv.compute_cache_key(False) != base


def test_cache_kill_switch(monkeypatch) -> None:
    sv = _load_engine("semantic_validate")
    monkeypatch.setenv("SEMANTIC_GATE_CACHE", "0")
    assert sv._cache_enabled() is False
    monkeypatch.setenv("SEMANTIC_GATE_CACHE", "1")
    assert sv._cache_enabled() is True
    monkeypatch.delenv("SEMANTIC_GATE_CACHE", raising=False)
    assert sv._cache_enabled() is True  # 默认开


# --------------------------------------------------------------------------- #
# Part A：发布后 refresh-derived 行为（best-effort、非门禁）
# --------------------------------------------------------------------------- #
def _stub_adapter(tmp_path: Path):
    from app.services.semi_kb import SemiKbAdapter

    (tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "scripts" / "kb.py").write_text("# stub\n", encoding="utf-8")
    adapter = SemiKbAdapter(root=tmp_path)
    calls: list[tuple[str, ...]] = []

    async def fake_command(*args: str, timeout: int = 1800) -> dict:
        calls.append(tuple(args))
        return {"exit_code": 0, "duration_seconds": 0.0, "output": "{}"}

    adapter.command = fake_command  # type: ignore[assignment]
    return adapter, calls


def _empty_batch() -> dict:
    return {"semantic": [], "business": [], "simulation": [], "knowledge": [], "rules": [], "articles": []}


def _one_semantic_candidate(tmp_path: Path) -> Path:
    source = tmp_path / "cand.json"
    source.write_text(json.dumps({
        "id": "scs.console.derived-probe", "created_at": "2026-09-06",
        "provenance": {"source_type": "model_prior", "confidence": "low", "source_ref": "model:test"},
        "additions": {"classes": [{"iri": "urn:pxai:semi:ConsoleDerivedProbeClass", "label_zh": "派生探针类", "subclass_of": ["urn:pxai:semi:Equipment"]}]},
    }), encoding="utf-8")
    return source


@pytest.mark.asyncio
async def test_article_only_publish_uses_gate_subset_then_refreshes(tmp_path: Path) -> None:
    adapter, calls = _stub_adapter(tmp_path)
    article = tmp_path / "article.md"
    article.write_text("# 场景", encoding="utf-8")
    batch = _empty_batch()
    batch["articles"] = [article]
    result = await adapter.process_candidates(batch, publish=True)
    assert result["published"] is True
    assert ("kb.py", "check", "--defer-derived") in calls  # 发布基线走门禁子集（跳派生物）
    assert ("kb.py", "refresh-derived") in calls  # 发布成功后补跑派生物
    assert result["checks"]["refresh_derived"]["passed"] is True


@pytest.mark.asyncio
async def test_refresh_derived_failure_never_fails_publish(tmp_path: Path) -> None:
    adapter, calls = _stub_adapter(tmp_path)

    async def fake_command(*args: str, timeout: int = 1800) -> dict:
        calls.append(tuple(args))
        if args[:2] == ("kb.py", "refresh-derived"):
            return {"exit_code": 1, "duration_seconds": 0.0, "output": "刷新失败"}
        return {"exit_code": 0, "duration_seconds": 0.0, "output": "{}"}

    adapter.command = fake_command  # type: ignore[assignment]
    article = tmp_path / "article.md"
    article.write_text("# 场景", encoding="utf-8")
    batch = _empty_batch()
    batch["articles"] = [article]
    result = await adapter.process_candidates(batch, publish=True)
    assert result["published"] is True  # refresh 失败绝不把已发布翻成失败
    assert result["checks"]["refresh_derived"]["passed"] is False  # 但如实记录


@pytest.mark.asyncio
async def test_semantic_publish_does_not_double_refresh(tmp_path: Path) -> None:
    adapter, calls = _stub_adapter(tmp_path)
    batch = _empty_batch()
    batch["semantic"] = [_one_semantic_candidate(tmp_path)]
    result = await adapter.process_candidates(batch, publish=True)
    assert result["published"] is True
    # 语义路径由 apply_semantic_changeset.py 在引擎内刷新，后端不得重复调用
    assert ("kb.py", "refresh-derived") not in calls


@pytest.mark.asyncio
async def test_refresh_derived_respects_config_off(tmp_path: Path, monkeypatch) -> None:
    import app.services.semi_kb as mod

    adapter, calls = _stub_adapter(tmp_path)
    monkeypatch.setattr(mod, "settings", types.SimpleNamespace(refresh_derived_after_publish=False))
    result: dict = {"checks": {}}
    await adapter._refresh_derived_after_publish(result)
    assert result["checks"]["refresh_derived"]["skipped"] is True
    assert ("kb.py", "refresh-derived") not in calls  # 配置关闭 → 完全不触发
