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
    assert settings.json()["auto_repair"] is True
    assert settings.json()["max_repair_attempts"] == 3
    updated = authenticated.put("/api/article-settings", json={"enabled": True, "frequency": "daily", "generate_time": "19:30", "timezone": "Asia/Shanghai", "approval_required": False, "auto_visuals": True, "daily_article_count": 3, "article_model_id": "gpt-test", "image_model_id": "gpt-image-2", "image_count": 2, "auto_repair": False, "max_repair_attempts": 0})
    assert updated.status_code == 200
    assert updated.json()["generate_time"] == "19:30"
    assert updated.json()["daily_article_count"] == 3
    assert updated.json()["image_count"] == 2
    assert updated.json()["auto_repair"] is False
    assert updated.json()["max_repair_attempts"] == 0
    assert authenticated.get("/api/article-topics").status_code == 200


def test_article_generate_route_is_not_captured_by_numeric_detail(authenticated: TestClient):
    response = authenticated.post("/api/articles/generate", json={})
    assert response.status_code == 422
    assert "默认模型" in str(response.json())
