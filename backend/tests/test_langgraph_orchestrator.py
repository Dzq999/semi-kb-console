from __future__ import annotations

import json
import asyncio
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.main import create_run
from app.models import Run, RunRound, User
from app.schemas import RunCreate
from app.services.orchestrator import orchestrator
from app.services.langgraph_runtime import RoundGraphEngine
from app.services.semantic_pipeline import round_directory


def _payload(agent_count: int = 2) -> RunCreate:
    return RunCreate.model_validate({
        "model_id": "gpt-test", "continuous": False, "round_interval_seconds": 0,
        "agents": [{
            "name": f"Agent {index}", "role": "research", "domain": "fab",
            "objective": f"coverage {index}", "source_mode": "model_prior",
        } for index in range(agent_count)],
    })


@pytest.mark.asyncio
async def test_langgraph_round_runs_all_nodes_and_tolerates_partial_agent_failure(authenticated, monkeypatch):
    authenticated.put("/api/credentials", json={"kind": "llm_api_key", "value": "test-key"})
    with SessionLocal() as db:
        user = db.scalar(select(User)); run = create_run(db, user.id, _payload())
        run_id = run.id; agent_ids = [agent.id for agent in run.agents]

    async def fake_status(): return {"ready": True}
    async def fake_metrics(_db=None, _user_id=None): return {"totals": {"classes": 1, "individuals": 2}, "today_added": {}}
    async def fake_process(_candidates, publish=False):
        return {"published": publish, "checks": {"semantic_precheck": {"passed": True}, "business_simulation": {"passed": True}}}

    async def fake_agent(_run_id, agent_id, round_number, _key, _gap, _config):
        if agent_id == agent_ids[1]: return None
        article = round_directory(_run_id, round_number) / f"{agent_id}-article.md"
        article.write_text("场景与客户痛点\n\n经营模型与仿真结果。", encoding="utf-8")
        return {"semantic_files": [], "business_files": [], "simulation_files": [], "article_file": str(article), "evidence_count": 2}

    monkeypatch.setattr("app.services.langgraph_runtime.semi_kb.status", fake_status)
    monkeypatch.setattr("app.services.langgraph_runtime.semi_kb.metrics", fake_metrics)
    monkeypatch.setattr("app.services.langgraph_runtime.semi_kb.process_candidates", fake_process)
    monkeypatch.setattr(orchestrator, "execute_agent", fake_agent)

    with SessionLocal() as db:
        config = json.loads(db.get(Run, run_id).config_json)
    assert await orchestrator.execute_round(run_id, 1, config, "unused")

    with SessionLocal() as db:
        row = db.scalar(select(RunRound).where(RunRound.run_id == run_id, RunRound.round_number == 1))
        assert row.status == "completed"
        attempts = json.loads(row.node_attempts_json)
        assert attempts["gap_analysis"] == 1
        assert attempts["scenario_article"] == 1
        assert json.loads(row.artifacts_json)["evidence_pages"] == 2
        assert row.checkpoint_id


@pytest.mark.asyncio
async def test_langgraph_quarantines_only_invalid_candidate_then_revalidates(authenticated, monkeypatch):
    authenticated.put("/api/credentials", json={"kind": "llm_api_key", "value": "test-key"})
    with SessionLocal() as db:
        user = db.scalar(select(User)); run = create_run(db, user.id, _payload(1)); run_id = run.id

    async def fake_status(): return {"ready": True}
    async def fake_metrics(_db=None, _user_id=None): return {"totals": {"classes": 1}, "today_added": {}}

    async def fake_agent(_run_id, agent_id, round_number, _key, _gap, _config):
        root = round_directory(_run_id, round_number)
        good = root / "good.json"; bad = root / "bad.json"; article = root / "article.md"
        good.write_text("{}", encoding="utf-8"); bad.write_text("{}", encoding="utf-8"); article.write_text("场景 客户痛点 经营 仿真", encoding="utf-8")
        return {"semantic_files": [str(good), str(bad)], "business_files": [], "simulation_files": [], "article_file": str(article), "evidence_count": 0}

    async def fake_process(candidates, publish=False):
        names = [path.name for path in candidates.get("semantic") or []]
        if "bad.json" in names: raise ValueError("bad candidate")
        return {"published": publish, "checks": {"semantic_precheck": {"passed": True}, "business_simulation": {"passed": True}}}

    monkeypatch.setattr("app.services.langgraph_runtime.semi_kb.status", fake_status)
    monkeypatch.setattr("app.services.langgraph_runtime.semi_kb.metrics", fake_metrics)
    monkeypatch.setattr("app.services.langgraph_runtime.semi_kb.process_candidates", fake_process)
    monkeypatch.setattr(orchestrator, "execute_agent", fake_agent)
    with SessionLocal() as db:
        config = json.loads(db.get(Run, run_id).config_json)
    assert await orchestrator.execute_round(run_id, 1, config, "unused")
    with SessionLocal() as db:
        row = db.scalar(select(RunRound).where(RunRound.run_id == run_id))
        quarantined = json.loads(row.quarantined_files_json)
        assert len(quarantined) == 1 and Path(quarantined[0]).name == "bad.json"
        assert Path(quarantined[0]).is_file()


@pytest.mark.asyncio
async def test_langgraph_resumes_from_last_node_checkpoint_after_process_cancellation(authenticated, monkeypatch):
    authenticated.put("/api/credentials", json={"kind": "llm_api_key", "value": "test-key"})
    with SessionLocal() as db:
        user = db.scalar(select(User)); run = create_run(db, user.id, _payload(1)); run_id = run.id
        config = json.loads(run.config_json)

    calls = {"agent": 0}
    async def fake_status(): return {"ready": True}
    async def fake_metrics(_db=None, _user_id=None): return {"totals": {"classes": 1}, "today_added": {}}
    async def fake_process(_candidates, publish=False): return {"published": publish, "checks": {"semantic_precheck": {"passed": True}, "business_simulation": {"passed": True}}}
    async def fake_agent(_run_id, agent_id, round_number, _key, _gap, _config):
        calls["agent"] += 1
        article = round_directory(_run_id, round_number) / "resume-article.md"
        article.write_text("场景 客户痛点 经营 仿真", encoding="utf-8")
        return {"semantic_files": [], "business_files": [], "simulation_files": [], "article_file": str(article), "evidence_count": 0}

    monkeypatch.setattr("app.services.langgraph_runtime.semi_kb.status", fake_status)
    monkeypatch.setattr("app.services.langgraph_runtime.semi_kb.metrics", fake_metrics)
    monkeypatch.setattr("app.services.langgraph_runtime.semi_kb.process_candidates", fake_process)
    monkeypatch.setattr(orchestrator, "execute_agent", fake_agent)
    engine = RoundGraphEngine(orchestrator)
    original = engine.semantic_modeling

    async def simulate_process_exit(_state):
        raise __import__("asyncio").CancelledError

    engine.semantic_modeling = simulate_process_exit
    with pytest.raises(__import__("asyncio").CancelledError):
        await engine.execute(run_id, 1, config, resume=False)
    engine.semantic_modeling = original
    assert await engine.execute(run_id, 1, config, resume=True)
    with SessionLocal() as db:
        row = db.scalar(select(RunRound).where(RunRound.run_id == run_id))
        attempts = json.loads(row.node_attempts_json)
        assert row.resumed_count == 1
        assert attempts["gap_analysis"] == 1
        assert attempts["parallel_research"] == 1
        assert attempts["semantic_modeling"] == 1
    assert calls["agent"] == 1


@pytest.mark.asyncio
async def test_langgraph_runs_ten_agent_subgraphs_concurrently(authenticated, monkeypatch):
    authenticated.put("/api/credentials", json={"kind": "llm_api_key", "value": "test-key"})
    with SessionLocal() as db:
        user = db.scalar(select(User)); run = create_run(db, user.id, _payload(10)); run_id = run.id
        config = json.loads(run.config_json)
    active = 0; maximum = 0; calls = 0
    async def fake_status(): return {"ready": True}
    async def fake_metrics(_db=None, _user_id=None): return {"totals": {"classes": 1}, "today_added": {}}
    async def fake_process(_candidates, publish=False): return {"published": publish, "checks": {"semantic_precheck": {"passed": True}, "business_simulation": {"passed": True}}}
    async def fake_agent(_run_id, agent_id, round_number, _key, _gap, _config):
        nonlocal active, maximum, calls
        active += 1; maximum = max(maximum, active); calls += 1
        await asyncio.sleep(0.02)
        active -= 1
        article = round_directory(_run_id, round_number) / f"{agent_id}-parallel.md"
        article.write_text("场景 客户痛点 经营 仿真", encoding="utf-8")
        return {"semantic_files": [], "business_files": [], "simulation_files": [], "article_file": str(article), "evidence_count": 0}
    monkeypatch.setattr("app.services.langgraph_runtime.semi_kb.status", fake_status)
    monkeypatch.setattr("app.services.langgraph_runtime.semi_kb.metrics", fake_metrics)
    monkeypatch.setattr("app.services.langgraph_runtime.semi_kb.process_candidates", fake_process)
    monkeypatch.setattr(orchestrator, "execute_agent", fake_agent)
    assert await orchestrator.execute_round(run_id, 1, config, "unused")
    assert calls == 10
    assert maximum > 1


@pytest.mark.asyncio
async def test_langgraph_round_success_writes_direction_and_next_round_reads_it(authenticated, monkeypatch):
    """闭环显式化：一轮成功 → RunRound.next_direction_json 有值；下一轮 augment_gap 读回注入 gap。"""
    from app.services.reports import augment_gap

    authenticated.put("/api/credentials", json={"kind": "llm_api_key", "value": "test-key"})
    with SessionLocal() as db:
        user = db.scalar(select(User)); run = create_run(db, user.id, _payload(1)); run_id = run.id
        config = json.loads(run.config_json)

    fake_gap = {"unmapped_total": 700, "business_relevant": 120,
                "by_theme": [{"theme": "运维域", "count": 30, "samples": ["巡检项"]}]}
    async def fake_status(): return {"ready": True}
    async def fake_metrics(_db=None, _user_id=None): return {"totals": {"classes": 1}, "today_added": {}}
    async def fake_process(_candidates, publish=False):
        return {"published": publish, "checks": {"semantic_precheck": {"passed": True}, "business_simulation": {"passed": True}}}
    async def fake_agent(_run_id, agent_id, round_number, _key, _gap, _config):
        article = round_directory(_run_id, round_number) / "direction-article.md"
        article.write_text("场景 客户痛点 经营 仿真", encoding="utf-8")
        return {"semantic_files": [], "business_files": [], "simulation_files": [], "article_file": str(article), "evidence_count": 0}

    monkeypatch.setattr("app.services.langgraph_runtime.semi_kb.status", fake_status)
    monkeypatch.setattr("app.services.langgraph_runtime.semi_kb.metrics", fake_metrics)
    monkeypatch.setattr("app.services.langgraph_runtime.semi_kb.process_candidates", fake_process)
    monkeypatch.setattr("app.services.langgraph_runtime.semi_kb.feature_gap", lambda: fake_gap)
    monkeypatch.setattr(orchestrator, "execute_agent", fake_agent)

    assert await orchestrator.execute_round(run_id, 1, config, "unused")

    with SessionLocal() as db:
        row = db.scalar(select(RunRound).where(RunRound.run_id == run_id, RunRound.round_number == 1))
        direction = json.loads(row.next_direction_json)
        assert direction["unmapped_total"] == 700
        assert "运维域" in direction["focus_themes"]

    # 下一轮 gap_analysis 的 augment_gap 应把上一轮方向读回。feature_gap 已被 monkeypatch。
    monkeypatch.setattr("app.services.reports.semi_kb.feature_gap", lambda: fake_gap)
    gap = augment_gap({}, run_id)
    assert gap["prior_round_direction"]["unmapped_total"] == 700
    assert "运维域" in gap["prior_round_direction"]["focus_themes"]
