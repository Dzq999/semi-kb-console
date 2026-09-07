from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal
from ..models import AgentIteration, AgentRun, Run, RunEvent, RunRound
from .llm import ExternalServiceError, LlmEndpoint, llm_service, user_api_key, user_llm_endpoint, web_research
from .semantic_pipeline import prompt_contract, round_candidate_files, round_directory, validate_and_store_agent_output
from .semi_kb import SemiKbError, semi_kb


STAGES = [
    "gap_analysis", "parallel_research", "evidence_extraction", "semantic_modeling",
    "cross_validation", "owl_shacl_reasoning", "business_simulation", "scenario_article",
]


def _json_object(text: str) -> dict:
    stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    start = stripped.find("{")
    if start < 0:
        raise ValueError("模型没有返回 JSON 对象")
    try:
        value, _ = json.JSONDecoder().raw_decode(stripped[start:])
    except json.JSONDecodeError as exc:
        raise ValueError(f"模型返回的 JSON 不完整：{exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("模型输出 JSON 顶层必须是对象")
    return value


def _compact_previous_output(previous: dict) -> dict:
    """Keep enough memory to avoid duplicate proposals without replaying a full prior response."""
    iris: list[str] = []
    for changeset in previous.get("semantic_changesets") or []:
        for values in (changeset.get("additions") or {}).values():
            if isinstance(values, list):
                iris.extend(str(item.get("iri")) for item in values if isinstance(item, dict) and item.get("iri"))
    return {
        "summary": str(previous.get("summary") or "")[:1000],
        "customer_pains": [str(item)[:500] for item in (previous.get("customer_pains") or [])[:12]],
        "proposed_iris": sorted(set(iris))[:500],
        "content_sha256": previous.get("content_sha256"),
    }


class RunOrchestrator:
    def __init__(self) -> None:
        self.tasks: dict[str, asyncio.Task] = {}
        self.cancelled: set[str] = set()
        self.paused: set[str] = set()
        self.stop_after_rounds: dict[str, int] = {}
        self.provider_semaphore = asyncio.Semaphore(settings.max_provider_concurrency)
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}"

    def emit(self, db: Session, run_id: str, event_type: str, message: str, payload: dict | None = None, level: str = "info") -> None:
        db.add(RunEvent(run_id=run_id, event_type=event_type, level=level, message=message, payload_json=json.dumps(payload or {}, ensure_ascii=False)))
        db.commit()

    def start(self, run_id: str) -> None:
        if run_id in self.tasks and not self.tasks[run_id].done():
            return
        self.cancelled.discard(run_id)
        self.paused.discard(run_id)
        task = asyncio.create_task(self.execute(run_id), name=f"run-{run_id}")
        self.tasks[run_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(run_id, None))

    def cancel(self, run_id: str) -> None:
        self.cancelled.add(run_id)
        task = self.tasks.get(run_id)
        if task and not task.done():
            # The cancel endpoint is a synchronous FastAPI handler and may run in
            # a worker thread. Schedule cancellation on the task's event loop so a
            # long LLM request/gather is interrupted immediately.
            task.get_loop().call_soon_threadsafe(task.cancel)
        with SessionLocal() as db:
            run = db.get(Run, run_id)
            if run:
                run.cancel_requested = True; run.status = "cancelling"; db.commit()

    def pause(self, run_id: str) -> None:
        self.paused.add(run_id)
        with SessionLocal() as db:
            run = db.get(Run, run_id)
            if run:
                run.pause_requested = True; run.status = "paused"; db.commit()

    def resume(self, run_id: str) -> None:
        self.paused.discard(run_id)
        with SessionLocal() as db:
            run = db.get(Run, run_id)
            if run:
                run.pause_requested = False; run.cancel_requested = False; run.status = "running"; db.commit()

    def request_stop_after(self, run_id: str, current_round: int, additional_rounds: int = 0) -> int:
        target = max(1, current_round + additional_rounds)
        self.stop_after_rounds[run_id] = target
        with SessionLocal() as db:
            run = db.get(Run, run_id)
            if run:
                run.stop_after_round = target; run.status = "stopping_after_round"; db.commit()
        return target

    async def _pause_point(self, run_id: str) -> None:
        while True:
            with SessionLocal() as db:
                run = db.get(Run, run_id)
                if not run: raise asyncio.CancelledError
                run.heartbeat_at = datetime.now(timezone.utc)
                db.commit()
                cancel_requested = run.cancel_requested or run_id in self.cancelled
                pause_requested = run.pause_requested or run_id in self.paused
            if cancel_requested: raise asyncio.CancelledError
            if not pause_requested: return
            await asyncio.sleep(0.25)

    async def _wait_between_rounds(self, run_id: str, seconds: int) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            await self._pause_point(run_id)
            if run_id in self.cancelled:
                raise asyncio.CancelledError
            with SessionLocal() as db:
                run = db.get(Run, run_id)
                stop_target = run.stop_after_round if run else self.stop_after_rounds.get(run_id)
            if stop_target is not None:
                with SessionLocal() as db:
                    current = db.scalar(select(func.max(RunRound.round_number)).where(RunRound.run_id == run_id)) or 0
                if int(current) >= stop_target:
                    return
            await asyncio.sleep(min(0.5, max(0.01, deadline - time.monotonic())))

    async def _heartbeat_loop(self, run_id: str) -> None:
        try:
            while True:
                with SessionLocal() as db:
                    run = db.get(Run, run_id)
                    if not run or run.completed_at:
                        return
                    run.worker_id = self.worker_id
                    run.heartbeat_at = datetime.now(timezone.utc)
                    db.commit()
                await asyncio.sleep(settings.worker_heartbeat_seconds)
        except asyncio.CancelledError:
            return

    async def execute_agent(self, run_id: str, agent_id: str, round_number: int, api_key: str, gap: dict, agent_config: dict, endpoint: LlmEndpoint | None = None) -> dict | None:
        with SessionLocal() as db:
            agent = db.get(AgentRun, agent_id)
            if not agent:
                return None
            # endpoint 未由上层传入时（如 langgraph 引擎路径自行解析 api_key、不透传端点），
            # 就地按本任务归属用户解析每用户覆盖，确保两条引擎路径都尊重用户自定义端点。
            if endpoint is None:
                run = db.get(Run, run_id)
                if run:
                    endpoint = user_llm_endpoint(db, run.user_id)
            input_hash = str(agent_config.get("input_hash") or hashlib.sha256(json.dumps({"run": run_id, "round": round_number, "agent": agent_id, "gap": gap}, ensure_ascii=False, sort_keys=True).encode()).hexdigest())
            iteration = db.scalar(select(AgentIteration).where(AgentIteration.agent_id == agent_id, AgentIteration.round_number == round_number))
            previous_input_hash = iteration.input_hash if iteration else None
            if iteration and iteration.status == "completed" and iteration.input_hash == input_hash:
                self.emit(db, run_id, "agent_cache_hit", f"第 {round_number} 轮 · {agent.name} 使用已完成的幂等结果", {"agent_id": agent.id, "round": round_number, "input_hash": input_hash})
                return json.loads(iteration.output_json or "{}")
            if iteration:
                iteration.status = "running"; iteration.started_at = datetime.now(timezone.utc); iteration.completed_at = None; iteration.error = None
                iteration.attempt_count = int(iteration.attempt_count or 0) + 1; iteration.input_hash = input_hash
            else:
                iteration = AgentIteration(run_id=run_id, agent_id=agent_id, round_number=round_number, status="running", started_at=datetime.now(timezone.utc), attempt_count=1, input_hash=input_hash)
                db.add(iteration)
            agent.status = "running"; agent.started_at = iteration.started_at; agent.error = None
            db.commit(); db.refresh(iteration)
            self.emit(db, run_id, "agent_started", f"第 {round_number} 轮 · {agent.name} 开始（{agent.source_mode}）", {"agent_id": agent.id, "round": round_number})
            started = time.monotonic()
            evidence: list[dict] = []
            last_error: Exception | None = None
            max_retries = int(agent_config.get("max_retries", 2))
            try:
                if agent.source_mode in {"web", "hybrid"}:
                    query = f"semiconductor manufacturing {agent.domain} {agent.objective} root cause equipment fab facility evidence"
                    try:
                        evidence = await web_research.search(query)
                    except ExternalServiceError as exc:
                        if agent.source_mode == "web":
                            raise
                        self.emit(db, run_id, "research_warning", f"{agent.name} Web 检索失败，Hybrid 本轮退化为模型先验：{exc}", {"agent_id": agent.id, "round": round_number}, "warning")
                iteration.evidence_json = json.dumps(evidence, ensure_ascii=False); db.commit()
                ontology = semi_kb.ontology_context(limit=350)
                business = semi_kb.business_context()
                vfab_knowledge = semi_kb.vfab_knowledge_context()
                existing_ids = semi_kb.existing_candidate_ids()
                previous = _compact_previous_output(json.loads(agent.output_json or "{}"))
                source_rules = {
                    "web": "所有新增事实必须由给出的网页正文支持；每个web变更集的source_ref必须使用证据URL。",
                    "model_prior": "只能标记model_prior，置信度不得为high；不允许编造URL或伪装成内部/vFab证据。",
                    "hybrid": "web与model_prior必须拆成不同变更集；不得用模型先验补写为web事实。",
                }[agent.source_mode]
                system = (
                    "你是企业级半导体知识工程Agent。只输出一个合法JSON对象，不要Markdown代码围栏。"
                    "扩充本体广度和深度，但已有IRI不得重复；多个Agent并行时也不得提出相同IRI，若概念已被其他Agent覆盖则改用更具体的命名或不提出变更，证据不足时允许不提出变更。"
                    "类、对象属性、数据属性、实例、关系断言、公理限制必须遵守提供的结构。"
                    "每轮应优先补齐合法关系断言（object_assertions/data_assertions）和可证据支持的OWL限制；关系的subject/predicate/object必须分别是已知或本轮同一输出中实际保留的IRI。"
                    "凡声明为 DiagnosticPlaybook 的实例，必须在同一输出里同时给出三条必填直连边：diagnosesAnomaly（恰好1条，对象为 Anomaly）、hasPossibleCause（至少1条）、hasDiagnosticAction（至少1条，对象为 Action）；三者但凡有一条缺失就不要声明该诊断手册（缺边的手册会在门禁前被整体剔除，白白浪费本轮产出）。"
                    "请同时输出至少1条结构化knowledge_entries（若有摘要或痛点，系统会保存为知识条目），并在资料支持新推理模式时输出rule_candidates；自动规则必须是可解释、可执行的只读SPARQL CONSTRUCT，不得输出更新型SPARQL。"
                    "knowledge_entries必须包含可核查source_refs和不少于40字content；rule_candidates不得把普通描述冒充规则。"
                    "existing_candidate_ids 列出了已入库的知识条目ID与自动规则ID（内容派生的slug）；不要重复提出这些ID对应的同名条目，若确有新增请改用更具体、更细分的命名，否则将被系统按重复隔离、无法入库。"
                    "vfab_knowledge_sources列出了已入库的SEMI标准与设备手册来源族；引用它们时source_refs须用其ref句柄，勿编造。标注restricted的来源（设备手册/NDA）只可引用其标题与来源标识，禁止在content中复制其正文。"
                    "所有复数结构以及实例objects/data映射中的每个值都必须使用JSON数组，即使只有一个值。"
                    "仿真只能引用已有经营模型和示例中的合法变量；不得编造现场实测数值。"
                    "若 gap_analysis.feature_gap 给出未映射的源特征，请输出 feature_mapping_candidates 把它们对齐到已声明的本体属性；"
                    "缺少贴切属性时先在本轮新建该属性（发布后下一轮即可映射），逐轮收敛反向缺口。"
                    "注意：特征映射、反向缺口收敛、门禁/校验、新建属性作为下一轮映射目标，这些都是知识库内部的工程动作，"
                    "只能体现在 semantic_changesets/feature_mapping_candidates 里；summary、customer_pains 与 scenario_article_markdown "
                    "必须面向半导体产线现场的真实客户业务问题（停机、良率、工艺偏移、周期、追溯、排障工时等），"
                    "绝不能把“映射覆盖率卡在N%”“逐字重映射已发布属性”“缺乏本体属性承载”这类建库口径写成客户痛点或经营影响。" + source_rules
                )
                repair_context = agent_config.get("repair_context") or {}
                if repair_context:
                    system += (
                        "这是一次门禁返修。必须严格依据 repair_context.gate_error 修正上一版输出；"
                        "不要删除未涉及的合法产物，不要改变来源边界，不要编造证据；返回完整可解析JSON。"
                    )
                prompt = json.dumps({
                    "round": round_number,
                    "agent": {"name": agent.name, "role": agent.role, "domain": agent.domain, "objective": agent.objective, "source_mode": agent.source_mode},
                    "gap_analysis": gap,
                    "known_ontology": ontology,
                    "business_simulation_context": business,
                    "vfab_knowledge_sources": vfab_knowledge,
                    "existing_candidate_ids": existing_ids,
                    "evidence": [{**item, "excerpt": str(item.get("excerpt") or "")[:3000]} for item in evidence[:4]],
                    "previous_round_output": previous,
                    "repair_context": repair_context,
                    "required_output_contract": prompt_contract(),
                }, ensure_ascii=False)
                for attempt in range(max_retries + 1):
                    try:
                        attempt_dir = round_directory(run_id, round_number) / "agents" / agent.id
                        attempt_dir.mkdir(parents=True, exist_ok=True)
                        raw_path = attempt_dir / f"raw-attempt-{attempt + 1:02d}.txt"
                        if previous_input_hash == input_hash and raw_path.is_file():
                            text = raw_path.read_text(encoding="utf-8")
                            self.emit(db, run_id, "model_response_reused", f"{agent.name} 复用崩溃前已保存的模型响应", {"agent_id": agent.id, "round": round_number, "attempt": attempt + 1})
                        else:
                            async with self.provider_semaphore:
                                text = await llm_service.complete(api_key, agent.model_id, system, prompt, timeout_seconds=int(agent_config.get("timeout_seconds", 300)), endpoint=endpoint)
                            raw_path.write_text(text, encoding="utf-8")
                        parsed = _json_object(text)
                        output = validate_and_store_agent_output(
                            parsed, run_id=run_id, round_number=round_number, agent_id=agent.id,
                            source_mode=agent.source_mode, model_id=agent.model_id, evidence=evidence,
                        )
                        iteration.output_json = json.dumps(output, ensure_ascii=False)
                        iteration.evidence_json = json.dumps(evidence, ensure_ascii=False)
                        iteration.candidate_files_json = json.dumps(output.get("semantic_files", []) + output.get("business_files", []) + output.get("simulation_files", []) + output.get("knowledge_files", []) + output.get("rule_files", []) + output.get("mapping_files", []), ensure_ascii=False)
                        iteration.status = "completed"; agent.output_json = iteration.output_json; agent.status = "completed"
                        return output
                    except (ExternalServiceError, json.JSONDecodeError, ValueError, OSError, jsonschema.ValidationError) as exc:
                        last_error = exc
                        if attempt >= max_retries:
                            raise
                        # 网络/供应商类错误（模型压根没产出）与“产物不合法”是两回事：
                        # 前者不应把纠错提示塞进 prompt（越塞越长越容易再次被掐断），标签也要如实。
                        is_network = isinstance(exc, (ExternalServiceError, OSError))
                        if is_network:
                            # 有界退避：避免对抖动代理热重连放大等待。上限取小，因为 llm.complete
                            # 内部对瞬时 HTTP 错误已有指数退避+jitter，这里只挡住紧接着的热重连。
                            await asyncio.sleep(min(4.0, 0.5 * (2 ** attempt)) + random.uniform(0, 0.4))
                            self.emit(db, run_id, "agent_retry", f"{agent.name} 模型网络调用失败，重试 {attempt + 1}/{max_retries}", {"agent_id": agent.id, "round": round_number, "error": str(exc), "kind": "network"}, "warning")
                        else:
                            prompt += f"\n\n上一次输出未通过机器校验：{type(exc).__name__}: {exc}。请完整重写合法JSON，不要解释。"
                            self.emit(db, run_id, "agent_retry", f"{agent.name} 输出校验失败，重试 {attempt + 1}/{max_retries}", {"agent_id": agent.id, "round": round_number, "error": str(exc), "kind": "validation"}, "warning")
            except Exception as exc:
                last_error = exc; iteration.status = "failed"; iteration.error = str(exc); agent.status = "failed"; agent.error = str(exc)
                self.emit(db, run_id, "agent_failed", f"第 {round_number} 轮 · {agent.name} 失败：{exc}", {"agent_id": agent.id, "round": round_number}, "error")
                return None
            finally:
                completed = datetime.now(timezone.utc); duration = round(time.monotonic() - started, 3)
                iteration.completed_at = completed; iteration.duration_seconds = duration
                agent.completed_at = completed; agent.duration_seconds = duration
                if last_error and not iteration.error and iteration.status == "failed": iteration.error = str(last_error)
                db.commit()
                if iteration.status == "completed":
                    self.emit(db, run_id, "agent_completed", f"第 {round_number} 轮 · {agent.name} 完成", {"agent_id": agent.id, "round": round_number, "duration_seconds": duration})

    async def execute_round(self, run_id: str, round_number: int, config: dict, api_key: str, endpoint: LlmEndpoint | None = None) -> bool:
        engine_name = str(config.get("orchestrator_engine") or settings.orchestrator_engine).casefold()
        if engine_name == "langgraph":
            from .langgraph_runtime import RoundGraphEngine
            with SessionLocal() as db:
                row = db.scalar(select(RunRound).where(RunRound.run_id == run_id, RunRound.round_number == round_number))
                resume = bool(row and row.status in {"running", "paused", "recovering"})
            return await RoundGraphEngine(self).execute(run_id, round_number, config, resume=resume)
        return await self.execute_round_legacy(run_id, round_number, config, api_key, endpoint)

    async def execute_round_legacy(self, run_id: str, round_number: int, config: dict, api_key: str, endpoint: LlmEndpoint | None = None) -> bool:
        started = time.monotonic()
        with SessionLocal() as db:
            run = db.get(Run, run_id)
            if not run: return False
            before = (await semi_kb.metrics(db, run.user_id))["totals"]
            round_row = RunRound(run_id=run_id, round_number=round_number, status="running", current_stage=STAGES[0], started_at=datetime.now(timezone.utc), metrics_before_json=json.dumps(before, ensure_ascii=False))
            db.add(round_row); db.commit(); db.refresh(round_row)
            self.emit(db, run_id, "round_started", f"第 {round_number} 轮开始", {"round": round_number, "agent_count": len(run.agents), "publish_changes": bool(config.get("publish_changes"))})
        gap: dict = {}; outputs: list[dict] = []; validation: dict = {}; artifacts: dict = {}
        try:
            for index, stage in enumerate(STAGES):
                await self._pause_point(run_id)
                if run_id in self.cancelled: raise asyncio.CancelledError
                with SessionLocal() as db:
                    run = db.get(Run, run_id); row = db.scalar(select(RunRound).where(RunRound.run_id == run_id, RunRound.round_number == round_number))
                    if not run or not row: return False
                    run.current_stage = stage; run.progress = round(index / len(STAGES) * 100, 1); row.current_stage = stage; db.commit()
                    self.emit(db, run_id, "stage_started", f"第 {round_number} 轮 · {stage}", {"stage": stage, "round": round_number, "progress": run.progress})
                if stage == "gap_analysis":
                    gap = await semi_kb.status()
                    from .reports import augment_gap
                    augment_gap(gap, run_id)
                    round_directory(run_id, round_number).joinpath("gap-analysis.json").write_text(json.dumps(gap, ensure_ascii=False, indent=2), encoding="utf-8")
                elif stage == "parallel_research":
                    with SessionLocal() as db:
                        run = db.get(Run, run_id); agent_ids = [agent.id for agent in run.agents] if run else []
                    agent_configs = config.get("agents") or []
                    results = await asyncio.gather(*(self.execute_agent(run_id, aid, round_number, api_key, gap, agent_configs[pos] if pos < len(agent_configs) else {}, endpoint) for pos, aid in enumerate(agent_ids)))
                    outputs = [item for item in results if item]
                    if not outputs: raise RuntimeError("本轮所有 Agent 均失败")
                    if len(outputs) < len(agent_ids):
                        with SessionLocal() as db: self.emit(db, run_id, "round_warning", f"第 {round_number} 轮部分 Agent 失败，使用 {len(outputs)}/{len(agent_ids)} 个结果继续", {"round": round_number}, "warning")
                elif stage == "evidence_extraction":
                    count = sum(int(output.get("evidence_count", 0)) for output in outputs); artifacts["evidence_pages"] = count
                    with SessionLocal() as db: self.emit(db, run_id, "evidence_ready", f"第 {round_number} 轮提取 {count} 个网页正文证据", {"round": round_number, "evidence_pages": count})
                elif stage == "semantic_modeling":
                    candidates = round_candidate_files(outputs); artifacts.update({key: [str(path) for path in value] for key, value in candidates.items()})
                    with SessionLocal() as db: self.emit(db, run_id, "candidates_ready", f"第 {round_number} 轮生成语义候选 {len(candidates['semantic'])}、经营模型候选 {len(candidates['business'])}、仿真候选 {len(candidates['simulation'])}、知识条目 {len(candidates['knowledge'])}、规则 {len(candidates['rules'])}", {"round": round_number, "semantic": len(candidates["semantic"]), "business": len(candidates["business"]), "simulation": len(candidates["simulation"]), "knowledge": len(candidates["knowledge"]), "rules": len(candidates["rules"])})
                elif stage == "cross_validation":
                    validation = await semi_kb.process_candidates(round_candidate_files(outputs), publish=bool(config.get("publish_changes")))
                    with SessionLocal() as db:
                        row = db.scalar(select(RunRound).where(RunRound.run_id == run_id, RunRound.round_number == round_number))
                        if row:
                            row.quarantined_files_json = json.dumps(validation.get("quarantined_candidates") or [], ensure_ascii=False)
                            db.commit()
                        for item in validation.get("quarantined_candidates") or []:
                            reason = re.sub(r"\s+", " ", str(item.get("reason") or "未知原因"))[-220:]
                            self.emit(db, run_id, "candidate_quarantined", f"候选已隔离：{Path(str(item.get('path') or '')).name}；{reason}", {"round": round_number, **item}, "warning")
                    with SessionLocal() as db: self.emit(db, run_id, "cross_validation", f"第 {round_number} 轮交叉验证通过", {"round": round_number, "published": validation.get("published"), "checks": validation.get("checks")})
                elif stage == "owl_shacl_reasoning":
                    check = (validation.get("checks") or {}).get("full_publish_gate") or (validation.get("checks") or {}).get("semantic_precheck") or {"passed": True}
                    if not check.get("passed", False): raise SemiKbError("OWL/SHACL/推理门禁失败")
                elif stage == "business_simulation":
                    check = (validation.get("checks") or {}).get("business_simulation", {"passed": True})
                    if not check.get("passed", False): raise SemiKbError("经营模型/仿真门禁失败")
                elif stage == "scenario_article":
                    count = len(round_candidate_files(outputs)["articles"]); artifacts["scenario_articles"] = count
                    with SessionLocal() as db: self.emit(db, run_id, "articles_ready", f"第 {round_number} 轮形成 {count} 项场景知识产物候选", {"round": round_number, "count": count})
            with SessionLocal() as db:
                run = db.get(Run, run_id); row = db.scalar(select(RunRound).where(RunRound.run_id == run_id, RunRound.round_number == round_number))
                if not run or not row: return False
                after = (await semi_kb.metrics(db, run.user_id))["totals"]
                row.status = "completed"; row.current_stage = "completed"; row.completed_at = datetime.now(timezone.utc); row.duration_seconds = round(time.monotonic() - started, 3)
                row.metrics_after_json = json.dumps(after, ensure_ascii=False); row.validation_json = json.dumps(validation, ensure_ascii=False); row.artifacts_json = json.dumps(artifacts, ensure_ascii=False)
                delta = {key: int(after.get(key, 0)) - int(before.get(key, 0)) for key in set(before) | set(after) if isinstance(before.get(key, 0), (int, float)) and isinstance(after.get(key, 0), (int, float))}
                # 本轮成功 → 产出下一轮结构化优化方向，下一轮 augment_gap 读回注入 gap，形成显式闭环。
                from .reports import compute_round_direction
                row.next_direction_json = json.dumps(compute_round_direction(semi_kb.feature_gap(), validation, delta), ensure_ascii=False)
                run.metrics_after_json = row.metrics_after_json; run.progress = 100; db.commit()
                self.emit(db, run_id, "round_completed", f"第 {round_number} 轮完成，准备下一轮", {"round": round_number, "duration_seconds": row.duration_seconds, "delta": delta, "published": validation.get("published", False)})
            return True
        except asyncio.CancelledError: raise
        except Exception as exc:
            with SessionLocal() as db:
                row = db.scalar(select(RunRound).where(RunRound.run_id == run_id, RunRound.round_number == round_number))
                if row:
                    row.status = "failed"; row.error = str(exc); row.completed_at = datetime.now(timezone.utc); row.duration_seconds = round(time.monotonic() - started, 3); row.validation_json = json.dumps(validation, ensure_ascii=False); row.artifacts_json = json.dumps(artifacts, ensure_ascii=False); db.commit()
                self.emit(db, run_id, "round_failed", f"第 {round_number} 轮失败：{exc}", {"round": round_number, "error": str(exc)}, "error")
            return False

    async def execute(self, run_id: str) -> None:
        heartbeat_task: asyncio.Task | None = None
        try:
            with SessionLocal() as db:
                run = db.get(Run, run_id)
                if not run: return
                if run.worker_id and run.worker_id != self.worker_id and run.heartbeat_at:
                    heartbeat = run.heartbeat_at
                    if heartbeat.tzinfo is None: heartbeat = heartbeat.replace(tzinfo=timezone.utc)
                    if (datetime.now(timezone.utc) - heartbeat).total_seconds() < settings.worker_heartbeat_seconds * 3:
                        self.emit(db, run_id, "worker_lease_rejected", f"任务已由工作进程 {run.worker_id} 执行", {"worker_id": run.worker_id}, "warning")
                        return
                api_key = user_api_key(db, run.user_id)
                if not api_key: raise ExternalServiceError("未配置模型 API Key")
                endpoint = user_llm_endpoint(db, run.user_id)
                config = json.loads(run.config_json or "{}")
                run.status = "running"; run.started_at = run.started_at or datetime.now(timezone.utc); run.completed_at = None; run.error = None
                run.worker_id = self.worker_id; run.heartbeat_at = datetime.now(timezone.utc)
                if not run.metrics_before_json or run.metrics_before_json == "{}": run.metrics_before_json = json.dumps((await semi_kb.metrics(db, run.user_id))["totals"], ensure_ascii=False)
                db.commit(); self.emit(db, run_id, "run_started", "持续循环任务已启动", {"agent_count": len(run.agents), "continuous": config.get("continuous", True), "round_interval_seconds": config.get("round_interval_seconds", 5)})
            heartbeat_task = asyncio.create_task(self._heartbeat_loop(run_id), name=f"heartbeat-{run_id}")
            consecutive_failures = 0
            max_rounds = config.get("max_rounds")  # None = 无限循环
            while True:
                await self._pause_point(run_id)
                with SessionLocal() as db:
                    latest = db.scalar(select(RunRound).where(RunRound.run_id == run_id).order_by(RunRound.round_number.desc()).limit(1))
                    current = latest.round_number if latest else 0
                    run = db.get(Run, run_id)
                    stop_target = run.stop_after_round if run else self.stop_after_rounds.get(run_id)

                # 检查是否达到最大轮次限制
                if max_rounds is not None and int(current) >= max_rounds:
                    with SessionLocal() as db:
                        run = db.get(Run, run_id)
                        if run:
                            run.status = "completed"
                            run.completed_at = datetime.now(timezone.utc)
                            db.commit()
                        self.emit(db, run_id, "run_completed", f"已完成设定的最大轮次 {max_rounds}，任务结束", {"rounds_completed": current, "max_rounds": max_rounds})
                    break

                if stop_target is not None and int(current) >= stop_target:
                    with SessionLocal() as db:
                        run = db.get(Run, run_id)
                        if run:
                            run.status = "completed"; run.current_stage = "completed"; run.completed_at = datetime.now(timezone.utc); run.progress = 100; db.commit()
                            self.emit(db, run_id, "run_completed", f"任务按请求在第 {int(current)} 轮结束", {"round": int(current)})
                    return
                resume_round = bool(latest and latest.status in {"running", "paused", "recovering"})
                round_number = int(current) if resume_round else int(current) + 1
                success = await self.execute_round(run_id, round_number, config, api_key, endpoint)
                consecutive_failures = 0 if success else consecutive_failures + 1
                with SessionLocal() as db:
                    persisted = db.get(Run, run_id)
                    stop_target = persisted.stop_after_round if persisted else self.stop_after_rounds.get(run_id)
                if not config.get("continuous", True) or (stop_target is not None and round_number >= stop_target):
                    with SessionLocal() as db:
                        run = db.get(Run, run_id)
                        if run:
                            run.status = "completed" if success else "failed"; run.current_stage = "completed" if success else "round_failed"; run.completed_at = datetime.now(timezone.utc); run.progress = 100 if success else run.progress
                            if not success: run.error = f"第 {round_number} 轮未通过，任务结束"
                            db.commit()
                            self.emit(db, run_id, "run_completed" if success else "run_failed", f"任务按请求在第 {round_number} 轮结束" if success else f"第 {round_number} 轮失败，任务结束", {"round": round_number}, "info" if success else "error")
                    return
                if consecutive_failures >= int(config.get("max_consecutive_round_failures", 3)):
                    with SessionLocal() as db:
                        run = db.get(Run, run_id)
                        if run:
                            run.status = "needs_attention"; run.current_stage = "round_failed"; run.error = f"连续 {consecutive_failures} 轮失败，已停止自动重试以避免持续消耗模型额度"; db.commit()
                            self.emit(db, run_id, "attention_required", run.error, {"round": round_number}, "error")
                    return
                delay = int(config.get("round_interval_seconds", 5)) if success else min(300, 5 * (2 ** consecutive_failures))
                with SessionLocal() as db:
                    run = db.get(Run, run_id)
                    if run:
                        run.status = "between_rounds"; run.current_stage = "between_rounds"; run.progress = 0; db.commit()
                        self.emit(db, run_id, "round_wait", f"{delay} 秒后开始第 {round_number + 1} 轮", {"next_round": round_number + 1, "delay_seconds": delay})
                await self._wait_between_rounds(run_id, delay)
                with SessionLocal() as db:
                    run = db.get(Run, run_id)
                    if run and run.status == "between_rounds": run.status = "running"; db.commit()
        except asyncio.CancelledError:
            with SessionLocal() as db:
                run = db.get(Run, run_id)
                if run:
                    run.status = "cancelled"; run.completed_at = datetime.now(timezone.utc); db.commit()
                    self.emit(db, run_id, "run_cancelled", "任务已立即停止；当前未发布候选保留在控制台产物目录", level="warning")
        except Exception as exc:
            with SessionLocal() as db:
                run = db.get(Run, run_id)
                if run:
                    run.status = "failed"; run.error = str(exc); run.completed_at = datetime.now(timezone.utc); db.commit()
                    self.emit(db, run_id, "run_failed", f"任务失败：{exc}", level="error")
        finally:
            if heartbeat_task:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass
            self.stop_after_rounds.pop(run_id, None); self.cancelled.discard(run_id); self.paused.discard(run_id)
            with SessionLocal() as db:
                run = db.get(Run, run_id)
                if run:
                    run.worker_id = None; run.heartbeat_at = datetime.now(timezone.utc); db.commit()


orchestrator = RunOrchestrator()
