from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.main import create_run
from app.models import Run, RunRound, User
from app.schemas import RunCreate
from app.services.orchestrator import orchestrator


@pytest.mark.asyncio
async def test_continuous_run_stops_at_requested_round(authenticated, monkeypatch):
    authenticated.put("/api/credentials", json={"kind": "llm_api_key", "value": "test-key"})
    payload = RunCreate.model_validate({
        "model_id": "gpt-test",
        "continuous": True,
        "round_interval_seconds": 0,
        "agents": [{"name": "Fab", "role": "research", "domain": "fab", "objective": "coverage", "source_mode": "model_prior"}],
    })
    with SessionLocal() as db:
        user = db.scalar(select(User))
        run = create_run(db, user.id, payload)
        run_id = run.id

    async def metrics(_db=None, _user_id=None):
        return {"totals": {"classes": 1}, "today_added": {}}

    rounds: list[int] = []

    async def execute_round(run_id: str, round_number: int, _config: dict, _api_key: str) -> bool:
        rounds.append(round_number)
        with SessionLocal() as db:
            db.add(RunRound(run_id=run_id, round_number=round_number, status="completed", current_stage="completed", metrics_before_json="{}", metrics_after_json="{}"))
            db.commit()
        return True

    monkeypatch.setattr("app.services.orchestrator.semi_kb.metrics", metrics)
    monkeypatch.setattr(orchestrator, "execute_round", execute_round)
    orchestrator.request_stop_after(run_id, current_round=1, additional_rounds=1)
    await orchestrator.execute(run_id)

    with SessionLocal() as db:
        run = db.get(Run, run_id)
        assert run.status == "completed"
        assert json.loads(run.config_json)["continuous"] is True
    assert rounds == [1, 2]


@pytest.mark.asyncio
async def test_running_loop_keeps_starting_rounds_until_next_round_stop_is_clicked(authenticated, monkeypatch):
    authenticated.put("/api/credentials", json={"kind": "llm_api_key", "value": "test-key"})
    payload = RunCreate.model_validate({
        "model_id": "gpt-test", "continuous": True, "round_interval_seconds": 0,
        "agents": [{"name": "Eqp", "role": "research", "domain": "eqp", "objective": "continuous coverage", "source_mode": "model_prior"}],
    })
    with SessionLocal() as db:
        user = db.scalar(select(User))
        run = create_run(db, user.id, payload)
        run_id = run.id

    async def metrics(_db=None, _user_id=None):
        return {"totals": {"classes": 1}, "today_added": {}}

    rounds: list[int] = []

    async def execute_round(active_run_id: str, round_number: int, _config: dict, _api_key: str) -> bool:
        rounds.append(round_number)
        with SessionLocal() as db:
            db.add(RunRound(run_id=active_run_id, round_number=round_number, status="completed", current_stage="completed", metrics_before_json="{}", metrics_after_json="{}"))
            db.commit()
        if round_number == 2:
            orchestrator.request_stop_after(active_run_id, current_round=round_number, additional_rounds=1)
        return True

    monkeypatch.setattr("app.services.orchestrator.semi_kb.metrics", metrics)
    monkeypatch.setattr(orchestrator, "execute_round", execute_round)
    await orchestrator.execute(run_id)

    with SessionLocal() as db:
        assert db.get(Run, run_id).status == "completed"
    assert rounds == [1, 2, 3]


def test_stop_after_next_round_endpoint(authenticated, monkeypatch):
    authenticated.put("/api/credentials", json={"kind": "llm_api_key", "value": "test-key"})

    async def fake_models(_key, search=""):
        return [{"id": "gpt-test", "available": True}]

    monkeypatch.setattr("app.main.llm_service.list_models", fake_models)
    monkeypatch.setattr(orchestrator, "start", lambda _run_id: None)
    response = authenticated.post("/api/runs", json={
        "model_id": "gpt-test", "continuous": True,
        "agents": [{"name": "Fab", "role": "research", "domain": "fab", "objective": "coverage", "source_mode": "model_prior"}],
    })
    run_id = response.json()["id"]
    with SessionLocal() as db:
        db.add(RunRound(run_id=run_id, round_number=3, status="running", current_stage="parallel_research"))
        db.commit()
    stopped = authenticated.post(f"/api/runs/{run_id}/stop-after-next-round")
    assert stopped.status_code == 200
    assert stopped.json()["stop_after_round"] == 4
