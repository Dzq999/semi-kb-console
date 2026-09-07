from __future__ import annotations

import json
import asyncio
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.main import create_run
from app.models import Run, RunEvent, RunRound, User
from app.schemas import RunCreate
from app.services.orchestrator import orchestrator
from app.services.langgraph_runtime import RoundGraphEngine
from app.services.llm import ExternalServiceError
from app.services.semantic_pipeline import round_directory
from app.services.semi_kb import SemiKbError


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


# --- 门禁路由（纯函数，无 DB/子进程）---------------------------------------

def test_classify_gate_error_isolates_reasoning():
    """推理/结构不可满足类 → isolate（跳过 LLM 修不了的返修）；安全类仍最先判为 hard。"""
    classify = RoundGraphEngine.classify_gate_error
    assert classify("本体推理不一致：unsatisfiable class X") == "isolate"
    assert classify("Reasoner reported an inconsistency between disjoint classes") == "isolate"
    # 安全/凭据类优先级最高，绝不被 isolate/repairable 抢走
    assert classify("检测到敏感凭据 bearer 泄露") == "hard"
    # 通用「缺字段」仍是可返修
    assert classify("候选缺少必填字段 hasPossibleCause") == "repairable"
    assert classify("完全未知的错误") == "system"


def test_route_validation_isolate_skips_repair():
    engine = RoundGraphEngine(orchestrator)
    base = {"gate_error": "x", "repair_attempt": 0, "config": {}}
    assert engine.route_validation({**base, "gate_error_class": "isolate"}) == "partial_publish"
    assert engine.route_validation({**base, "gate_error_class": "hard"}) == "fail_gate"
    assert engine.route_validation({**base, "gate_error_class": "repairable"}) == "repair_candidates"
    assert engine.route_validation({"gate_error": None}) == "owl_shacl_reasoning"
    # repairable 到达上限 → 不再空耗返修，转 partial_publish
    at_limit = {"gate_error": "x", "gate_error_class": "repairable", "repair_attempt": 5,
                "config": {"max_consecutive_round_failures": 1}}
    assert engine.route_validation(at_limit) == "partial_publish"


def test_route_after_repair_zero_progress_skips_gate():
    engine = RoundGraphEngine(orchestrator)
    assert engine.route_after_repair({"repair_made_progress": True}) == "cross_validation"
    assert engine.route_after_repair({"repair_made_progress": False}) == "partial_publish"
    # 旧 checkpoint 缺该字段 → 保守走 partial_publish（发好隔坏），不重跑整图门禁
    assert engine.route_after_repair({}) == "partial_publish"


# --- partial_publish 隔离预检封顶 -------------------------------------------

def _run_with_round(round_number: int = 1, agent_count: int = 1) -> str:
    with SessionLocal() as db:
        user = db.scalar(select(User))
        run = create_run(db, user.id, _payload(agent_count))
        run_id = run.id
        db.add(RunRound(run_id=run_id, round_number=round_number, status="running", current_stage="cross_validation"))
        db.commit()
    return run_id


def _semantic_outputs(run_id: str, round_number: int, count: int) -> list[dict]:
    root = round_directory(run_id, round_number)
    files = []
    for index in range(count):
        path = root / f"semantic-{index}.json"
        path.write_text("{}", encoding="utf-8")
        files.append(str(path))
    return [{"semantic_files": files, "business_files": [], "simulation_files": [],
             "knowledge_files": [], "rule_files": [], "mapping_files": [], "article_file": None}]


def _partial_state(run_id: str, round_number: int, outputs: list[dict], config: dict) -> dict:
    return {"run_id": run_id, "round_number": round_number, "config": config, "outputs": outputs,
            "quarantined_files": [], "repair_attempt": 0,
            "gate_error": "本体推理不一致：unsatisfiable class", "gate_error_class": "isolate"}


@pytest.mark.asyncio
async def test_partial_publish_caps_prechecks_and_quarantines_remainder(authenticated, monkeypatch):
    """全坏批次：isolate() 预检次数被预算封顶，剩余未证明候选保守隔离，绝不逐个再跑昂贵门禁。"""
    authenticated.put("/api/credentials", json={"kind": "llm_api_key", "value": "test-key"})
    run_id = _run_with_round()
    outputs = _semantic_outputs(run_id, 1, 8)
    calls = {"precheck": 0, "process": 0}

    async def fake_precheck(group):
        calls["precheck"] += 1
        return {"exit_code": 1, "passed": False, "output": "unsatisfiable"}

    async def fake_process(*args, **kwargs):
        calls["process"] += 1
        return {"accepted_candidates": {"semantic": 1}}

    async def fake_aux(_candidates):
        return {"accepted_candidates": {}, "quarantined_candidates": []}

    monkeypatch.setattr("app.services.langgraph_runtime.semi_kb.semantic_precheck_sources", fake_precheck)
    monkeypatch.setattr("app.services.langgraph_runtime.semi_kb.process_candidates", fake_process)
    monkeypatch.setattr("app.services.langgraph_runtime.semi_kb.publish_auxiliary_candidates", fake_aux)

    engine = RoundGraphEngine(orchestrator)
    state = _partial_state(run_id, 1, outputs, {"partial_isolate_max_prechecks": 3})
    with pytest.raises(SemiKbError):  # 全坏批次没有任何候选可发布 → 正确地失败
        await engine.partial_publish(state)

    assert calls["precheck"] == 3  # 封顶：预检次数不超过预算
    assert calls["process"] == 0   # 预算耗尽的候选不再逐个跑昂贵门禁
    quarantine = round_directory(run_id, 1) / "quarantine"
    assert len(list(quarantine.glob("semantic-*.json"))) == 8  # 未证明的一律隔离
    with SessionLocal() as db:
        assert db.query(RunEvent).filter_by(run_id=run_id, event_type="isolate_budget_exhausted").count() == 1


@pytest.mark.asyncio
async def test_partial_publish_deadline_stops_bisection(authenticated, monkeypatch):
    """墙钟预算：deadline 立即到期时，一次预检都不跑，整批保守隔离。"""
    authenticated.put("/api/credentials", json={"kind": "llm_api_key", "value": "test-key"})
    run_id = _run_with_round()
    outputs = _semantic_outputs(run_id, 1, 4)
    calls = {"precheck": 0}

    async def fake_precheck(group):
        calls["precheck"] += 1
        return {"exit_code": 1, "passed": False, "output": "unsatisfiable"}

    async def fake_aux(_candidates):
        return {"accepted_candidates": {}, "quarantined_candidates": []}

    monkeypatch.setattr("app.services.langgraph_runtime.semi_kb.semantic_precheck_sources", fake_precheck)
    monkeypatch.setattr("app.services.langgraph_runtime.semi_kb.publish_auxiliary_candidates", fake_aux)

    engine = RoundGraphEngine(orchestrator)
    # deadline_seconds=0 → 进入 isolate 时 time.monotonic() >= deadline 立即成立
    state = _partial_state(run_id, 1, outputs, {"partial_isolate_deadline_seconds": 0})
    with pytest.raises(SemiKbError):
        await engine.partial_publish(state)

    assert calls["precheck"] == 0  # 墙钟已耗尽，不再触发任何整图预检
    quarantine = round_directory(run_id, 1) / "quarantine"
    assert len(list(quarantine.glob("semantic-*.json"))) == 4


@pytest.mark.asyncio
async def test_partial_publish_isolate_happy_path_single_check_publishes_all(authenticated, monkeypatch):
    """整批一次预检即通过：只跑一次权威门禁、透传 batch_prechecked、无隔离。"""
    authenticated.put("/api/credentials", json={"kind": "llm_api_key", "value": "test-key"})
    run_id = _run_with_round()
    outputs = _semantic_outputs(run_id, 1, 4)
    calls = {"precheck": 0, "process": 0, "batch_flag": None}

    async def fake_precheck(group):
        calls["precheck"] += 1
        return {"exit_code": 0, "passed": True, "output": ""}

    async def fake_process(filtered, publish=False, *, semantic_batch_prechecked=False):
        calls["process"] += 1
        calls["batch_flag"] = semantic_batch_prechecked
        return {"accepted_candidates": {"semantic": len(filtered["semantic"])}}

    async def fake_aux(_candidates):
        return {"accepted_candidates": {}, "quarantined_candidates": []}

    monkeypatch.setattr("app.services.langgraph_runtime.semi_kb.semantic_precheck_sources", fake_precheck)
    monkeypatch.setattr("app.services.langgraph_runtime.semi_kb.process_candidates", fake_process)
    monkeypatch.setattr("app.services.langgraph_runtime.semi_kb.publish_auxiliary_candidates", fake_aux)

    engine = RoundGraphEngine(orchestrator)
    result = await engine.partial_publish(_partial_state(run_id, 1, outputs, {}))
    assert result["success"] is True
    assert calls["precheck"] == 1      # 顶层整批一次通过
    assert calls["process"] == 1       # 只跑一次权威门禁
    assert calls["batch_flag"] is True  # 透传「整批已预检」，下游跳过重复预检
    quarantine = round_directory(run_id, 1) / "quarantine"
    assert not quarantine.exists() or not list(quarantine.glob("semantic-*.json"))


# --- execute_agent 网络重试的有界退避（Fix 4）------------------------------

@pytest.mark.asyncio
async def test_execute_agent_backoff_on_network_retry(authenticated, monkeypatch):
    authenticated.put("/api/credentials", json={"kind": "llm_api_key", "value": "test-key"})
    with SessionLocal() as db:
        user = db.scalar(select(User)); run = create_run(db, user.id, _payload(1))
        run_id = run.id; agent_id = run.agents[0].id

    calls = {"complete": 0}
    sleeps: list[float] = []

    async def fake_complete(*_args, **_kwargs):
        calls["complete"] += 1
        if calls["complete"] == 1:
            raise ExternalServiceError("connection reset")
        return "{}"

    def fake_validate(_parsed, **_kwargs):
        return {"semantic_files": [], "business_files": [], "simulation_files": [],
                "knowledge_files": [], "rule_files": [], "mapping_files": []}

    async def fake_sleep(duration):
        sleeps.append(duration)

    monkeypatch.setattr("app.services.orchestrator.llm_service.complete", fake_complete)
    monkeypatch.setattr("app.services.orchestrator.validate_and_store_agent_output", fake_validate)
    monkeypatch.setattr("app.services.orchestrator.asyncio.sleep", fake_sleep)

    output = await orchestrator.execute_agent(run_id, agent_id, 1, "test-key", {}, {"max_retries": 2})
    assert output is not None
    assert calls["complete"] == 2          # 网络失败一次后重试成功
    assert len(sleeps) == 1                 # 网络重试前有界退避恰一次
    assert 0.0 <= sleeps[0] <= 4.4          # 上限有界（min(4.0, ...) + jitter<0.4）


@pytest.mark.asyncio
async def test_execute_agent_no_backoff_on_validation_retry(authenticated, monkeypatch):
    authenticated.put("/api/credentials", json={"kind": "llm_api_key", "value": "test-key"})
    with SessionLocal() as db:
        user = db.scalar(select(User)); run = create_run(db, user.id, _payload(1))
        run_id = run.id; agent_id = run.agents[0].id

    calls = {"complete": 0, "validate": 0}
    sleeps: list[float] = []

    async def fake_complete(*_args, **_kwargs):
        calls["complete"] += 1
        return "{}"

    def fake_validate(_parsed, **_kwargs):
        calls["validate"] += 1
        if calls["validate"] == 1:
            raise ValueError("机器校验失败")
        return {"semantic_files": [], "business_files": [], "simulation_files": [],
                "knowledge_files": [], "rule_files": [], "mapping_files": []}

    async def fake_sleep(duration):
        sleeps.append(duration)

    monkeypatch.setattr("app.services.orchestrator.llm_service.complete", fake_complete)
    monkeypatch.setattr("app.services.orchestrator.validate_and_store_agent_output", fake_validate)
    monkeypatch.setattr("app.services.orchestrator.asyncio.sleep", fake_sleep)

    output = await orchestrator.execute_agent(run_id, agent_id, 1, "test-key", {}, {"max_retries": 2})
    assert output is not None
    assert calls["complete"] == 2 and calls["validate"] == 2
    assert sleeps == []  # 校验类重试不退避（避免把纠错提示越拖越久）
