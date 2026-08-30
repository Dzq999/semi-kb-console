from __future__ import annotations

import pytest

from app.db import SessionLocal
from app.models import ArticleTopic, User
from app.services.articles import generate_article
from app.services.llm import llm_service


@pytest.mark.asyncio
async def test_article_validation_retries_at_most_configured_limit(authenticated, monkeypatch):
    authenticated.put("/api/credentials", json={"kind": "llm_api_key", "value": "test-key"})
    with SessionLocal() as db:
        user = db.query(User).filter_by(username="admin").one()
        topic = ArticleTopic(
            user_id=user.id,
            title="设备停机导致交付波动",
            domain="eqp",
            pain_point="设备停机导致产能波动",
            business_context="现场设备与交付计划需要联动",
            evidence_json='[{"source":"https://example.com/evidence","excerpt":"设备停机记录"}]',
            status="qualified",
        )
        db.add(topic)
        db.commit()
        db.refresh(topic)

        calls = []

        async def fake_complete(*args, **kwargs):
            calls.append(args[3])
            if len(calls) == 1:
                return "设备停机为何总是晚一步\n太短"
            return "设备停机为何总是晚一步\n" + ("客户痛点与设备停机导致产能波动密切相关。" * 80)

        monkeypatch.setattr(llm_service, "complete", fake_complete)
        article = await generate_article(
            db,
            user.id,
            topic,
            "gpt-test",
            auto_visuals=False,
            max_repair_attempts=3,
        )

        assert len(calls) == 2
        assert article.repair_attempts == 1
        assert article.status == "waiting_approval"
        assert '"repair_attempts": 1' in article.validation_json


@pytest.mark.asyncio
async def test_article_repair_limit_zero_hands_draft_to_user(authenticated, monkeypatch):
    authenticated.put("/api/credentials", json={"kind": "llm_api_key", "value": "test-key"})
    with SessionLocal() as db:
        user = db.query(User).filter_by(username="admin").one()
        topic = ArticleTopic(user_id=user.id, title="质量异常", domain="fab", pain_point="质量异常", evidence_json='[{"source":"https://example.com"}]')
        db.add(topic)
        db.commit()
        db.refresh(topic)

        async def fake_complete(*args, **kwargs):
            return "质量异常如何被及时发现\n内容不足"

        monkeypatch.setattr(llm_service, "complete", fake_complete)
        article = await generate_article(db, user.id, topic, "gpt-test", auto_visuals=False, max_repair_attempts=0)

        assert article.repair_attempts == 0
        assert article.status == "blocked"
        assert "需用户修改" in article.generation_stage
