from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.semi_kb import semi_kb

from app.services.semantic_pipeline import validate_and_store_agent_output


def test_cross_reference_cleanup_keeps_valid_class_and_drops_dangling_property() -> None:
    raw = {
        "summary": "设备异常知识需要统一建模，便于跨系统定位和复盘。",
        "customer_pains": ["同一异常在不同系统中使用不同名称，导致排查耗时。"],
        "semantic_changesets": [{
            "provenance": {"source_type": "model_prior", "confidence": "low", "source_ref": "model:test"},
            "additions": {
                "classes": [{"iri": "urn:pxai:semi:ConsoleValidEnrichmentClass", "label_zh": "验证类", "subclass_of": ["urn:pxai:semi:Equipment"]}],
                "datatype_properties": [{"iri": "urn:pxai:semi:ConsoleDanglingProperty", "label_zh": "悬空属性", "domain": ["urn:pxai:semi:ClassFromAnotherAgent"], "datatype": "http://www.w3.org/2001/XMLSchema#string"}],
            },
        }],
        "scenario_article_markdown": "场景：设备异常知识。客户痛点：名称不一致。经营与仿真含义：仅用于自动校验，不构成经营承诺。",
    }
    result = validate_and_store_agent_output(raw, run_id="run-enrichment-test", round_number=1, agent_id="agent-1", source_mode="model_prior", model_id="gpt-test", evidence=[])
    document = json.loads(Path(result["semantic_files"][0]).read_text(encoding="utf-8"))
    assert len(document["additions"]["classes"]) == 1
    assert "datatype_properties" not in document["additions"]
    assert any("domain未声明" in warning for warning in result["sanitization_warnings"])


def test_empty_domain_property_is_dropped_not_fatal() -> None:
    # 复现第 2 轮 gate 失败：模型对新建对象属性给出 domain: []（schema minItems:1）。
    # sanitizer 必须在严格校验前丢弃该属性并保住候选其余部分，而非让整份输出报错。
    raw = {
        "summary": "设备与腔室之间的从属关系需要统一建模，便于跨系统定位。",
        "customer_pains": ["腔室归属在不同系统中口径不一，排查耗时。"],
        "semantic_changesets": [{
            "provenance": {"source_type": "model_prior", "confidence": "low", "source_ref": "model:test"},
            "additions": {
                "classes": [{"iri": "urn:pxai:semi:ConsoleEmptyDomainProbeClass", "label_zh": "空域探针类", "subclass_of": ["urn:pxai:semi:Equipment"]}],
                "object_properties": [
                    {"iri": "urn:pxai:semi:consoleEmptyDomainProp", "label_zh": "空域属性", "domain": [], "range": ["urn:pxai:semi:Equipment"]},
                    {"iri": "urn:pxai:semi:consoleEmptyRangeProp", "label_zh": "空值域属性", "domain": ["urn:pxai:semi:Equipment"], "range": []},
                ],
            },
        }],
        "scenario_article_markdown": "场景：腔室归属建模。客户痛点：口径不一。经营与仿真含义：仅用于自动校验，不构成经营承诺。",
    }
    result = validate_and_store_agent_output(raw, run_id="run-empty-domain-test", round_number=1, agent_id="agent-1", source_mode="model_prior", model_id="gpt-test", evidence=[])
    document = json.loads(Path(result["semantic_files"][0]).read_text(encoding="utf-8"))
    assert len(document["additions"]["classes"]) == 1
    assert "object_properties" not in document["additions"]
    assert any("domain 缺失或为空" in warning for warning in result["sanitization_warnings"])
    assert any("range 缺失或为空" in warning for warning in result["sanitization_warnings"])


def test_inverse_of_kept_when_paired_and_dropped_when_dangling() -> None:
    # 让公理计数能增长：本批次两个对象属性互为反向，inverse_of 应保留（顺序无关）；
    # 指向不存在属性的 inverse_of 是悬空引用，应被剥掉但不牵连属性本身。
    raw = {
        "summary": "腔室与设备的包含关系需要成对建模，便于双向推理。",
        "customer_pains": ["只记录单向包含关系，反向查询时需要额外拼接。"],
        "semantic_changesets": [{
            "provenance": {"source_type": "model_prior", "confidence": "low", "source_ref": "model:test"},
            "additions": {
                "object_properties": [
                    {"iri": "urn:pxai:semi:consoleContainsChamberX", "label_zh": "包含腔室", "domain": ["urn:pxai:semi:Equipment"], "range": ["urn:pxai:semi:Chamber"], "inverse_of": "urn:pxai:semi:consoleChamberOfX"},
                    {"iri": "urn:pxai:semi:consoleChamberOfX", "label_zh": "腔室归属", "domain": ["urn:pxai:semi:Chamber"], "range": ["urn:pxai:semi:Equipment"]},
                    {"iri": "urn:pxai:semi:consoleDanglingInverseX", "label_zh": "悬空反向属性", "domain": ["urn:pxai:semi:Equipment"], "range": ["urn:pxai:semi:Chamber"], "inverse_of": "urn:pxai:semi:consoleNeverDeclaredProp"},
                ],
            },
        }],
        "scenario_article_markdown": "场景：包含关系成对建模。客户痛点：单向记录。经营与仿真含义：仅用于自动校验，不构成经营承诺。",
    }
    result = validate_and_store_agent_output(raw, run_id="run-inverse-test", round_number=1, agent_id="agent-1", source_mode="model_prior", model_id="gpt-test", evidence=[])
    document = json.loads(Path(result["semantic_files"][0]).read_text(encoding="utf-8"))
    props = {item["iri"]: item for item in document["additions"]["object_properties"]}
    assert len(props) == 3  # 三条属性都保住，没有因悬空 inverse 被丢弃
    assert props["urn:pxai:semi:consoleContainsChamberX"]["inverse_of"] == "urn:pxai:semi:consoleChamberOfX"
    assert "inverse_of" not in props["urn:pxai:semi:consoleDanglingInverseX"]  # 悬空引用被剥掉
    assert any("悬空 inverse_of" in warning for warning in result["sanitization_warnings"])


def test_knowledge_and_rule_candidates_are_normalized() -> None:
    raw = {
        "summary": "设备停机事件与维护响应之间存在可验证的时间约束。",
        "customer_pains": ["维护响应时间缺少统一口径，影响交付风险评估。"],
        "semantic_changesets": [],
        "knowledge_entries": [{"entry": {"id": "urn:pxai:semi:knowledge:enrichment-test", "title": "维护响应知识", "domain": "eqp", "summary": "响应时间约束", "content": "设备停机后维护响应时间需要统一记录，并与交付风险评估关联。", "source_refs": [], "related_iris": [], "confidence": "high"}}],
        "rule_candidates": [{"rule": {"rule_id": "R-AUTO-ENRICHMENT-001", "name": "响应时间风险提示", "purpose": "为后续规则测试提供结构化候选", "implementation": "sparql", "query": "CONSTRUCT { ?event <urn:pxai:semi:hasRisk> ?risk } WHERE { ?event a <urn:pxai:semi:EquipmentDowntimeEvent> . }", "preconditions": ["事件类型已声明"], "conclusion": "生成风险提示候选", "required_sources": ["equipment_event"], "tests": [], "confidence": "medium"}}],
        "scenario_article_markdown": "场景：设备停机。客户痛点：维护响应时间不统一。经营与仿真含义：只用于校验。",
    }
    result = validate_and_store_agent_output(raw, run_id="run-enrichment-test", round_number=2, agent_id="agent-1", source_mode="model_prior", model_id="gpt-test", evidence=[])
    assert len(result["knowledge_files"]) == 1
    assert len(result["rule_files"]) == 1


@pytest.mark.asyncio
async def test_bad_semantic_candidate_is_quarantined_without_blocking_valid_one(tmp_path: Path) -> None:
    valid = tmp_path / "valid.json"
    valid.write_text(json.dumps({
        "id": "scs.console.enrichment-valid", "created_at": "2026-08-30",
        "provenance": {"source_type": "model_prior", "confidence": "low", "source_ref": "model:test"},
        "additions": {"classes": [{"iri": "urn:pxai:semi:ConsoleEnrichmentValidClass", "label_zh": "有效候选类", "subclass_of": ["urn:pxai:semi:Equipment"]}]},
    }), encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({
        "id": "scs.console.enrichment-bad", "created_at": "2026-08-30",
        "provenance": {"source_type": "model_prior", "confidence": "low", "source_ref": "model:test"},
        "additions": {"datatype_properties": [{"iri": "urn:pxai:semi:ConsoleEnrichmentBadProperty", "label_zh": "坏属性", "domain": ["urn:pxai:semi:DoesNotExist"], "datatype": "http://www.w3.org/2001/XMLSchema#string"}]},
    }), encoding="utf-8")
    result = await semi_kb.process_candidates({"semantic": [bad, valid], "business": [], "simulation": [], "knowledge": [], "rules": [], "articles": []}, publish=False)
    assert result["semantic_candidates"] == 2
    assert any(item["path"] == str(bad) for item in result["quarantined_candidates"])
    assert result["checks"]["semantic_precheck"]["passed"] is True


def _isolated_adapter(tmp_path: Path):
    """构造一个指向临时引擎根、且所有子进程都被打桩的 SemiKbAdapter。

    不接触共享的 data/engine，也不真正跑脚本——用于断言整图预检的调用次数。
    """
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


def _one_semantic_candidate(tmp_path: Path) -> Path:
    source = tmp_path / "cand.json"
    source.write_text(json.dumps({
        "id": "scs.console.dedup-probe", "created_at": "2026-09-04",
        "provenance": {"source_type": "model_prior", "confidence": "low", "source_ref": "model:test"},
        "additions": {"classes": [{"iri": "urn:pxai:semi:ConsoleDedupProbeClass", "label_zh": "去重探针类", "subclass_of": ["urn:pxai:semi:Equipment"]}]},
    }), encoding="utf-8")
    return source


@pytest.mark.asyncio
async def test_passing_batch_runs_semantic_precheck_only_once(tmp_path: Path) -> None:
    """Opt1：整批预检通过、未发生隔离时，646 行的语义预检被复用而非重跑。

    改造前:同一整批要跑 541 与 646 两遍整图 --check;改造后只剩 541 那一遍。
    """
    adapter, calls = _isolated_adapter(tmp_path)
    source = _one_semantic_candidate(tmp_path)
    result = await adapter.process_candidates(
        {"semantic": [source], "business": [], "simulation": [], "knowledge": [], "rules": [], "articles": []},
        publish=True,
    )
    checks = [c for c in calls if c == ("apply_semantic_changeset.py", "--check")]
    applies = [c for c in calls if c == ("apply_semantic_changeset.py",)]
    assert len(checks) == 1, f"整批通过应只剩一遍预检，实际 {calls}"
    assert len(applies) == 1, "权威 apply 门禁必须照常执行"
    assert result["checks"]["semantic_precheck"]["passed"] is True
    assert "复用" in result["checks"]["semantic_precheck"]["output"]


@pytest.mark.asyncio
async def test_prechecked_batch_skips_both_prechecks_but_keeps_apply(tmp_path: Path) -> None:
    """Opt2：调用方(partial_publish)已整批预检时，process_candidates 跳过全部预检，只留 apply。"""
    adapter, calls = _isolated_adapter(tmp_path)
    source = _one_semantic_candidate(tmp_path)
    result = await adapter.process_candidates(
        {"semantic": [source], "business": [], "simulation": [], "knowledge": [], "rules": [], "articles": []},
        publish=True,
        semantic_batch_prechecked=True,
    )
    checks = [c for c in calls if c == ("apply_semantic_changeset.py", "--check")]
    applies = [c for c in calls if c == ("apply_semantic_changeset.py",)]
    assert len(checks) == 0, f"已预检批次不应再跑任何整图预检，实际 {calls}"
    assert len(applies) == 1, "权威 apply 门禁仍必须执行"
    assert result["checks"]["semantic_batch_precheck"]["passed"] is True
