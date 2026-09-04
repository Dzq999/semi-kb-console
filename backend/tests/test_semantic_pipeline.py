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


# 用户复查发现的不合格场景原文（把知识库自建工程机制当客户痛点）。
_SELF_REFERENTIAL_ARTICLE = (
    "# 质量审查第3轮反向特征缺口收敛场景\n\n"
    "## 业务场景\n将前两轮已发布的8项源特征逐字重映射到既有属性，使映射覆盖从38.2%生效。\n\n"
    "## 客户痛点\n"
    "- 映射覆盖未生效：源特征目录显示未映射，覆盖率卡在38.2%，需逐字重映射使覆盖率生效。\n"
    "- 质量域分类编码缺乏本体属性承载，无法核对配置完整性。\n\n"
    "## 经营影响\n映射覆盖率停滞，前两轮已发布属性未生效，影响跨系统根因分析效率。\n\n"
    "## 证据边界\n来源类型 model_prior，置信度 medium。\n\n"
    "## 仿真含义\n本轮不产出仿真候选情景。\n"
)


def test_self_referential_scenario_is_rejected():
    raw = candidate("unused")
    raw["semantic_changesets"][0]["provenance"] = {"source_type": "model_prior", "confidence": "low", "source_ref": "model:test"}
    raw["summary"] = "将前两轮已发布属性逐字重映射使映射覆盖生效"
    raw["customer_pains"] = ["映射覆盖未生效，覆盖率卡在38.2%", "缺乏本体属性承载，无法与本体对齐"]
    raw["scenario_article_markdown"] = _SELF_REFERENTIAL_ARTICLE
    with pytest.raises(ValueError, match="产线现场客户问题"):
        validate_and_store_agent_output(raw, run_id="run-selfref", round_number=1, agent_id="a1", source_mode="model_prior", model_id="gpt-test", evidence=[])


def test_real_fabfloor_scenario_passes_guardrail():
    """真实产线痛点即使在证据/仿真段提到本体/映射也应放行。"""
    raw = candidate("unused")
    raw["semantic_changesets"][0]["provenance"] = {"source_type": "model_prior", "confidence": "low", "source_ref": "model:test"}
    raw["summary"] = "设备非计划停机导致光刻瓶颈产能损失"
    raw["customer_pains"] = ["工程师需跨MES/SPC/维护日志手工交叉匹配数据，根因定位缓慢", "设备故障后才响应，扩大停机与生产扰动"]
    raw["scenario_article_markdown"] = (
        "# 预测性维护降低非计划停机\n\n"
        "## 场景\n设备工程团队用时序传感数据做预测性维护与故障检测。\n\n"
        "## 客户痛点\n复杂异常涉及设备、配方、工装与物料交互，单一日志不足以形成完整因果链。\n\n"
        "## 影响\n可将非计划停机转化为可规划维护窗口，恢复光刻瓶颈产能。\n\n"
        "## 证据边界\n本轮结论为model_prior，未引用现场实测数值。\n\n"
        "## 仿真含义\n仅引用fab-baseline合法变量lithography_capacity，映射覆盖等建库口径不写入客户叙事。\n"
    )
    result = validate_and_store_agent_output(raw, run_id="run-realfab", round_number=1, agent_id="a1", source_mode="model_prior", model_id="gpt-test", evidence=[])
    assert Path(result["article_file"]).is_file()


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
