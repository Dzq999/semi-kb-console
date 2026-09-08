from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.models import AgentIteration, Article, Run, RunEvent, RunRound, User
from app.schemas import RunCreate
from app.services.llm import llm_service
from app.services.orchestrator import orchestrator
from app.main import create_run
from app.services.wechat_publisher import wechat_publisher
from app.config import settings


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


def test_llm_endpoint_persists_and_resolves(authenticated: TestClient):
    # 保存自定义端点后，/api/users/me 回读到，且解析器按用户覆盖返回。
    response = authenticated.patch("/api/users/me/preferences/llm-endpoint", json={
        "llm_base_url": "https://proxy.example.com/v1/",
        "model_catalog_url": "https://proxy.example.com/v1/models",
        "llm_api_style": "openai",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["llm_base_url"] == "https://proxy.example.com/v1"  # 末尾斜杠被清洗
    assert body["llm_api_style"] == "openai"
    prefs = authenticated.get("/api/users/me").json()["preferences"]
    assert prefs["model_catalog_url"] == "https://proxy.example.com/v1/models"
    with SessionLocal() as db:
        user = db.query(User).first()
        from app.services.llm import user_llm_endpoint
        resolved = user_llm_endpoint(db, user.id)
    assert resolved.base_url == "https://proxy.example.com/v1"
    assert resolved.api_style == "openai"


def test_llm_endpoint_rejects_non_local_http(authenticated: TestClient):
    # 防 SSRF：非 https 的远端地址应被 schema 校验拒绝（422）。
    response = authenticated.patch("/api/users/me/preferences/llm-endpoint", json={
        "llm_base_url": "http://169.254.169.254/latest",
    })
    assert response.status_code == 422


def test_llm_endpoint_blank_falls_back_to_default(authenticated: TestClient):
    # 留空字段回落到全局默认，不残留旧覆盖值。
    authenticated.patch("/api/users/me/preferences/llm-endpoint", json={"llm_base_url": "https://proxy.example.com/v1"})
    authenticated.patch("/api/users/me/preferences/llm-endpoint", json={"llm_base_url": ""})
    with SessionLocal() as db:
        user = db.query(User).first()
        from app.services.llm import user_llm_endpoint
        resolved = user_llm_endpoint(db, user.id)
    assert resolved.base_url == settings.llm_base_url


def test_run_keeps_each_agent_source(authenticated: TestClient, monkeypatch):
    authenticated.put("/api/credentials", json={"kind": "llm_api_key", "value": "test-key"})

    async def fake_models(_key, search="", **_kwargs):
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


def test_run_references_are_deduplicated_and_filter_unsafe_urls(authenticated: TestClient):
    with SessionLocal() as db:
        user = db.query(User).filter_by(username="admin").one()
        run = create_run(db, user.id, RunCreate.model_validate({
            "model_id": "gpt-test",
            "continuous": False,
            "agents": [{"name": "Fab", "role": "research", "domain": "fab", "objective": "coverage", "source_mode": "web"}],
        }))
        agent_id = run.agents[0].id
        db.add(AgentIteration(
            run_id=run.id,
            agent_id=agent_id,
            round_number=1,
            status="completed",
            evidence_json=json.dumps([
                {"title": "公开来源", "url": "https://example.com/fab", "fetch_status": "ok", "excerpt": "摘要"},
                {"title": "重复来源", "url": "https://example.com/fab", "fetch_status": "ok"},
                {"title": "本地地址", "url": "http://127.0.0.1/internal", "fetch_status": "ok"},
                {"title": "令牌地址", "url": "https://example.com/a?token=hidden", "fetch_status": "ok"},
                {"title": "模型先验（不应展示）", "url": "https://example.com/prior", "source_type": "model_prior", "fetch_status": "ok"},
            ], ensure_ascii=False),
        ))
        db.commit()
        run_id = run.id

    response = authenticated.get(f"/api/runs/{run_id}/references")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["url"] == "https://example.com/fab"
    assert body["items"][0]["provenance"][0]["agent_name"] == "Fab"


def test_article_can_be_sent_to_wechat_draft_and_is_idempotent(authenticated: TestClient, monkeypatch):
    authenticated.put("/api/credentials", json={"kind": "wechat_app_id", "value": "wx-test-app"})
    authenticated.put("/api/credentials", json={"kind": "wechat_app_secret", "value": "secret-test"})
    with SessionLocal() as db:
        user = db.query(User).filter_by(username="admin").one()
        article = Article(
            user_id=user.id,
            title="设备状态信号为何总是晚一步",
            subtitle="从一个现场信号看交付风险",
            status="waiting_approval",
            content_markdown="# 设备状态信号为何总是晚一步\n\n客户痛点与现场表现。",
            content_html="<h1>设备状态信号为何总是晚一步</h1>",
            validation_json=json.dumps({"passed": True}),
        )
        db.add(article)
        db.commit()
        db.refresh(article)
        article_id = article.id

    calls = []

    async def fake_create_draft(**kwargs):
        calls.append(kwargs)
        assert kwargs["app_id"] == "wx-test-app"
        assert kwargs["app_secret"] == "secret-test"
        return {"media_id": "draft-media-1", "errcode": 0, "errmsg": "ok"}

    monkeypatch.setattr(wechat_publisher, "create_draft", fake_create_draft)
    first = authenticated.post(f"/api/articles/{article_id}/wechat-draft")
    assert first.status_code == 200
    assert first.json()["wechat_status"] == "sent_to_draft"
    assert first.json()["wechat_draft_media_id"] == "draft-media-1"
    second = authenticated.post(f"/api/articles/{article_id}/wechat-draft")
    assert second.status_code == 200
    assert len(calls) == 1


def test_scenario_knowledge_products_are_listed_and_read(authenticated: TestClient):
    target_dir = settings.semi_kb_root / "knowledge" / "articles" / "agent-rounds"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "test-api-scenario.md"
    target.write_text("# 场景知识产物\n\n这里是测试内容。", encoding="utf-8")
    try:
        listing = authenticated.get("/api/scenario-knowledge")
        assert listing.status_code == 200
        item = next(entry for entry in listing.json()["items"] if entry["path"].endswith("test-api-scenario.md"))
        assert listing.json()["total"] >= 1
        detail = authenticated.get("/api/scenario-knowledge/file", params={"path": item["path"]})
        assert detail.status_code == 200
        assert "场景知识产物" in detail.json()["content"]
        blocked = authenticated.get("/api/scenario-knowledge/file", params={"path": "knowledge/articles/generated/article-1.md"})
        assert blocked.status_code == 403
    finally:
        target.unlink(missing_ok=True)


def _seed_run(db, user_id: int, status: str) -> str:
    """Create a terminal-or-active run with a child round, event and iteration."""
    run = create_run(db, user_id, RunCreate.model_validate({
        "model_id": "gpt-test",
        "continuous": False,
        "agents": [{"name": "Fab", "role": "research", "domain": "fab", "objective": "coverage", "source_mode": "web"}],
    }))
    run.status = status
    db.add(RunRound(run_id=run.id, round_number=1, status="completed", current_stage="completed"))
    db.add(RunEvent(run_id=run.id, event_type="run_started", message="started", payload_json="{}"))
    db.add(AgentIteration(run_id=run.id, agent_id=run.agents[0].id, round_number=1, status="completed"))
    db.commit()
    return run.id


def test_ontology_cascade_endpoint_partitions_domain_individuals(authenticated: TestClient):
    response = authenticated.get("/api/ontology/cascade")
    assert response.status_code == 200
    body = response.json()
    assert body["systems"], "expected at least one source system"
    # 每个源系统聚合的实例总数之和必须等于领域实例总量（级联不重不漏）。
    assert sum(system["total"] for system in body["systems"]) == body["domain_total"]
    labels = {system["system"] for system in body["systems"]}
    assert {"manufacturing", "erp"} <= labels
    # 制造侧必须携带工艺段细分；ERP 全部落在跨段通用，不产生前段/后段。
    manufacturing = next(system for system in body["systems"] if system["system"] == "manufacturing")
    assert manufacturing["segments"], "manufacturing must expose process-segment breakdown"


def test_export_accepts_source_system_scope(authenticated: TestClient):
    response = authenticated.post("/api/exports", json={"kind": "ontology", "scope": "erp"})
    assert response.status_code == 202
    assert response.json()["scope"] == "erp"
    # 默认 scope 省略时回落 all
    default = authenticated.post("/api/exports", json={"kind": "ontology"})
    assert default.json()["scope"] == "all"
    # 非法 scope 被 schema 拒绝
    bad = authenticated.post("/api/exports", json={"kind": "ontology", "scope": "sap-only"})
    assert bad.status_code == 422


def test_report_settings_default_email_reminder_enabled(authenticated: TestClient):
    body = authenticated.get("/api/report-settings").json()
    assert body["email_reminder_enabled"] is True


def test_report_settings_email_reminder_can_be_disabled(authenticated: TestClient):
    payload = {
        "enabled": True,
        "generate_time": "18:00",
        "approval_required": True,
        "reminder_timeout_minutes": 10,
        "email_sender": "sender@qq.com",
        "email_recipient": "ops@qq.com",
        "email_reminder_enabled": False,
    }
    assert authenticated.put("/api/report-settings", json=payload).status_code == 200
    body = authenticated.get("/api/report-settings").json()
    assert body["email_reminder_enabled"] is False
    assert body["email_recipient"] == "ops@qq.com"


def test_delete_run_purges_all_children(authenticated: TestClient):
    with SessionLocal() as db:
        user = db.query(User).filter_by(username="admin").one()
        run_id = _seed_run(db, user.id, "completed")

    response = authenticated.delete(f"/api/runs/{run_id}")
    assert response.status_code == 200
    assert response.json()["deleted"] == run_id

    with SessionLocal() as db:
        assert db.get(Run, run_id) is None
        assert db.query(RunRound).filter_by(run_id=run_id).count() == 0
        assert db.query(RunEvent).filter_by(run_id=run_id).count() == 0
        assert db.query(AgentIteration).filter_by(run_id=run_id).count() == 0


def test_delete_active_run_is_rejected(authenticated: TestClient):
    with SessionLocal() as db:
        user = db.query(User).filter_by(username="admin").one()
        run_id = _seed_run(db, user.id, "running")

    response = authenticated.delete(f"/api/runs/{run_id}")
    assert response.status_code == 409
    with SessionLocal() as db:
        assert db.get(Run, run_id) is not None


def test_clear_finished_keeps_active_runs(authenticated: TestClient):
    with SessionLocal() as db:
        user = db.query(User).filter_by(username="admin").one()
        done_id = _seed_run(db, user.id, "completed")
        failed_id = _seed_run(db, user.id, "failed")
        active_id = _seed_run(db, user.id, "running")

    response = authenticated.post("/api/runs/clear-finished")
    assert response.status_code == 200
    assert response.json()["deleted"] == 2

    with SessionLocal() as db:
        assert db.get(Run, done_id) is None
        assert db.get(Run, failed_id) is None
        assert db.get(Run, active_id) is not None
