from __future__ import annotations

from fastapi.testclient import TestClient


def test_export_center_and_safe_knowledge_detail(authenticated: TestClient, monkeypatch):
    authenticated.post("/api/exports", json={"kind": "knowledge"})
    listing = authenticated.get("/api/exports")
    assert listing.status_code == 200
    assert listing.json()["items"]
    traversal = authenticated.get("/api/knowledge/file", params={"path": "../secrets.txt"})
    assert traversal.status_code in {403, 404}


def test_article_settings_and_topics(authenticated: TestClient):
    settings = authenticated.get("/api/article-settings")
    assert settings.status_code == 200
    assert settings.json()["approval_required"] is True
    updated = authenticated.put("/api/article-settings", json={"enabled": True, "frequency": "daily", "generate_time": "19:30", "timezone": "Asia/Shanghai", "approval_required": False, "auto_visuals": True})
    assert updated.status_code == 200
    assert updated.json()["generate_time"] == "19:30"
    assert authenticated.get("/api/article-topics").status_code == 200
