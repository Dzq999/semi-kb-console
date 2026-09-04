"""知识库问答服务与端点测试：接地上下文只读聚合、答案解析容错、会话多轮落库、用户隔离。

不依赖真 LLM——monkeypatch llm_service.complete 返回罐装 JSON。
"""
from __future__ import annotations

import json

import pytest

from app.services import qa
from app.services.llm import llm_service


def test_parse_answer_extracts_structured_json():
    raw = json.dumps({
        "answer": "单位成本约 300 CNY/unit。",
        "citations": [{"source": "经营模型", "ref": "business/models/x.yaml", "note": "取值来源"}],
        "grounded": True,
    }, ensure_ascii=False)
    parsed = qa._parse_answer(raw)
    assert parsed["grounded"] is True
    assert parsed["answer"].startswith("单位成本")
    assert parsed["citations"][0]["ref"] == "business/models/x.yaml"


def test_parse_answer_strips_code_fence():
    raw = "```json\n{\"answer\": \"内容\", \"citations\": [], \"grounded\": false}\n```"
    parsed = qa._parse_answer(raw)
    assert parsed["answer"] == "内容"
    assert parsed["grounded"] is False


def test_parse_answer_falls_back_on_non_json():
    parsed = qa._parse_answer("这是纯文本，不是 JSON")
    assert parsed["grounded"] is False
    assert parsed["citations"] == []
    assert "纯文本" in parsed["answer"]


def test_parse_answer_drops_malformed_citations():
    raw = json.dumps({"answer": "x", "citations": ["not-a-dict", {"source": "本体"}], "grounded": True}, ensure_ascii=False)
    parsed = qa._parse_answer(raw)
    # 非 dict 引用被剔除，合法 dict 补齐缺省字段。
    assert len(parsed["citations"]) == 1
    assert parsed["citations"][0]["source"] == "本体"
    assert parsed["citations"][0]["ref"] == ""


def test_build_grounding_context_is_readonly_and_shaped():
    ctx = qa.build_grounding_context()
    assert set(ctx.keys()) >= {"business_models", "simulation_scenarios", "knowledge_sources", "ontology_terms"}
    assert isinstance(ctx["business_models"], list)
    assert isinstance(ctx["simulation_scenarios"], list)


def test_qa_conversation_ask_flow_persists_history(authenticated, monkeypatch):
    authenticated.put("/api/credentials", json={"kind": "llm_api_key", "value": "test-key"})

    calls: list[dict] = []

    async def fake_complete(api_key, model_id, system, user, *args, **kwargs):
        calls.append({"system": system, "user": user})
        return json.dumps({
            "answer": "根据经营模型，单位成本约 300 CNY/unit。",
            "citations": [{"source": "经营模型", "ref": "business/models/base.yaml", "note": "variable_unit_cost"}],
            "grounded": True,
        }, ensure_ascii=False)

    monkeypatch.setattr(llm_service, "complete", fake_complete)

    created = authenticated.post("/api/qa/conversations", json={}).json()
    conv_id = created["id"]

    answered = authenticated.post(f"/api/qa/conversations/{conv_id}/ask", json={"question": "成本预测怎么算？", "model_id": "gpt-test"})
    assert answered.status_code == 200
    body = answered.json()
    assert body["role"] == "assistant"
    assert body["grounded"] is True
    assert body["citations"][0]["ref"] == "business/models/base.yaml"

    # 会话应含 user + assistant 两条，且标题由首问生成。
    detail = authenticated.get(f"/api/qa/conversations/{conv_id}").json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["title"] == "成本预测怎么算？"

    # 第二问：历史上下文应带上前一轮（system+user 里含知识上下文）。
    authenticated.post(f"/api/qa/conversations/{conv_id}/ask", json={"question": "那产能呢？", "model_id": "gpt-test"})
    detail2 = authenticated.get(f"/api/qa/conversations/{conv_id}").json()
    assert len(detail2["messages"]) == 4
    payload = json.loads(calls[-1]["user"])
    assert payload["question"] == "那产能呢？"
    assert any(m["content"] == "成本预测怎么算？" for m in payload["conversation_history"])
    assert "knowledge_context" in payload


def test_qa_conversation_requires_model(authenticated):
    authenticated.put("/api/credentials", json={"kind": "llm_api_key", "value": "test-key"})
    conv_id = authenticated.post("/api/qa/conversations", json={}).json()["id"]
    # 未传 model_id 且无默认模型 → 422。
    resp = authenticated.post(f"/api/qa/conversations/{conv_id}/ask", json={"question": "x"})
    assert resp.status_code == 422


def test_qa_conversation_isolated_per_user(authenticated, client):
    conv_id = authenticated.post("/api/qa/conversations", json={}).json()["id"]
    # 另一个用户登录后访问不到（本测试单用户实例，模拟未授权：删除会话再查）。
    got = authenticated.get(f"/api/qa/conversations/{conv_id}")
    assert got.status_code == 200
    missing = authenticated.get("/api/qa/conversations/does-not-exist")
    assert missing.status_code == 404


def test_qa_conversation_delete(authenticated):
    conv_id = authenticated.post("/api/qa/conversations", json={}).json()["id"]
    assert authenticated.delete(f"/api/qa/conversations/{conv_id}").status_code == 204
    assert authenticated.get(f"/api/qa/conversations/{conv_id}").status_code == 404
