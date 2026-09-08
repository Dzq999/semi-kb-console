from __future__ import annotations

import asyncio
import hashlib
import json
import re
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
    gate_error_class: str | None
    repair_attempt: int
    repair_successes: int
    repair_failures: int
    repair_made_progress: bool
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

    @staticmethod
    def classify_gate_error(error: str | None) -> str:
        """Classify a gate failure without weakening security controls.

        Only semantic completeness failures are sent back to the model.  Safety,
        provenance and executable-query violations are isolated immediately.
        Ontology reasoning / structural conflicts that an LLM cannot repair are
        classified ``isolate`` so they skip the futile repair loop and go
        straight to ``partial_publish``'s surgical quarantine.
        """
        text = str(error or "").casefold()
        hard = ("sensitive", "敏感", "凭据", "bearer", "危险", "只读", "insert", "delete", "非法 iri", "悬空引用", "来源伪造", "source_ref 伪造")
        if any(token in text for token in hard):
            return "hard"
        # 结构/推理不可满足类失败——LLM 修不了。高精度、避免误伤 repairable：
        # 这些串来自外部 apply_semantic_changeset.py 的 reasoner stdout，token 应以
        # 一次真实推理失败的 backend.log/事件输出为准做校准（见 robust-waddling-church.md 2a）。
        isolate_tokens = ("推理", "reasoning", "不一致", "inconsisten", "unsatisf", "不可满足", "disjoint", "互斥", "clash", "本体冲突", "结构不兼容", "incompatible", "cycle", "循环依赖", "成环")
        if any(token in text for token in isolate_tokens):
            return "isolate"
        repairable = ("缺少", "不完整", "引用", "restriction", "diagnostic", "possiblecause", "possible cause", "hasdiagnosticaction", "haspossiblecause", "门禁失败", "校验失败", "candidate", "候选")
        if any(token in text for token in repairable):
            return "repairable"
        return "system"

    @staticmethod
    def _is_baseline_gate_failure(error: str, outputs: list[dict[str, Any]]) -> bool:
        """Treat a focus-node SHACL error as baseline-only only when the node
        cannot be found in the current round's candidate payloads.

        Older logic classified every report containing ``Focus Node`` as a
        baseline failure.  Candidate-local SHACL violations consequently
        quarantined otherwise valid business and simulation files.  This check
        keeps the conservative baseline behavior while preserving independent
        candidate publication.
        """
        text = str(error or "")
        if "constraint violation" not in text.casefold() or "focus node" not in text.casefold():
            return False
        candidate_text_parts: list[str] = []
        for output in outputs:
            for path in output.get("semantic_files") or []:
                try:
                    candidate_text_parts.append(Path(str(path)).read_text(encoding="utf-8", errors="ignore"))
                except OSError:
                    continue
        candidate_text = "\n".join(candidate_text_parts).casefold()
        focus_nodes = re.findall(r"Focus Node:\s*(<[^>]+>|[^\s\\r\\n]+)", text, flags=re.IGNORECASE)
        focus_nodes = [token.strip("<>").casefold() for token in focus_nodes if token.strip("<>")]
        if focus_nodes:
            return not any(token in candidate_text for token in focus_nodes)
        return True

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
        from .reports import augment_gap
        augment_gap(gap, state["run_id"])
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
        artifacts["candidate_counts"] = {key: len(value) for key, value in candidates.items()}
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
            error_text = str(exc)
            with SessionLocal() as db:
                self.controller.emit(db, state["run_id"], "gate_failed", f"第 {state['round_number']} 轮候选门禁失败：{error_text}", {"round": state["round_number"], "error": error_text, "error_class": self.classify_gate_error(error_text)}, "warning")
            return {"gate_error": error_text, "gate_error_class": self.classify_gate_error(error_text)}

    def route_validation(self, state: RoundGraphState) -> str:
        if not state.get("gate_error"):
            return "owl_shacl_reasoning"
        cls = state.get("gate_error_class")
        if cls == "hard":
            return "fail_gate"
        # 结构/推理类失败 LLM 修不了：跳过「repair→重跑整图门禁」的约 10 分钟空耗，
        # 直接进 partial_publish，由 isolate() 外科式隔离坏候选（好候选照常发布）。
        if cls == "isolate":
            return "partial_publish"
        config = state.get("config") or {}
        auto_repair = bool(config.get("auto_repair", True))
        limit = int(config.get("max_auto_repair_attempts", settings.graph_repair_attempts))
        if bool(config.get("repair_follow_failure_threshold", True)):
            limit = int(config.get("max_consecutive_round_failures", limit))
        if auto_repair and cls == "repairable" and int(state.get("repair_attempt", 0)) < max(0, min(10, limit)):
            return "repair_candidates"
        return "partial_publish" if cls in ("repairable", "isolate") else "fail_gate"

    def route_after_repair(self, state: RoundGraphState) -> str:
        # repair 有成功产出才值得重跑整图门禁；0 成功时输入未变，重跑必然再失败——
        # 直接进 partial_publish，省下约 10 分钟的重复整图门禁。
        return "cross_validation" if state.get("repair_made_progress") else "partial_publish"

    async def repair_candidates(self, state: RoundGraphState) -> dict:
        await self._control(state, "candidate_repair", 55)
        attempt = int(state.get("repair_attempt", 0)) + 1
        error = str(state.get("gate_error") or "门禁校验失败")
        context = {
            "repair": True,
            "repair_attempt": attempt,
            "gate_error": error[:6000],
            "instruction": "仅修复门禁指出的问题；保留合法内容和来源，不编造现场数据，不输出解释，只返回完整 JSON。",
        }
        with SessionLocal() as db:
            run = db.get(Run, state["run_id"])
            agent_ids = [agent.id for agent in run.agents] if run else []
        configs = (state.get("config") or {}).get("agents") or []
        # Restrict repair calls to Agents that produced the affected artifact
        # *and* the focus entity named by the validator.  Category-only matching
        # used to send every semantic Agent through a second/third LLM call even
        # when SHACL identified one specific candidate.  Reading the small local
        # candidate JSON files is cheap and makes repair proportional to the
        # actual defect.  If a validator cannot expose a focus entity, retain a
        # bounded category fallback rather than fan out to all ten Agents.
        error_lower = error.casefold()
        category_keys = [("仿真", "simulation_files"), ("经营", "business_files"), ("规则", "rule_files"), ("知识", "knowledge_files"), ("knowledge", "knowledge_files"), ("rule", "rule_files"), ("simulation", "simulation_files"), ("business", "business_files"), ("diagnostic", "semantic_files"), ("possiblecause", "semantic_files"), ("possible cause", "semantic_files"), ("shacl", "semantic_files"), ("owl", "semantic_files")]
        focus_tokens: list[str] = []
        for match in re.findall(r"Focus Node:\s*(<[^>]+>|[^\s\\r\\n]+)", error, flags=re.IGNORECASE):
            token = match.strip().strip("<>")
            if token:
                focus_tokens.extend((token, token.rsplit(":", 1)[-1], token.rsplit("/", 1)[-1]))
        focus_tokens = sorted({token for token in focus_tokens if len(token) >= 6}, key=len, reverse=True)
        selected_indexes: list[int] = []
        for index, output in enumerate(state.get("outputs") or []):
            if index >= len(agent_ids):
                continue
            key = next((file_key for token, file_key in category_keys if token in error_lower), None)
            if key is None:
                selected_indexes.append(index)
                continue
            files = [Path(str(path)) for path in (output.get(key) or [])]
            if not files:
                continue
            if not focus_tokens:
                selected_indexes.append(index)
                continue
            try:
                candidate_text = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in files if path.is_file())
            except OSError:
                candidate_text = ""
            if any(token in candidate_text for token in focus_tokens):
                selected_indexes.append(index)
        if not selected_indexes:
            # A malformed/opaque validator message still gets a chance to repair,
            # but never causes an unbounded ten-Agent fan-out. Prefer the first
            # three Agents that produced the affected category.
            key = next((file_key for token, file_key in category_keys if token in error_lower), None)
            selected_indexes = [
                index for index, output in enumerate(state.get("outputs") or [])
                if index < len(agent_ids) and (key is None or output.get(key))
            ][:3]
        if not selected_indexes:
            selected_indexes = list(range(min(3, len(agent_ids))))

        async def repair_one(index: int, agent_id: str) -> tuple[int, dict | None]:
            cfg = dict(configs[index] if index < len(configs) else {})
            cfg["repair_context"] = context
            cfg["input_hash"] = hashlib.sha256(json.dumps({"run": state["run_id"], "round": state["round_number"], "agent": agent_id, "repair": context}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            with SessionLocal() as db:
                run = db.get(Run, state["run_id"])
                key = user_api_key(db, run.user_id) if run else None
            if not key:
                return index, None
            try:
                return index, await self.controller.execute_agent(state["run_id"], agent_id, state["round_number"], key, state.get("gap") or {}, cfg)
            except Exception as exc:
                with SessionLocal() as db:
                    self.controller.emit(db, state["run_id"], "repair_failed", f"Agent {agent_id} 自动返修失败：{exc}", {"round": state["round_number"], "agent_id": agent_id, "attempt": attempt}, "warning")
                return index, None

        repaired_results = await asyncio.gather(*(repair_one(index, agent_ids[index]) for index in selected_indexes))
        original_outputs = list(state.get("outputs") or [])
        outputs = list(original_outputs)
        successes = 0
        for index, item in repaired_results:
            if item:
                successes += 1
                if index < len(outputs):
                    outputs[index] = item
                else:
                    outputs.append(item)
        if successes:
            with SessionLocal() as db:
                self.controller.emit(db, state["run_id"], "repair_completed", f"第 {state['round_number']} 轮已完成第 {attempt} 次自动返修（{successes}/{len(selected_indexes)} 个 Agent）", {"round": state["round_number"], "attempt": attempt, "successes": successes, "selected_agents": len(selected_indexes)}, "info")
        else:
            with SessionLocal() as db:
                self.controller.emit(db, state["run_id"], "repair_failed", f"第 {state['round_number']} 轮第 {attempt} 次自动返修未产生可用结果", {"round": state["round_number"], "attempt": attempt}, "warning")
        return {"outputs": outputs or state.get("outputs") or [], "repair_attempt": attempt, "repair_successes": int(state.get("repair_successes", 0)) + successes, "repair_failures": int(state.get("repair_failures", 0)) + (len(selected_indexes) - successes), "repair_made_progress": successes > 0, "gate_error": error, "gate_error_class": state.get("gate_error_class")}

    async def fail_gate(self, state: RoundGraphState) -> dict:
        raise SemiKbError(state.get("gate_error") or "候选门禁失败")

    async def partial_publish(self, state: RoundGraphState) -> dict:
        """Publish a single filtered batch after bounded repair attempts.

        We first isolate incompatible semantic candidates with logarithmic
        prechecks, then invoke the expensive full publish gate exactly once for
        the remaining batch.  This avoids the previous per-Agent/per-file
        ``apply_semantic_changeset`` explosion.
        """
        await self._control(state, "partial_publish", 82)
        quarantined = list(state.get("quarantined_files") or [])
        rejected_semantic: list[Path] = []
        rejected_reasons: dict[str, str] = {}
        # 因预算耗尽而被隔离的候选：它们从未单独预检过，不能在下面的兜底里逐个再跑
        # process_candidates（那会重新引入被封顶的昂贵逐文件门禁）——直接隔离。
        budget_exhausted_paths: set[str] = set()
        outputs = state.get("outputs") or []
        all_files = round_candidate_files(outputs)
        semantic = list(all_files["semantic"])

        # 仅当「整批」作为一个组通过整图预检时置真。二分过程中即便每个叶子单独通过、
        # 整批却因交互而失败(top 检查失败)也不置真——这样传给 process_candidates 的
        # semantic_batch_prechecked 才严格等价于「这一整批刚通过整图 --check」,可安全跳过
        # 下游重复预检；否则退回让下游自己再检一遍。
        top_batch_ok = {"value": False}
        # 隔离预检预算：每次 semantic_precheck_sources = 一次约 10 分钟的整图 --check。
        # 无封顶时递归二分最坏 ~2N−1 次。用「次数 + 墙钟」双闸把最坏扇出关住；
        # 预算耗尽即把未证明的候选保守隔离（quarantine，永不发布）——不削弱发布门禁。
        cfg = state.get("config") or {}
        budget = {
            "used": 0,
            "max": int(cfg.get("partial_isolate_max_prechecks", settings.partial_isolate_max_prechecks)),
            "deadline": time.monotonic() + int(cfg.get("partial_isolate_deadline_seconds", settings.partial_isolate_deadline_seconds)),
            "exhausted": False,
        }

        async def isolate(group: list[Path], top: bool = False) -> list[Path]:
            if not group:
                return []
            # 预算闸门：放在预检之前，证明不了就一个都不发（保守隔离），确保封住调用次数与墙钟。
            if budget["exhausted"] or budget["used"] >= budget["max"] or time.monotonic() >= budget["deadline"]:
                first = not budget["exhausted"]
                deadline_hit = time.monotonic() >= budget["deadline"]
                budget["exhausted"] = True
                for path in group:
                    rejected_semantic.append(path)
                    rejected_reasons[str(path)] = "隔离预检预算耗尽，未证明的候选按保守策略隔离"
                    budget_exhausted_paths.add(str(path))
                if first:
                    with SessionLocal() as db:
                        self.controller.emit(db, state["run_id"], "isolate_budget_exhausted", f"第 {state['round_number']} 轮隔离预检预算耗尽，剩余未证明候选按保守策略隔离", {"round": state["round_number"], "checks_used": budget["used"], "max": budget["max"], "deadline_hit": deadline_hit}, "warning")
                return []
            budget["used"] += 1
            check = await semi_kb.semantic_precheck_sources(group)
            if check.get("exit_code", 0) == 0 or check.get("passed", False):
                if top:
                    top_batch_ok["value"] = True
                return group
            if len(group) == 1:
                path = group[0]
                rejected_semantic.append(path)
                rejected_reasons[str(path)] = str(check.get("output") or "")
                return []
            # 顺序执行：_candidate_lock 本就串行化每次预检，asyncio.gather 零 wall-clock 收益；
            # 顺序化让预算确定，右子树在预算耗尽后能立即短路。
            midpoint = max(1, len(group) // 2)
            left = await isolate(group[:midpoint])
            right = await isolate(group[midpoint:])
            return left + right

        gate_text = str(state.get("gate_error") or "")
        # A SHACL report that points at pre-existing focus nodes is a baseline
        # failure, not a candidate-local defect.  Do not launch probe commands
        # (or model repairs) for every file in that case; quarantine the new
        # semantic batch and continue with independent artifacts immediately.
        baseline_gate = self._is_baseline_gate_failure(gate_text, outputs)
        valid_semantic = [] if baseline_gate else await isolate(semantic, top=True)
        if baseline_gate:
            for path in semantic:
                rejected_semantic.append(path)
                rejected_reasons[str(path)] = gate_text
        valid_set = {str(path) for path in valid_semantic}
        filtered = {key: list(value) for key, value in all_files.items()}
        filtered["semantic"] = [path for path in semantic if str(path) in valid_set]
        # Knowledge, rule and article artifacts have independent contracts; publish
        # them in one local transaction without invoking the expensive semantic gate.
        auxiliary = await semi_kb.publish_auxiliary_candidates({"knowledge": all_files["knowledge"], "rules": all_files["rules"], "articles": all_files["articles"]})
        published = int(sum((auxiliary.get("accepted_candidates") or {}).values()))
        for item in auxiliary.get("quarantined_candidates") or []:
            path = Path(str(item.get("path") or ""))
            if path.is_file():
                quarantine = path.parent / "quarantine"; quarantine.mkdir(exist_ok=True); target = quarantine / path.name
                try: shutil.move(str(path), str(target)); quarantined.append(str(target))
                except OSError: target = path
                with SessionLocal() as db:
                    self.controller.emit(db, state["run_id"], "candidate_quarantined", f"候选已隔离：{path.name}；{item.get('reason', '')}", {"round": state["round_number"], "path": str(target), "error": item.get("reason", "")}, "warning")
        filtered["knowledge"] = []; filtered["rules"] = []; filtered["articles"] = []
        result: dict[str, Any] = auxiliary
        try:
            if not any(filtered[key] for key in ("semantic", "business", "simulation", "mappings")) and not rejected_semantic:
                return {"validation": {**auxiliary, "partial": True, "published": bool(published), "repair_attempts": int(state.get("repair_attempt", 0))}, "quarantined_files": list(dict.fromkeys(quarantined)), "gate_error": None, "success": published > 0}
            if not filtered["semantic"] and rejected_semantic:
                raise RuntimeError("语义候选预检全部失败，进入候选级兜底")
            # 若整批语义候选刚在 isolate 顶层通过整图预检、且未发生任何隔离,则本批与
            # filtered["semantic"] 完全一致,把这一事实透传给 process_candidates,让它跳过
            # 重复的批量/语义预检,直达权威 apply 门禁。
            batch_prechecked = bool(top_batch_ok["value"] and not rejected_semantic and filtered["semantic"])
            result = await semi_kb.process_candidates(filtered, publish=True, semantic_batch_prechecked=batch_prechecked)
            published += int(sum((result.get("accepted_candidates") or {}).values())) or sum(len(filtered[key]) for key in ("semantic", "business", "simulation"))
            # Candidates rejected by the isolated precheck are quarantined only
            # after the valid batch has been published.
            for path in rejected_semantic:
                if path.is_file():
                    quarantine = path.parent / "quarantine"; quarantine.mkdir(exist_ok=True); target = quarantine / path.name
                    try: shutil.move(str(path), str(target)); quarantined.append(str(target))
                    except OSError: pass
                    with SessionLocal() as db:
                        reason = rejected_reasons.get(str(path), "候选级门禁未通过")
                        self.controller.emit(db, state["run_id"], "candidate_quarantined", f"候选已隔离：{path.name}；{reason[-500:]}", {"round": state["round_number"], "path": str(target), "error": reason[-3000:]}, "warning")
        except Exception as exc:
            # If precheck rejected every semantic file, try the application
            # service's own candidate-level validation once. This preserves
            # compatibility with lightweight adapters and can still salvage a
            # valid candidate that only fails when isolated from its siblings.
            # A command name alone does not prove that the existing baseline is
            # broken: candidate-local SHACL errors also come from
            # ``semantic_validate.py``. Only skip single-candidate salvage when
            # the report explicitly identifies a pre-existing focus node.
            baseline_failure = any(
                "constraint violation" in reason.casefold() and "focus node" in reason.casefold()
                for reason in rejected_reasons.values()
            )
            for path in rejected_semantic:
                if not path.is_file():
                    continue
                exhausted_reject = str(path) in budget_exhausted_paths
                if baseline_failure or exhausted_reject:
                    single_exc = "隔离预检预算耗尽，未证明的候选按保守策略隔离" if exhausted_reject else "现有基线全链门禁失败，跳过重复发布尝试"
                    quarantine = path.parent / "quarantine"; quarantine.mkdir(exist_ok=True); target = quarantine / path.name
                    try: shutil.move(str(path), str(target)); quarantined.append(str(target))
                    except OSError: pass
                    with SessionLocal() as db:
                        self.controller.emit(db, state["run_id"], "candidate_quarantined", f"候选已隔离：{path.name}；{single_exc}", {"round": state["round_number"], "path": str(target), "error": rejected_reasons.get(str(path), single_exc)[-3000:]}, "warning")
                    continue
                single = {"semantic": [path], "business": [], "simulation": [], "knowledge": [], "rules": [], "articles": []}
                try:
                    await semi_kb.process_candidates(single, publish=True)
                    published += 1
                except Exception as single_exc:
                    quarantine = path.parent / "quarantine"; quarantine.mkdir(exist_ok=True); target = quarantine / path.name
                    try: shutil.move(str(path), str(target)); quarantined.append(str(target))
                    except OSError: pass
                    with SessionLocal() as db:
                        self.controller.emit(db, state["run_id"], "candidate_quarantined", f"候选已隔离：{path.name}；{single_exc}", {"round": state["round_number"], "path": str(target), "error": str(single_exc)}, "warning")
            # Non-semantic candidates (e.g. a malformed simulation/rule) are
            # isolated individually only after the single filtered batch fails.
            for key in ("business", "simulation", "knowledge", "rules"):
                for path in filtered[key]:
                    if baseline_gate:
                        if path.is_file():
                            quarantine = path.parent / "quarantine"; quarantine.mkdir(exist_ok=True); target = quarantine / path.name
                            try: shutil.move(str(path), str(target)); quarantined.append(str(target))
                            except OSError: pass
                            with SessionLocal() as db:
                                self.controller.emit(db, state["run_id"], "candidate_quarantined", f"候选已隔离：{path.name}；现有基线门禁失败，跳过重复校验", {"round": state["round_number"], "path": str(target), "error": gate_text[-3000:]}, "warning")
                        continue
                    single = {"semantic": [], "business": [], "simulation": [], "knowledge": [], "rules": [], "articles": []}
                    single[key].append(path)
                    try:
                        await semi_kb.process_candidates(single, publish=True)
                        published += 1
                    except Exception as single_exc:
                        quarantine = path.parent / "quarantine"; quarantine.mkdir(exist_ok=True)
                        target = quarantine / path.name
                        try: shutil.move(str(path), str(target)); quarantined.append(str(target))
                        except OSError: pass
                        with SessionLocal() as db:
                            self.controller.emit(db, state["run_id"], "candidate_quarantined", f"候选已隔离：{path.name}；{single_exc}", {"round": state["round_number"], "path": str(target), "error": str(single_exc)}, "warning")
            if published == 0:
                raise SemiKbError(str(exc))
            result = {**auxiliary, "published": True, "partial_error": str(exc), "accepted_candidates": {**(auxiliary.get("accepted_candidates") or {}), "semantic": 0, "business": 0, "simulation": 0}, "partial_published_total": published}
        if published <= 0:
            raise SemiKbError(state.get("gate_error") or "自动返修后没有候选通过门禁")
        quarantined = list(dict.fromkeys(quarantined))
        last_validation = dict(result)
        last_validation["partial"] = True
        last_validation["published"] = True
        last_validation["repair_attempts"] = int(state.get("repair_attempt", 0))
        return {"validation": last_validation, "quarantined_files": quarantined, "gate_error": None, "success": True}

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
            partial = bool((state.get("validation") or {}).get("partial"))
            delta = {key: int(after.get(key, 0)) - int(before.get(key, 0)) for key in set(before) | set(after) if isinstance(before.get(key, 0), (int, float)) and isinstance(after.get(key, 0), (int, float))}
            has_change = any(value > 0 for value in delta.values())
            no_change_status = bool((state.get("config") or {}).get("publish_changes")) and not has_change
            row.status = "completed_partial" if partial else ("completed_no_change" if no_change_status else "completed"); row.current_stage = "completed"; row.completed_at = datetime.now(timezone.utc)
            started_at = row.started_at or row.completed_at
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=timezone.utc)
            row.duration_seconds = round((row.completed_at - started_at).total_seconds(), 3)
            row.metrics_after_json = json.dumps(after, ensure_ascii=False)
            validation = dict(state.get("validation") or {})
            validation["repair_attempts"] = int(state.get("repair_attempt", 0))
            validation["repair_successes"] = int(state.get("repair_successes", 0))
            validation["repair_failures"] = int(state.get("repair_failures", 0))
            row.validation_json = json.dumps(validation, ensure_ascii=False)
            artifacts = dict(state.get("artifacts") or {})
            artifacts["repair_attempts"] = int(state.get("repair_attempt", 0))
            artifacts["repair_successes"] = int(state.get("repair_successes", 0))
            artifacts["repair_failures"] = int(state.get("repair_failures", 0))
            artifacts["published"] = bool(validation.get("published"))
            artifacts["partial"] = partial
            row.artifacts_json = json.dumps(artifacts, ensure_ascii=False)
            row.quarantined_files_json = json.dumps(state.get("quarantined_files") or [], ensure_ascii=False)
            # 两侧平衡反馈：two-side × three-dim 的本轮增量。某侧权重>0 却该维度未产出(delta≤0)，
            # 记入 balance_shortfall，透传给 compute_round_direction 写进下一轮方向（后置反馈、非硬门禁）。
            focus = (state.get("config") or {}).get("system_focus") or {}
            mfg_weight = int(focus.get("manufacturing", 40)); erp_weight = int(focus.get("erp", 60))
            both_required = mfg_weight > 0 and erp_weight > 0
            balance_shortfall: list[dict] = []
            if both_required:
                _dim_keys = {
                    "class": ("class_system_manufacturing", "class_system_erp"),
                    "property": ("property_system_manufacturing", "property_system_erp"),
                    "relation": ("relation_system_manufacturing", "relation_system_erp"),
                }
                for dim, (mfg_key, erp_key) in _dim_keys.items():
                    if mfg_weight > 0 and int(delta.get(mfg_key, 0)) <= 0:
                        balance_shortfall.append({"side": "manufacturing", "dimension": dim})
                    if erp_weight > 0 and int(delta.get(erp_key, 0)) <= 0:
                        balance_shortfall.append({"side": "erp", "dimension": dim})
            # 本轮成功 → 产出下一轮结构化优化方向，下一轮 gap_analysis 的 augment_gap 读回注入。
            from .reports import compute_round_direction
            row.next_direction_json = json.dumps(compute_round_direction(semi_kb.feature_gap(), validation, delta, balance_shortfall=balance_shortfall, system_focus=focus), ensure_ascii=False)
            run.metrics_after_json = row.metrics_after_json; run.progress = 100; run.heartbeat_at = datetime.now(timezone.utc)
            db.commit()
            self.controller.emit(db, state["run_id"], "round_completed", f"第 {state['round_number']} 轮{'部分完成' if partial else '完成'}，准备下一轮", {"round": state["round_number"], "duration_seconds": row.duration_seconds, "delta": delta, "published": (state.get("validation") or {}).get("published", False), "partial": partial, "repair_attempts": int(state.get("repair_attempt", 0)), "repair_successes": int(state.get("repair_successes", 0)), "engine": "langgraph"})
        return {"success": True}

    def build(self):
        graph = StateGraph(RoundGraphState)
        nodes = {
            "gap_analysis": self.gap_analysis, "parallel_research": self.parallel_research,
            "evidence_extraction": self.evidence_extraction, "semantic_modeling": self.semantic_modeling,
            "cross_validation": self.cross_validation, "repair_candidates": self.repair_candidates,
            "fail_gate": self.fail_gate, "partial_publish": self.partial_publish, "owl_shacl_reasoning": self.owl_shacl_reasoning,
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
            "repair_candidates": "repair_candidates", "fail_gate": "fail_gate", "partial_publish": "partial_publish", "owl_shacl_reasoning": "owl_shacl_reasoning",
        })
        graph.add_conditional_edges("repair_candidates", self.route_after_repair, {
            "cross_validation": "cross_validation", "partial_publish": "partial_publish",
        })
        graph.add_edge("partial_publish", "finalize")
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
            "validation": {}, "artifacts": {}, "gate_error": None, "gate_error_class": None, "repair_attempt": 0, "repair_successes": 0, "repair_failures": 0, "repair_made_progress": False,
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
            with SessionLocal() as db:
                row = db.scalar(select(RunRound).where(RunRound.run_id == run_id, RunRound.round_number == round_number))
                if row and row.status not in {"completed", "completed_partial", "completed_no_change"}:
                    row.status = "cancelled"
                    row.current_stage = "cancelled"
                    row.completed_at = datetime.now(timezone.utc)
                    row.error = "轮次在部分发布阶段被用户停止"
                    row.duration_seconds = round(time.monotonic() - started, 3)
                    db.commit()
            raise
        except Exception as exc:
            with SessionLocal() as db:
                row = db.scalar(select(RunRound).where(RunRound.run_id == run_id, RunRound.round_number == round_number))
                if row:
                    row.status = "failed"; row.error = str(exc); row.completed_at = datetime.now(timezone.utc)
                    row.duration_seconds = round(time.monotonic() - started, 3); db.commit()
                self.controller.emit(db, run_id, "round_failed", f"第 {round_number} 轮失败：{exc}", {"round": round_number, "error": str(exc), "engine": "langgraph"}, "error")
            return False
