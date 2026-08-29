from __future__ import annotations

from fastapi.testclient import TestClient

from app.services.llm import llm_service
from app.services.orchestrator import orchestrator


def test_setup_and_session(client: TestClient):
    assert client.get("/api/auth/status").json() == {"setup_required": True}
    response = client.post("/api/auth/setup", json={"username": "admin", "password": "strong-password"})
    assert response.status_code == 201
    me = client.get("/api/users/me")
    assert me.status_code == 200
    assert me.json()["preferences"]["default_agent_count"] == 6


def test_credentials_are_masked(authenticated: TestClient):
    response = authenticated.put("/api/credentials", json={"kind": "llm_api_key", "value": "secret-value-123"})
    assert response.status_code == 200
    body = authenticated.get("/api/credentials").json()
    assert body["items"][0]["configured"] is True
    assert "secret-value-123" not in str(body)


def test_run_keeps_each_agent_source(authenticated: TestClient, monkeypatch):
    authenticated.put("/api/credentials", json={"kind": "llm_api_key", "value": "test-key"})

    async def fake_models(_key, search=""):
        return [{"id": "gpt-test", "available": True}]

    monkeypatch.setattr(llm_service, "list_models", fake_models)
    monkeypatch.setattr(orchestrator, "start", lambda _run_id: None)
    response = authenticated.post("/api/runs", json={
        "model_id": "gpt-test",
        "agents": [
            {"name": "Fab", "role": "research", "domain": "fab", "objective": "Fab coverage", "source_mode": "web"},
            {"name": "EQP", "role": "research", "domain": "eqp", "objective": "EQP coverage", "source_mode": "model_prior"}
        ]
    })
    assert response.status_code == 202
    assert [item["source_mode"] for item in response.json()["agents"]] == ["web", "model_prior"]


def test_agent_limit_is_enforced(authenticated: TestClient):
    agents = [{"name": f"A{i}", "role": "r", "domain": "fab", "objective": "coverage", "source_mode": "web"} for i in range(11)]
    response = authenticated.post("/api/runs", json={"model_id": "x", "agents": agents})
    assert response.status_code == 422

