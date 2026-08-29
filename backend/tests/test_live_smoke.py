from __future__ import annotations

import json
import os

import pytest
from sqlalchemy import select

from app.db import Base, SessionLocal, engine
from app.main import create_run
from app.models import AgentIteration, Run, RunRound, User
from app.schemas import RunCreate
from app.security import hash_password
from app.services.llm import llm_service
from app.services.orchestrator import orchestrator


@pytest.mark.skipif(os.getenv("RUN_LIVE_TESTS") != "1", reason="requires live model and web access")
@pytest.mark.asyncio
async def test_one_real_web_agent_full_round_without_publication():
    api_key = os.getenv("4SAPI_API_KEY")
    assert api_key, "missing 4SAPI_API_KEY"
    catalog = await llm_service.list_models(api_key)
    model_ids = {item["id"] for item in catalog}
    assert model_ids
    model = "gpt-5.6-sol" if "gpt-5.6-sol" in model_ids else sorted(model_ids)[0]
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        user = User(username="live-smoke", password_hash=hash_password("live-smoke-password"))
        db.add(user); db.flush()
        run = create_run(db, user.id, RunCreate.model_validate({
            "model_id": model,
            "continuous": False,
            "publish_changes": False,
            "round_interval_seconds": 0,
            "agents": [{
                "name": "Fab Web Smoke", "role": "research", "domain": "fab",
                "objective": "扩展晶圆厂设备状态与产能损失问题域",
                "source_mode": "web", "max_retries": 2,
            }],
        }))
        run_id = run.id
    await orchestrator.execute(run_id)
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        round_row = db.scalar(select(RunRound).where(RunRound.run_id == run_id))
        iteration = db.scalar(select(AgentIteration).where(AgentIteration.run_id == run_id))
        assert run.status == "completed", run.error
        assert round_row and round_row.status == "completed", round_row.error if round_row else "missing round"
        assert iteration and iteration.status == "completed", iteration.error if iteration else "missing iteration"
        evidence = json.loads(iteration.evidence_json)
        assert any(item.get("fetch_status") == "ok" and item.get("content_sha256") for item in evidence)
        output = json.loads(iteration.output_json)
        assert output.get("article_file")
        assert json.loads(round_row.validation_json).get("published") is False
