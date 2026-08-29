from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timezone

import jsonschema
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal
from ..models import AgentIteration, AgentRun, Run, RunEvent, RunRound
from .llm import ExternalServiceError, llm_service, user_api_key, web_research
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

    def pause(self, run_id: str) -> None:
        self.paused.add(run_id)

    def resume(self, run_id: str) -> None:
        self.paused.discard(run_id)

    def request_stop_after(self, run_id: str, current_round: int, additional_rounds: int = 0) -> int:
        target = max(1, current_round + additional_rounds)
        self.stop_after_rounds[run_id] = target
        return target

    async def _pause_point(self, run_id: str) -> None:
        while run_id in self.paused:
            if run_id in self.cancelled:
                raise asyncio.CancelledError
            await asyncio.sleep(0.25)

    async def _wait_between_rounds(self, run_id: str, seconds: int) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            await self._pause_point(run_id)
            if run_id in self.cancelled:
                raise asyncio.CancelledError
            stop_target = self.stop_after_rounds.get(run_id)
            if stop_target is not None:
                with SessionLocal() as db:
                    current = db.scalar(select(func.max(RunRound.round_number)).where(RunRound.run_id == run_id)) or 0
                if int(current) >= stop_target:
                    return
            await asyncio.sleep(min(0.5, max(0.01, deadline - time.monotonic())))

    async def execute_agent(self, run_id: str, agent_id: str, round_number: int, api_key: str, gap: dict, agent_config: dict) -> dict | None:
        with SessionLocal() as db:
            agent = db.get(AgentRun, agent_id)
            if not agent:
                return None
            iteration = AgentIteration(run_id=run_id, agent_id=agent_id, round_number=round_number, status="running", started_at=datetime.now(timezone.utc))
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
                ontology = semi_kb.ontology_context(limit=350)
                business = semi_kb.business_context()
                previous = _compact_previous_output(json.loads(agent.output_json or "{}"))
                source_rules = {
                    "web": "所有新增事实必须由给出的网页正文支持；每个web变更集的source_ref必须使用证据URL。",
                    "model_prior": "只能标记model_prior，置信度不得为high；不允许编造URL或伪装成内部/vFab证据。",
                    "hybrid": "web与model_prior必须拆成不同变更集；不得用模型先验补写为web事实。",
                }[agent.source_mode]
                system = (
                    "你是企业级半导体知识工程Agent。只输出一个合法JSON对象，不要Markdown代码围栏。"
                    "扩充本体广度和深度，但已有IRI不得重复，证据不足时允许不提出变更。"
                    "类、对象属性、数据属性、实例、关系断言、公理限制必须遵守提供的结构。"
                    "所有复数结构以及实例objects/data映射中的每个值都必须使用JSON数组，即使只有一个值。"
                    "仿真只能引用已有经营模型和示例中的合法变量；不得编造现场实测数值。" + source_rules
                )
                prompt = json.dumps({
                    "round": round_number,
                    "agent": {"name": agent.name, "role": agent.role, "domain": agent.domain, "objective": agent.objective, "source_mode": agent.source_mode},
                    "gap_analysis": gap,
                    "known_ontology": ontology,
                    "business_simulation_context": business,
                    "evidence": [{**item, "excerpt": str(item.get("excerpt") or "")[:3000]} for item in evidence[:4]],
                    "previous_round_output": previous,
                    "required_output_contract": prompt_contract(),
                }, ensure_ascii=False)
                for attempt in range(max_retries + 1):
                    try:
                        async with self.provider_semaphore:
                            text = await llm_service.complete(api_key, agent.model_id, system, prompt, timeout_seconds=int(agent_config.get("timeout_seconds", 300)))
                        attempt_dir = round_directory(run_id, round_number) / "agents" / agent.id
                        attempt_dir.mkdir(parents=True, exist_ok=True)
                        (attempt_dir / f"raw-attempt-{attempt + 1:02d}.txt").write_text(text, encoding="utf-8")
                        parsed = _json_object(text)
                        output = validate_and_store_agent_output(
                            parsed, run_id=run_id, round_number=round_number, agent_id=agent.id,
                            source_mode=agent.source_mode, model_id=agent.model_id, evidence=evidence,
                        )
                        iteration.output_json = json.dumps(output, ensure_ascii=False)
                        iteration.evidence_json = json.dumps(evidence, ensure_ascii=False)
                        iteration.candidate_files_json = json.dumps(output.get("semantic_files", []) + output.get("business_files", []) + output.get("simulation_files", []), ensure_ascii=False)
                        iteration.status = "completed"; agent.output_json = iteration.output_json; agent.status = "completed"
                        return output
                    except (ExternalServiceError, json.JSONDecodeError, ValueError, OSError, jsonschema.ValidationError) as exc:
                        last_error = exc
                        if attempt >= max_retries:
                            raise
                        prompt += f"\n\n上一次输出未通过机器校验：{type(exc).__name__}: {exc}。请完整重写合法JSON，不要解释。"
                        self.emit(db, run_id, "agent_retry", f"{agent.name} 输出校验失败，重试 {attempt + 1}/{max_retries}", {"agent_id": agent.id, "round": round_number, "error": str(exc)}, "warning")
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

    async def execute_round(self, run_id: str, round_number: int, config: dict, api_key: str) -> bool:
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
                    round_directory(run_id, round_number).joinpath("gap-analysis.json").write_text(json.dumps(gap, ensure_ascii=False, indent=2), encoding="utf-8")
                elif stage == "parallel_research":
                    with SessionLocal() as db:
                        run = db.get(Run, run_id); agent_ids = [agent.id for agent in run.agents] if run else []
                    agent_configs = config.get("agents") or []
                    results = await asyncio.gather(*(self.execute_agent(run_id, aid, round_number, api_key, gap, agent_configs[pos] if pos < len(agent_configs) else {}) for pos, aid in enumerate(agent_ids)))
                    outputs = [item for item in results if item]
                    if not outputs: raise RuntimeError("本轮所有 Agent 均失败")
                    if len(outputs) < len(agent_ids):
                        with SessionLocal() as db: self.emit(db, run_id, "round_warning", f"第 {round_number} 轮部分 Agent 失败，使用 {len(outputs)}/{len(agent_ids)} 个结果继续", {"round": round_number}, "warning")
                elif stage == "evidence_extraction":
                    count = sum(int(output.get("evidence_count", 0)) for output in outputs); artifacts["evidence_pages"] = count
                    with SessionLocal() as db: self.emit(db, run_id, "evidence_ready", f"第 {round_number} 轮提取 {count} 个网页正文证据", {"round": round_number, "evidence_pages": count})
                elif stage == "semantic_modeling":
                    candidates = round_candidate_files(outputs); artifacts.update({key: [str(path) for path in value] for key, value in candidates.items()})
                    with SessionLocal() as db: self.emit(db, run_id, "candidates_ready", f"第 {round_number} 轮生成语义候选 {len(candidates['semantic'])}、经营模型候选 {len(candidates['business'])}、仿真候选 {len(candidates['simulation'])}", {"round": round_number, "semantic": len(candidates["semantic"]), "business": len(candidates["business"]), "simulation": len(candidates["simulation"])})
                elif stage == "cross_validation":
                    validation = await semi_kb.process_candidates(round_candidate_files(outputs), publish=bool(config.get("publish_changes")))
                    with SessionLocal() as db: self.emit(db, run_id, "cross_validation", f"第 {round_number} 轮交叉验证通过", {"round": round_number, "published": validation.get("published"), "checks": validation.get("checks")})
                elif stage == "owl_shacl_reasoning":
                    check = (validation.get("checks") or {}).get("full_publish_gate") or (validation.get("checks") or {}).get("semantic_precheck") or {"passed": True}
                    if not check.get("passed", False): raise SemiKbError("OWL/SHACL/推理门禁失败")
                elif stage == "business_simulation":
                    check = (validation.get("checks") or {}).get("business_simulation", {"passed": True})
                    if not check.get("passed", False): raise SemiKbError("经营模型/仿真门禁失败")
                elif stage == "scenario_article":
                    count = len(round_candidate_files(outputs)["articles"]); artifacts["scenario_articles"] = count
                    with SessionLocal() as db: self.emit(db, run_id, "articles_ready", f"第 {round_number} 轮形成 {count} 篇场景文章候选", {"round": round_number, "count": count})
            with SessionLocal() as db:
                run = db.get(Run, run_id); row = db.scalar(select(RunRound).where(RunRound.run_id == run_id, RunRound.round_number == round_number))
                if not run or not row: return False
                after = (await semi_kb.metrics(db, run.user_id))["totals"]
                row.status = "completed"; row.current_stage = "completed"; row.completed_at = datetime.now(timezone.utc); row.duration_seconds = round(time.monotonic() - started, 3)
                row.metrics_after_json = json.dumps(after, ensure_ascii=False); row.validation_json = json.dumps(validation, ensure_ascii=False); row.artifacts_json = json.dumps(artifacts, ensure_ascii=False)
                run.metrics_after_json = row.metrics_after_json; run.progress = 100; db.commit()
                delta = {key: int(after.get(key, 0)) - int(before.get(key, 0)) for key in set(before) | set(after) if isinstance(before.get(key, 0), (int, float)) and isinstance(after.get(key, 0), (int, float))}
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
        try:
            with SessionLocal() as db:
                run = db.get(Run, run_id)
                if not run: return
                api_key = user_api_key(db, run.user_id)
                if not api_key: raise ExternalServiceError("未配置模型 API Key")
                config = json.loads(run.config_json or "{}")
                run.status = "running"; run.started_at = run.started_at or datetime.now(timezone.utc); run.completed_at = None; run.error = None
                if not run.metrics_before_json or run.metrics_before_json == "{}": run.metrics_before_json = json.dumps((await semi_kb.metrics(db, run.user_id))["totals"], ensure_ascii=False)
                db.commit(); self.emit(db, run_id, "run_started", "持续循环任务已启动", {"agent_count": len(run.agents), "continuous": config.get("continuous", True), "round_interval_seconds": config.get("round_interval_seconds", 5)})
            consecutive_failures = 0
            while True:
                await self._pause_point(run_id)
                with SessionLocal() as db: current = db.scalar(select(func.max(RunRound.round_number)).where(RunRound.run_id == run_id)) or 0
                stop_target = self.stop_after_rounds.get(run_id)
                if stop_target is not None and int(current) >= stop_target:
                    with SessionLocal() as db:
                        run = db.get(Run, run_id)
                        if run:
                            run.status = "completed"; run.current_stage = "completed"; run.completed_at = datetime.now(timezone.utc); run.progress = 100; db.commit()
                            self.emit(db, run_id, "run_completed", f"任务按请求在第 {int(current)} 轮结束", {"round": int(current)})
                    return
                round_number = int(current) + 1
                success = await self.execute_round(run_id, round_number, config, api_key)
                consecutive_failures = 0 if success else consecutive_failures + 1
                stop_target = self.stop_after_rounds.get(run_id)
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
            self.stop_after_rounds.pop(run_id, None); self.cancelled.discard(run_id); self.paused.discard(run_id)


orchestrator = RunOrchestrator()
