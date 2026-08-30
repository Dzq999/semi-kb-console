from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import select

from ..config import settings
from ..db import SessionLocal
from ..models import AgentIteration, AgentRun, Run, RunRound
from .checkpoints import checkpoint_runtime
from .llm import user_api_key
from .semantic_pipeline import round_candidate_files, round_directory
from .semi_kb import SemiKbError, semi_kb


class RoundGraphState(TypedDict, total=False):
    run_id: str
    round_number: int
    config: dict[str, Any]
    gap: dict[str, Any]
    outputs: list[dict[str, Any]]
    validation: dict[str, Any]
    artifacts: dict[str, Any]
    gate_error: str | None
    repair_attempt: int
    quarantined_files: list[str]
    success: bool


class AgentGraphState(TypedDict, total=False):
    run_id: str
    round_number: int
    agent_id: str
    agent_config: dict[str, Any]
    gap: dict[str, Any]
    input_hash: str
    output: dict[str, Any] | None
    error: str | None


class RoundGraphEngine:
    """A checkpointed graph for exactly one round; the outer controller owns the endless loop."""

    def __init__(self, controller: Any) -> None:
        self.controller = controller

    def _stage(self, run_id: str, round_number: int, stage: str, progress: float) -> None:
        with SessionLocal() as db:
            run = db.get(Run, run_id)
            row = db.scalar(select(RunRound).where(RunRound.run_id == run_id, RunRound.round_number == round_number))
            if not run or not row:
                raise RuntimeError("运行或轮次不存在")
            run.current_stage = stage
            run.progress = progress
            run.heartbeat_at = datetime.now(timezone.utc)
            row.current_stage = stage
            attempts = json.loads(row.node_attempts_json or "{}")
            attempts[stage] = int(attempts.get(stage, 0)) + 1
            row.node_attempts_json = json.dumps(attempts, ensure_ascii=False)
            db.commit()
            self.controller.emit(db, run_id, "graph_node_started", f"第 {round_number} 轮 · {stage}", {"stage": stage, "round": round_number, "attempt": attempts[stage], "engine": "langgraph"})

    async def _control(self, state: RoundGraphState, stage: str, progress: float) -> None:
        await self.controller._pause_point(state["run_id"])
        self._stage(state["run_id"], state["round_number"], stage, progress)

    async def gap_analysis(self, state: RoundGraphState) -> dict:
        await self._control(state, "gap_analysis", 0)
        gap = await semi_kb.status()
        round_directory(state["run_id"], state["round_number"]).joinpath("gap-analysis.json").write_text(json.dumps(gap, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"gap": gap, "artifacts": dict(state.get("artifacts") or {})}

    def _agent_graph(self):
        graph = StateGraph(AgentGraphState)

        async def prepare(agent_state: AgentGraphState) -> dict:
            stable = json.dumps({
                "run_id": agent_state["run_id"], "round": agent_state["round_number"],
                "agent_id": agent_state["agent_id"], "config": agent_state.get("agent_config") or {},
                "gap_hash": hashlib.sha256(json.dumps(agent_state.get("gap") or {}, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
            }, ensure_ascii=False, sort_keys=True)
            return {"input_hash": hashlib.sha256(stable.encode()).hexdigest()}

        async def execute(agent_state: AgentGraphState) -> dict:
            with SessionLocal() as db:
                agent = db.get(AgentRun, agent_state["agent_id"])
                run = db.get(Run, agent_state["run_id"])
                api_key = user_api_key(db, run.user_id) if run else None
            if not agent or not api_key:
                return {"output": None, "error": "Agent或模型凭据不存在"}
            config = dict(agent_state.get("agent_config") or {})
            config["input_hash"] = agent_state["input_hash"]
            try:
                output = await self.controller.execute_agent(
                    agent_state["run_id"], agent_state["agent_id"], agent_state["round_number"], api_key,
                    agent_state.get("gap") or {}, config,
                )
                return {"output": output, "error": None if output else "Agent执行失败"}
            except Exception as exc:
                return {"output": None, "error": str(exc)}

        async def finalize(agent_state: AgentGraphState) -> dict:
            with SessionLocal() as db:
                iteration = db.scalar(select(AgentIteration).where(
                    AgentIteration.agent_id == agent_state["agent_id"],
                    AgentIteration.round_number == agent_state["round_number"],
                ))
                if iteration:
                    iteration.input_hash = agent_state.get("input_hash")
                    db.commit()
            return {}

        graph.add_node("prepare", prepare)
        graph.add_node("execute", execute)
        graph.add_node("finalize", finalize)
        graph.add_edge(START, "prepare")
        graph.add_edge("prepare", "execute")
        graph.add_edge("execute", "finalize")
        graph.add_edge("finalize", END)
        return graph.compile(checkpointer=checkpoint_runtime.saver)

    async def parallel_research(self, state: RoundGraphState) -> dict:
        await self._control(state, "parallel_research", 12.5)
        with SessionLocal() as db:
            run = db.get(Run, state["run_id"])
            agent_ids = [agent.id for agent in run.agents] if run else []
        configs = (state.get("config") or {}).get("agents") or []
        graph = self._agent_graph()

        async def invoke(index: int, agent_id: str) -> dict:
            agent_state: AgentGraphState = {
                "run_id": state["run_id"], "round_number": state["round_number"], "agent_id": agent_id,
                "agent_config": configs[index] if index < len(configs) else {}, "gap": state.get("gap") or {},
            }
            config = checkpoint_runtime.config(state["run_id"], state["round_number"], f"agent:{agent_id}")
            result = await graph.ainvoke(agent_state, config=config)
            checkpoint = await checkpoint_runtime.saver.aget_tuple(config)
            if checkpoint:
                with SessionLocal() as db:
                    iteration = db.scalar(select(AgentIteration).where(
                        AgentIteration.agent_id == agent_id,
                        AgentIteration.round_number == state["round_number"],
                    ))
                    if iteration:
                        iteration.checkpoint_id = str(checkpoint.config.get("configurable", {}).get("checkpoint_id") or "")
                        db.commit()
            return result

        results = await asyncio.gather(*(invoke(index, agent_id) for index, agent_id in enumerate(agent_ids)))
        outputs = [item.get("output") for item in results if item.get("output")]
        if not outputs:
            errors = [item.get("error") for item in results if item.get("error")]
            raise RuntimeError("本轮所有 Agent 均失败" + ("：" + "；".join(errors[:3]) if errors else ""))
        if len(outputs) < len(agent_ids):
            with SessionLocal() as db:
                self.controller.emit(db, state["run_id"], "round_warning", f"第 {state['round_number']} 轮部分 Agent 失败，使用 {len(outputs)}/{len(agent_ids)} 个结果继续", {"round": state["round_number"], "failed": len(agent_ids) - len(outputs)}, "warning")
        return {"outputs": outputs}

    async def evidence_extraction(self, state: RoundGraphState) -> dict:
        await self._control(state, "evidence_extraction", 25)
        count = sum(int(output.get("evidence_count", 0)) for output in state.get("outputs") or [])
        artifacts = dict(state.get("artifacts") or {}); artifacts["evidence_pages"] = count
        with SessionLocal() as db:
            self.controller.emit(db, state["run_id"], "evidence_ready", f"第 {state['round_number']} 轮提取 {count} 个网页正文证据", {"round": state["round_number"], "evidence_pages": count})
        return {"artifacts": artifacts}

    async def semantic_modeling(self, state: RoundGraphState) -> dict:
        await self._control(state, "semantic_modeling", 37.5)
        candidates = round_candidate_files(state.get("outputs") or [])
        artifacts = dict(state.get("artifacts") or {})
        artifacts.update({key: [str(path) for path in value] for key, value in candidates.items()})
        with SessionLocal() as db:
            self.controller.emit(db, state["run_id"], "candidates_ready", f"第 {state['round_number']} 轮生成语义候选 {len(candidates['semantic'])}、经营模型候选 {len(candidates['business'])}、仿真候选 {len(candidates['simulation'])}、知识条目 {len(candidates['knowledge'])}、规则 {len(candidates['rules'])}", {"round": state["round_number"], "semantic": len(candidates["semantic"]), "business": len(candidates["business"]), "simulation": len(candidates["simulation"]), "knowledge": len(candidates["knowledge"]), "rules": len(candidates["rules"])})
        return {"artifacts": artifacts}

    async def cross_validation(self, state: RoundGraphState) -> dict:
        await self._control(state, "cross_validation", 50)
        with SessionLocal() as db:
            row = db.scalar(select(RunRound).where(RunRound.run_id == state["run_id"], RunRound.round_number == state["round_number"]))
            cached = json.loads(row.validation_json or "{}") if row else {}
        if cached.get("_graph_gate_complete"):
            with SessionLocal() as db:
                self.controller.emit(db, state["run_id"], "gate_cache_hit", f"第 {state['round_number']} 轮复用已提交的门禁结果", {"round": state["round_number"], "published": cached.get("published", False)})
            return {"validation": cached, "gate_error": None}
        try:
            validation = await semi_kb.process_candidates(round_candidate_files(state.get("outputs") or []), publish=bool((state.get("config") or {}).get("publish_changes")))
            validation["_graph_gate_complete"] = True
            with SessionLocal() as db:
                row = db.scalar(select(RunRound).where(RunRound.run_id == state["run_id"], RunRound.round_number == state["round_number"]))
                if row:
                    row.validation_json = json.dumps(validation, ensure_ascii=False)
                    row.quarantined_files_json = json.dumps(validation.get("quarantined_candidates") or [], ensure_ascii=False)
                    db.commit()
                for item in validation.get("quarantined_candidates") or []:
                    reason = " ".join(str(item.get("reason") or "未知原因").split())[-220:]
                    self.controller.emit(db, state["run_id"], "candidate_quarantined", f"候选已隔离：{Path(str(item.get('path') or '')).name}；{reason}", {"round": state["round_number"], **item}, "warning")
                self.controller.emit(db, state["run_id"], "cross_validation", f"第 {state['round_number']} 轮交叉验证通过", {"round": state["round_number"], "published": validation.get("published"), "checks": validation.get("checks")})
            return {"validation": validation, "gate_error": None}
        except Exception as exc:
            with SessionLocal() as db:
                self.controller.emit(db, state["run_id"], "gate_failed", f"第 {state['round_number']} 轮候选门禁失败：{exc}", {"round": state["round_number"], "error": str(exc)}, "warning")
            return {"gate_error": str(exc)}

    def route_validation(self, state: RoundGraphState) -> str:
        if not state.get("gate_error"):
            return "owl_shacl_reasoning"
        return "repair_candidates" if int(state.get("repair_attempt", 0)) < settings.graph_repair_attempts else "fail_gate"

    async def repair_candidates(self, state: RoundGraphState) -> dict:
        await self._control(state, "candidate_repair", 55)
        outputs = json.loads(json.dumps(state.get("outputs") or []))
        quarantined = list(state.get("quarantined_files") or [])
        for output in outputs:
            for key in ("semantic_files", "business_files", "simulation_files", "knowledge_files", "rule_files"):
                valid: list[str] = []
                for raw_path in output.get(key) or []:
                    path = Path(raw_path)
                    category = {"semantic_files": "semantic", "business_files": "business", "simulation_files": "simulation", "knowledge_files": "knowledge", "rule_files": "rules"}[key]
                    try:
                        await semi_kb.process_candidates({"semantic": [path] if category == "semantic" else [], "business": [path] if category == "business" else [], "simulation": [path] if category == "simulation" else [], "knowledge": [path] if category == "knowledge" else [], "rules": [path] if category == "rules" else [], "articles": []}, publish=False)
                        valid.append(raw_path)
                    except Exception as exc:
                        quarantine = path.parent / "quarantine"; quarantine.mkdir(exist_ok=True)
                        target = quarantine / path.name
                        if path.is_file(): shutil.move(str(path), str(target))
                        quarantined.append(str(target))
                        with SessionLocal() as db:
                            self.controller.emit(db, state["run_id"], "candidate_quarantined", f"候选已隔离：{path.name}", {"round": state["round_number"], "path": str(target), "error": str(exc)}, "warning")
                output[key] = valid
        if not any(round_candidate_files(outputs)[key] for key in ("semantic", "business", "simulation", "knowledge", "rules")):
            raise SemiKbError("所有结构化候选均未通过门禁，已隔离")
        return {"outputs": outputs, "repair_attempt": int(state.get("repair_attempt", 0)) + 1, "quarantined_files": quarantined, "gate_error": None}

    async def fail_gate(self, state: RoundGraphState) -> dict:
        raise SemiKbError(state.get("gate_error") or "候选门禁失败")

    async def owl_shacl_reasoning(self, state: RoundGraphState) -> dict:
        await self._control(state, "owl_shacl_reasoning", 62.5)
        validation = state.get("validation") or {}
        checks = validation.get("checks") or {}
        check = checks.get("full_publish_gate") or checks.get("semantic_precheck") or checks.get("baseline_gate") or checks.get("candidate_precheck")
        if not check or not check.get("passed", False):
            raise SemiKbError("OWL/SHACL/推理门禁失败或缺少校验结果")
        return {}

    async def business_simulation(self, state: RoundGraphState) -> dict:
        await self._control(state, "business_simulation", 75)
        validation = state.get("validation") or {}
        checks = validation.get("checks") or {}
        check = checks.get("business_simulation")
        if not check and not validation.get("business_candidates") and not validation.get("simulation_candidates"):
            check = checks.get("full_publish_gate") or checks.get("baseline_gate") or checks.get("candidate_precheck")
        if not check or not check.get("passed", False):
            raise SemiKbError("经营模型/仿真门禁失败或缺少校验结果")
        return {}

    async def scenario_article(self, state: RoundGraphState) -> dict:
        await self._control(state, "scenario_article", 87.5)
        count = len(round_candidate_files(state.get("outputs") or [])["articles"])
        artifacts = dict(state.get("artifacts") or {}); artifacts["scenario_articles"] = count
        with SessionLocal() as db:
            self.controller.emit(db, state["run_id"], "articles_ready", f"第 {state['round_number']} 轮形成 {count} 项场景知识产物候选", {"round": state["round_number"], "count": count})
        return {"artifacts": artifacts}

    async def finalize(self, state: RoundGraphState) -> dict:
        await self._control(state, "finalize_round", 95)
        with SessionLocal() as db:
            run = db.get(Run, state["run_id"])
            row = db.scalar(select(RunRound).where(RunRound.run_id == state["run_id"], RunRound.round_number == state["round_number"]))
            if not run or not row: raise RuntimeError("运行或轮次不存在")
            before = json.loads(row.metrics_before_json or "{}")
            after = (await semi_kb.metrics(db, run.user_id))["totals"]
            row.status = "completed"; row.current_stage = "completed"; row.completed_at = datetime.now(timezone.utc)
            started_at = row.started_at or row.completed_at
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=timezone.utc)
            row.duration_seconds = round((row.completed_at - started_at).total_seconds(), 3)
            row.metrics_after_json = json.dumps(after, ensure_ascii=False)
            row.validation_json = json.dumps(state.get("validation") or {}, ensure_ascii=False)
            row.artifacts_json = json.dumps(state.get("artifacts") or {}, ensure_ascii=False)
            row.quarantined_files_json = json.dumps(state.get("quarantined_files") or [], ensure_ascii=False)
            run.metrics_after_json = row.metrics_after_json; run.progress = 100; run.heartbeat_at = datetime.now(timezone.utc)
            db.commit()
            delta = {key: int(after.get(key, 0)) - int(before.get(key, 0)) for key in set(before) | set(after) if isinstance(before.get(key, 0), (int, float)) and isinstance(after.get(key, 0), (int, float))}
            self.controller.emit(db, state["run_id"], "round_completed", f"第 {state['round_number']} 轮完成，准备下一轮", {"round": state["round_number"], "duration_seconds": row.duration_seconds, "delta": delta, "published": (state.get("validation") or {}).get("published", False), "engine": "langgraph"})
        return {"success": True}

    def build(self):
        graph = StateGraph(RoundGraphState)
        nodes = {
            "gap_analysis": self.gap_analysis, "parallel_research": self.parallel_research,
            "evidence_extraction": self.evidence_extraction, "semantic_modeling": self.semantic_modeling,
            "cross_validation": self.cross_validation, "repair_candidates": self.repair_candidates,
            "fail_gate": self.fail_gate, "owl_shacl_reasoning": self.owl_shacl_reasoning,
            "business_simulation": self.business_simulation, "scenario_article": self.scenario_article,
            "finalize": self.finalize,
        }
        for name, node in nodes.items(): graph.add_node(name, node)
        graph.add_edge(START, "gap_analysis")
        graph.add_edge("gap_analysis", "parallel_research")
        graph.add_edge("parallel_research", "evidence_extraction")
        graph.add_edge("evidence_extraction", "semantic_modeling")
        graph.add_edge("semantic_modeling", "cross_validation")
        graph.add_conditional_edges("cross_validation", self.route_validation, {
            "repair_candidates": "repair_candidates", "fail_gate": "fail_gate", "owl_shacl_reasoning": "owl_shacl_reasoning",
        })
        graph.add_edge("repair_candidates", "cross_validation")
        graph.add_edge("owl_shacl_reasoning", "business_simulation")
        graph.add_edge("business_simulation", "scenario_article")
        graph.add_edge("scenario_article", "finalize")
        graph.add_edge("finalize", END)
        return graph.compile(checkpointer=checkpoint_runtime.saver)

    async def execute(self, run_id: str, round_number: int, config: dict, resume: bool = False) -> bool:
        started = time.monotonic()
        with SessionLocal() as db:
            run = db.get(Run, run_id)
            if not run: return False
            row = db.scalar(select(RunRound).where(RunRound.run_id == run_id, RunRound.round_number == round_number))
            if not row:
                before = (await semi_kb.metrics(db, run.user_id))["totals"]
                row = RunRound(run_id=run_id, round_number=round_number, status="running", current_stage="gap_analysis", started_at=datetime.now(timezone.utc), metrics_before_json=json.dumps(before, ensure_ascii=False))
                db.add(row)
            else:
                row.status = "running"; row.resumed_count += 1
            run.checkpoint_thread_id = f"{run_id}:round:{round_number}"
            db.commit()
            self.controller.emit(db, run_id, "round_started" if not resume else "round_resumed", f"第 {round_number} 轮{'恢复' if resume else '开始'}", {"round": round_number, "agent_count": len(run.agents), "publish_changes": bool(config.get("publish_changes")), "engine": "langgraph"})
        graph_config = checkpoint_runtime.config(run_id, round_number)
        initial: RoundGraphState | None = None if resume else {
            "run_id": run_id, "round_number": round_number, "config": config, "gap": {}, "outputs": [],
            "validation": {}, "artifacts": {}, "gate_error": None, "repair_attempt": 0,
            "quarantined_files": [], "success": False,
        }
        try:
            result = await self.build().ainvoke(initial, config=graph_config)
            checkpoint = await checkpoint_runtime.saver.aget_tuple(graph_config)
            with SessionLocal() as db:
                row = db.scalar(select(RunRound).where(RunRound.run_id == run_id, RunRound.round_number == round_number))
                if row and checkpoint:
                    row.checkpoint_id = str(checkpoint.config.get("configurable", {}).get("checkpoint_id") or "")
                    db.commit()
            return bool(result.get("success"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            with SessionLocal() as db:
                row = db.scalar(select(RunRound).where(RunRound.run_id == run_id, RunRound.round_number == round_number))
                if row:
                    row.status = "failed"; row.error = str(exc); row.completed_at = datetime.now(timezone.utc)
                    row.duration_seconds = round(time.monotonic() - started, 3); db.commit()
                self.controller.emit(db, run_id, "round_failed", f"第 {round_number} 轮失败：{exc}", {"round": round_number, "error": str(exc), "engine": "langgraph"}, "error")
            return False
