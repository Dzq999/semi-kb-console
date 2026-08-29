from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.semantic_pipeline import validate_and_store_agent_output
from app.services.semi_kb import semi_kb
from app.services.orchestrator import _json_object


def candidate(source_ref: str) -> dict:
    return {
        "summary": "测试场景",
        "customer_pains": ["设备状态语义不统一导致排障缓慢"],
        "evidence_notes": ["仅用于结构测试"],
        "semantic_changesets": [{
            "provenance": {"source_type": "web", "confidence": "high", "source_ref": source_ref},
            "additions": {"classes": [{"iri": "urn:pxai:semi:ConsolePipelineTestClass", "label_zh": "控制台测试类", "subclass_of": ["urn:pxai:semi:Equipment"]}]},
        }],
        "simulation_candidates": [],
        "scenario_article_markdown": "场景：设备状态建模。客户痛点：跨系统名称不统一。经营与仿真含义：只用于验证流程，不代表经营承诺。" * 3,
    }


def test_web_candidate_requires_fetched_evidence_url():
    with pytest.raises(ValueError, match="正文证据"):
        validate_and_store_agent_output(candidate("https://invalid.example/a"), run_id="run-test", round_number=1, agent_id="a1", source_mode="web", model_id="gpt-test", evidence=[])


def test_model_prior_is_capped_and_stored():
    raw = candidate("https://invalid.example/a")
    raw["semantic_changesets"][0]["provenance"] = {"source_type": "model_prior", "confidence": "high", "source_ref": "made-up"}
    result = validate_and_store_agent_output(raw, run_id="run-test", round_number=2, agent_id="a1", source_mode="model_prior", model_id="gpt-test", evidence=[])
    document = json.loads(Path(result["semantic_files"][0]).read_text(encoding="utf-8"))
    assert document["provenance"] == {"source_type": "model_prior", "confidence": "medium", "source_ref": "model:gpt-test"}
    assert Path(result["article_file"]).is_file()


def test_json_parser_accepts_trailing_model_text():
    assert _json_object('{"summary":"ok"}\n补充说明') == {"summary": "ok"}


def test_scalar_individual_values_are_normalized_before_schema_validation():
    raw = candidate("unused")
    raw["semantic_changesets"][0]["provenance"] = {"source_type": "model_prior", "confidence": "low", "source_ref": "model:test"}
    additions = raw["semantic_changesets"][0]["additions"]
    additions["datatype_properties"] = [{
        "iri": "urn:pxai:semi:ConsoleScalarValueProperty",
        "label_zh": "控制台标量测试属性",
        "domain": "urn:pxai:semi:Equipment",
        "datatype": "http://www.w3.org/2001/XMLSchema#string",
    }]
    additions["individuals"] = [{
        "iri": "urn:pxai:semi:ConsoleScalarValueInstance",
        "types": "urn:pxai:semi:Equipment",
        "label_zh": "控制台标量测试实例",
        "data": {"urn:pxai:semi:ConsoleScalarValueProperty": "单值"},
    }]
    result = validate_and_store_agent_output(raw, run_id="run-scalar-normalize", round_number=1, agent_id="a1", source_mode="model_prior", model_id="gpt-test", evidence=[])
    document = json.loads(Path(result["semantic_files"][0]).read_text(encoding="utf-8"))
    individual = document["additions"]["individuals"][0]
    assert individual["types"] == ["urn:pxai:semi:Equipment"]
    assert individual["data"]["urn:pxai:semi:ConsoleScalarValueProperty"] == ["单值"]
    assert any("规范化为数组" in warning for warning in result["sanitization_warnings"])


@pytest.mark.asyncio
async def test_valid_candidate_passes_real_engine_without_publication(tmp_path):
    path = tmp_path / "candidate.json"
    document = {
        "id": "scs.console.integration-check",
        "created_at": "2026-08-30",
        "provenance": {"source_type": "model_prior", "confidence": "low", "source_ref": "model:integration-test"},
        "additions": {"classes": [{"iri": "urn:pxai:semi:ConsoleIntegrationCheckClass", "label_zh": "控制台集成检查类", "subclass_of": ["urn:pxai:semi:Equipment"]}]},
    }
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    result = await semi_kb.process_candidates({"semantic": [path], "simulation": [], "articles": []}, publish=False)
    assert result["published"] is False
    assert result["checks"]["semantic_precheck"]["passed"] is True
    assert result["checks"]["business_simulation"]["passed"] is True
    assert not list((semi_kb.root / "semantic_changesets" / "pending").glob("console-*-candidate-*.json"))
