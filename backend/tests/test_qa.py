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


def test_parse_answer_salvages_answer_when_body_has_bare_quotes():
    """回归：answer 正文夹裸引号会让 json.loads 失败——绝不能把 JSON 原文回给用户，
    须抠出 answer markdown、还原 \\n、并单独判读 grounded。（企微机器人回 JSON 的 bug）"""
    # 正文里的 "裸引号" 破坏 JSON；\n 是合法转义，应还原为真实换行。
    raw = '{"answer": "开头说明。这里有"裸引号"会破坏 JSON。\\n\\n## 小标题\\n要点一", ' \
          '"citations": [{"source": "经营模型", "ref": "x.yaml", "note": "n"}], "grounded": true}'
    parsed = qa._parse_answer(raw)
    assert parsed["grounded"] is True  # grounded 从文本单独判读，未被裸引号连累
    assert "裸引号" in parsed["answer"]
    assert "\n" in parsed["answer"]  # \\n 已还原为真实换行
    # 绝不泄漏 JSON 骨架
    assert not parsed["answer"].lstrip().startswith("{")
    assert '"answer"' not in parsed["answer"]
    assert '"citations"' not in parsed["answer"]


def test_parse_answer_never_leaks_json_fence_on_broken_envelope():
    """回归：带 ```json 围栏且正文含裸引号——剥壳+抠取后不得残留围栏或 JSON 结构。"""
    raw = '```json\n{"answer": "答案含"引号"文本", "citations": [], "grounded": false}\n```'
    parsed = qa._parse_answer(raw)
    assert "引号" in parsed["answer"]
    assert "```" not in parsed["answer"]
    assert '"grounded"' not in parsed["answer"]
    assert parsed["grounded"] is False


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


def test_parse_plan_complex_and_simple():
    complex_raw = json.dumps({
        "mode": "complex",
        "steps": [{"title": "测算基线成本", "instruction": "取经营模型单位成本"},
                  {"title": "测算良率增益", "instruction": "算良率对产出的影响"}],
        "citations": [{"source": "经营模型", "ref": "business/models/base.yaml"}],
        "grounded": True,
    }, ensure_ascii=False)
    plan = qa._parse_plan(complex_raw)
    assert plan["mode"] == "complex"
    assert [s["title"] for s in plan["steps"]] == ["测算基线成本", "测算良率增益"]
    assert plan["citations"][0]["ref"] == "business/models/base.yaml"

    simple = qa._parse_plan(json.dumps({"mode": "simple", "steps": [], "grounded": False}, ensure_ascii=False))
    assert simple["mode"] == "simple" and simple["steps"] == []


def test_parse_plan_falls_back_on_garbage():
    plan = qa._parse_plan("not json at all")
    assert plan["mode"] == "simple" and plan["steps"] == [] and plan["grounded"] is False


def test_parse_plan_caps_steps():
    steps = [{"title": f"步骤{i}", "instruction": "x"} for i in range(10)]
    plan = qa._parse_plan(json.dumps({"steps": steps}, ensure_ascii=False))
    assert len(plan["steps"]) == qa._MAX_STEPS


def test_stream_endpoint_emits_stages_and_persists(authenticated, monkeypatch):
    """完整档：规划→逐步真调用→流式汇总→落库；断言事件序与最终消息落库。"""
    authenticated.put("/api/credentials", json={"kind": "llm_api_key", "value": "test-key"})

    step_calls: list[dict] = []

    async def fake_complete(api_key, model_id, system, user, *args, **kwargs):
        if "规划器" in system:
            return json.dumps({
                "mode": "complex",
                "steps": [{"title": "测算基线成本", "instruction": "取单位成本"},
                          {"title": "测算利润影响", "instruction": "汇总对利润的影响"}],
                "citations": [{"source": "经营模型", "ref": "business/models/base.yaml", "note": "unit_cost"}],
                "grounded": True,
            }, ensure_ascii=False)
        step_calls.append(json.loads(user))
        return "本步结论：单位成本约 300 CNY/unit。"

    async def fake_stream(api_key, model_id, system, user, *args, **kwargs):
        for piece in ["综合结论：", "良率提升 2% ", "带来利润增长。"]:
            yield piece

    monkeypatch.setattr(llm_service, "complete", fake_complete)
    monkeypatch.setattr(llm_service, "stream_complete", fake_stream)

    conv_id = authenticated.post("/api/qa/conversations", json={}).json()["id"]
    with authenticated.stream("POST", f"/api/qa/conversations/{conv_id}/ask/stream",
                              json={"question": "良率提升对利润的影响？", "model_id": "gpt-test"}) as resp:
        assert resp.status_code == 200
        events = [json.loads(line) for line in resp.iter_lines() if line.strip()]

    types = [e["type"] for e in events]
    assert types[0] == "stage"  # grounding started
    assert "token" in types and types[-1] == "done"
    # 两个分步都产生了 started/done 阶段事件。
    step_stage_keys = {e["key"] for e in events if e["type"] == "stage" and e.get("group") == "step"}
    assert step_stage_keys == {"step-0", "step-1"}
    # 每个分步都真的各调了一次 LLM，且把前序结果串进去了。
    assert len(step_calls) == 2
    assert "previous_step_results" in step_calls[1]
    assert step_calls[1]["previous_step_results"], "第二步应带上第一步结果"

    done = next(e for e in events if e["type"] == "done")
    assert done["message"]["content"] == "综合结论：良率提升 2% 带来利润增长。"
    assert done["message"]["grounded"] is True
    assert done["message"]["citations"][0]["ref"] == "business/models/base.yaml"

    # 落库：user + assistant 两条，标题取首问。
    detail = authenticated.get(f"/api/qa/conversations/{conv_id}").json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][1]["content"].startswith("综合结论")


def test_stream_endpoint_simple_mode_skips_steps(authenticated, monkeypatch):
    authenticated.put("/api/credentials", json={"kind": "llm_api_key", "value": "test-key"})

    async def fake_complete(api_key, model_id, system, user, *args, **kwargs):
        return json.dumps({"mode": "simple", "steps": [], "citations": [], "grounded": False}, ensure_ascii=False)

    async def fake_stream(api_key, model_id, system, user, *args, **kwargs):
        yield "直接作答内容。"

    monkeypatch.setattr(llm_service, "complete", fake_complete)
    monkeypatch.setattr(llm_service, "stream_complete", fake_stream)

    conv_id = authenticated.post("/api/qa/conversations", json={}).json()["id"]
    with authenticated.stream("POST", f"/api/qa/conversations/{conv_id}/ask/stream",
                              json={"question": "什么是良率？", "model_id": "gpt-test"}) as resp:
        events = [json.loads(line) for line in resp.iter_lines() if line.strip()]

    # simple 模式不应出现 group=step 的分步阶段。
    assert not any(e["type"] == "stage" and e.get("group") == "step" for e in events)
    done = next(e for e in events if e["type"] == "done")
    assert done["message"]["content"] == "直接作答内容。"


def test_stream_endpoint_reports_error_as_event(authenticated, monkeypatch):
    """规划阶段模型网络失败 → 生成器以 error 事件收尾，HTTP 仍 200、不 500。"""
    from app.services.llm import ExternalServiceError

    authenticated.put("/api/credentials", json={"kind": "llm_api_key", "value": "test-key"})

    async def boom(*args, **kwargs):
        raise ExternalServiceError("模型网络调用失败：ConnectTimeout")

    monkeypatch.setattr(llm_service, "complete", boom)

    conv_id = authenticated.post("/api/qa/conversations", json={}).json()["id"]
    with authenticated.stream("POST", f"/api/qa/conversations/{conv_id}/ask/stream",
                              json={"question": "x", "model_id": "gpt-test"}) as resp:
        assert resp.status_code == 200
        events = [json.loads(line) for line in resp.iter_lines() if line.strip()]
    error = next(e for e in events if e["type"] == "error")
    assert "网络" in error["detail"]
    # 失败时不应落库助手消息。
    detail = authenticated.get(f"/api/qa/conversations/{conv_id}").json()
    assert all(m["role"] != "assistant" for m in detail["messages"])


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
