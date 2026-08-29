from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Cookie, Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from rdflib import Graph, RDF, RDFS
from rdflib.namespace import OWL
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .db import Base, SessionLocal, engine, get_db
from .models import AgentIteration, AgentRun, DailyReport, DailyReportRevision, EncryptedCredential, ExportJob, LoopSetting, NotificationRecord, ReportSetting, Run, RunEvent, RunRound, User, UserPreference
from .schemas import CredentialUpdate, DefaultModelUpdate, ExportCreate, LoginRequest, LoopUpdate, ReportContentUpdate, ReportSettingsUpdate, RunCreate, SetupRequest
from .security import encrypt_secret, hash_password, new_session_token, verify_password
from .services.exports import create_export
from .services.llm import ExternalServiceError, llm_service, user_api_key
from .services.notifications import NotificationError, send_email_reminder, send_wecom
from .services.orchestrator import orchestrator
from .services.reports import generate_report, send_report, validate_report
from .services.semi_kb import SemiKbError, semi_kb


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        interrupted = db.scalars(select(Run).where(Run.status.in_(["running", "paused", "between_rounds", "stopping_after_round", "cancelling"]))).all()
        for run in interrupted:
            run.status = "interrupted"
            run.error = "后端重启导致运行中断，可从运行历史重新启动"
        db.commit()
    scheduler.add_job(scheduler_tick, "interval", seconds=60, id="scheduler-tick", max_instances=1, coalesce=True, replace_existing=True)
    scheduler.start()
    try:
        yield
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)


app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[settings.frontend_origin], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
scheduler = AsyncIOScheduler(timezone=settings.timezone)
sessions: dict[str, int] = {}
model_cache: dict[str, object] = {"items": [], "fetched_at": None}


def current_user(session_id: Annotated[str | None, Cookie()] = None, db: Session = Depends(get_db)) -> User:
    user_id = sessions.get(session_id or "")
    user = db.get(User, user_id) if user_id else None
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return user


def json_load(value: str, default):
    try:
        return json.loads(value or "")
    except json.JSONDecodeError:
        return default


def run_payload(run: Run) -> dict:
    config = json_load(run.config_json, {})
    current_round = max((item.round_number for item in run.rounds), default=0)
    return {
        "id": run.id, "model_id": run.model_id, "status": run.status, "current_stage": run.current_stage,
        "progress": run.progress, "created_at": run.created_at, "started_at": run.started_at,
        "completed_at": run.completed_at, "error": run.error, "current_round": current_round,
        "rounds_completed": sum(item.status == "completed" for item in run.rounds),
        "continuous": config.get("continuous", True), "publish_changes": config.get("publish_changes", False),
        "round_interval_seconds": config.get("round_interval_seconds", 5),
        "stop_after_round": orchestrator.stop_after_rounds.get(run.id),
        "agents": [{"id": a.id, "name": a.name, "role": a.role, "domain": a.domain, "source_mode": a.source_mode, "model_id": a.model_id, "status": a.status, "duration_seconds": a.duration_seconds, "error": a.error, "output": json_load(a.output_json, {})} for a in run.agents],
    }


def create_run(db: Session, user_id: int, request: RunCreate) -> Run:
    run_id = "run-" + uuid.uuid4().hex[:16]
    run = Run(id=run_id, user_id=user_id, model_id=request.model_id, config_json=request.model_dump_json())
    db.add(run)
    for index, agent in enumerate(request.agents, 1):
        db.add(AgentRun(id=f"{run_id}-a{index:02d}", run_id=run_id, name=agent.name, role=agent.role, domain=agent.domain, objective=agent.objective, source_mode=agent.source_mode, model_id=agent.model_override or request.model_id))
    db.commit()
    db.refresh(run)
    return run


async def generate_report_job(user_id: int, report_date: str, model_id: str) -> None:
    with SessionLocal() as db:
        try:
            await generate_report(db, user_id, report_date, model_id)
        except Exception:
            report = db.scalar(select(DailyReport).where(DailyReport.user_id == user_id, DailyReport.report_date == report_date))
            if report and report.status not in {"send_blocked", "send_failed"}:
                report.status = "send_blocked"
                db.commit()


async def scheduler_tick() -> None:
    zone = ZoneInfo(settings.timezone)
    now = datetime.now(zone)
    with SessionLocal() as db:
        for row in db.scalars(select(ReportSetting).where(ReportSetting.enabled.is_(True))).all():
            if row.generate_time == now.strftime("%H:%M"):
                date_text = now.date().isoformat()
                exists = db.scalar(select(DailyReport).where(DailyReport.user_id == row.user_id, DailyReport.report_date == date_text))
                preference = db.get(UserPreference, row.user_id)
                if not exists and preference and preference.default_model_id:
                    asyncio.create_task(generate_report_job(row.user_id, date_text, preference.default_model_id))
        pending = db.scalars(select(DailyReport).where(DailyReport.status == "waiting_approval")).all()
        for report in pending:
            setting = db.get(ReportSetting, report.user_id)
            if not setting or not report.generated_at:
                continue
            deadline = report.generated_at + timedelta(minutes=setting.reminder_timeout_minutes)
            sent = db.scalar(select(func.count(NotificationRecord.id)).where(NotificationRecord.report_id == report.id, NotificationRecord.channel == "email", NotificationRecord.status == "sent"))
            if datetime.now(timezone.utc) >= deadline and not sent:
                try:
                    await send_email_reminder(db, report.user_id, f"日报待审核提醒 {report.report_date}", f"日报已生成超过 {setting.reminder_timeout_minutes} 分钟，当前仍待审核发送。", report.id)
                except NotificationError:
                    pass
        for loop in db.scalars(select(LoopSetting).where(LoopSetting.enabled.is_(True))).all():
            due = loop.next_run_at is None or loop.next_run_at <= datetime.now(timezone.utc)
            active = db.scalar(select(func.count(Run.id)).where(Run.user_id == loop.user_id, Run.status.in_(["pending", "running", "paused", "between_rounds", "stopping_after_round", "cancelling"])))
            if due and not active:
                try:
                    request = RunCreate.model_validate(json_load(loop.run_config_json, {}))
                except Exception:
                    loop.enabled = False
                    db.commit()
                    continue
                run = create_run(db, loop.user_id, request)
                orchestrator.start(run.id)
                loop.next_run_at = datetime.now(timezone.utc) + timedelta(minutes=loop.interval_minutes)
                db.commit()


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "app": settings.app_name, "semi_kb_root": str(settings.semi_kb_root), "max_agents": settings.max_agent_count}


@app.get("/api/auth/status")
def auth_status(db: Session = Depends(get_db)) -> dict:
    return {"setup_required": (db.scalar(select(func.count(User.id))) or 0) == 0}


@app.post("/api/auth/setup", status_code=201)
def setup_account(payload: SetupRequest, response: Response, db: Session = Depends(get_db)) -> dict:
    if db.scalar(select(func.count(User.id))):
        raise HTTPException(status_code=409, detail="系统已完成初始化")
    user = User(username=payload.username, password_hash=hash_password(payload.password))
    db.add(user)
    db.flush()
    db.add(UserPreference(user_id=user.id, default_agent_count=6, timezone=settings.timezone))
    db.add(ReportSetting(user_id=user.id))
    db.add(LoopSetting(user_id=user.id))
    db.commit()
    token = new_session_token()
    sessions[token] = user.id
    response.set_cookie("session_id", token, httponly=True, samesite="lax", secure=False, max_age=86400)
    return {"id": user.id, "username": user.username}


@app.post("/api/auth/login")
def login(payload: LoginRequest, response: Response, db: Session = Depends(get_db)) -> dict:
    user = db.scalar(select(User).where(User.username == payload.username))
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = new_session_token()
    sessions[token] = user.id
    response.set_cookie("session_id", token, httponly=True, samesite="lax", secure=False, max_age=86400)
    return {"id": user.id, "username": user.username}


@app.post("/api/auth/logout", status_code=204)
def logout(response: Response, session_id: Annotated[str | None, Cookie()] = None) -> Response:
    if session_id:
        sessions.pop(session_id, None)
    response.delete_cookie("session_id")
    return response


@app.get("/api/users/me")
def me(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    preference = db.get(UserPreference, user.id)
    return {"id": user.id, "username": user.username, "preferences": {"default_model_id": preference.default_model_id, "default_agent_count": preference.default_agent_count, "timezone": preference.timezone}}


@app.get("/api/models")
async def models(search: str = "", refresh: bool = False, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    api_key = user_api_key(db, user.id)
    if not api_key:
        raise HTTPException(status_code=424, detail="未配置模型 API Key")
    fetched_at = model_cache.get("fetched_at")
    stale = not fetched_at or datetime.now(timezone.utc) - fetched_at > timedelta(minutes=5)
    try:
        if refresh or stale or not model_cache["items"]:
            model_cache["items"] = await llm_service.list_models(api_key)
            model_cache["fetched_at"] = datetime.now(timezone.utc)
    except ExternalServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    items = model_cache["items"]
    if search:
        items = [item for item in items if search.casefold() in item["id"].casefold()]
    preference = db.get(UserPreference, user.id)
    return {"items": items, "total": len(items), "default_model_id": preference.default_model_id, "fetched_at": model_cache["fetched_at"]}


@app.patch("/api/users/me/preferences/default-model")
async def update_default_model(payload: DefaultModelUpdate, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    api_key = user_api_key(db, user.id)
    if not api_key:
        raise HTTPException(status_code=424, detail="未配置模型 API Key")
    try:
        available = await llm_service.list_models(api_key, payload.model_id)
    except ExternalServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if payload.model_id not in {item["id"] for item in available}:
        raise HTTPException(status_code=422, detail="该模型当前不可用")
    preference = db.get(UserPreference, user.id)
    preference.default_model_id = payload.model_id
    db.commit()
    return {"default_model_id": payload.model_id}


@app.get("/api/credentials")
def credentials(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    rows = db.scalars(select(EncryptedCredential).where(EncryptedCredential.user_id == user.id)).all()
    return {"items": [{"kind": row.kind, "configured": True, "masked_hint": row.masked_hint, "updated_at": row.updated_at} for row in rows]}


@app.put("/api/credentials")
def save_credential(payload: CredentialUpdate, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    row = db.scalar(select(EncryptedCredential).where(EncryptedCredential.user_id == user.id, EncryptedCredential.kind == payload.kind))
    hint = payload.masked_hint or (payload.value[:2] + "••••" + payload.value[-2:] if len(payload.value) >= 6 else "已配置")
    if row:
        row.ciphertext = encrypt_secret(payload.value)
        row.masked_hint = hint
    else:
        db.add(EncryptedCredential(user_id=user.id, kind=payload.kind, ciphertext=encrypt_secret(payload.value), masked_hint=hint))
    db.commit()
    if payload.kind == "llm_api_key":
        model_cache["items"] = []
        model_cache["fetched_at"] = None
    return {"kind": payload.kind, "configured": True, "masked_hint": hint}


@app.delete("/api/credentials/{kind}", status_code=204)
def delete_credential(kind: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> Response:
    row = db.scalar(select(EncryptedCredential).where(EncryptedCredential.user_id == user.id, EncryptedCredential.kind == kind))
    if row:
        db.delete(row)
        db.commit()
    return Response(status_code=204)


@app.get("/api/dashboard")
async def dashboard(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    try:
        metrics = await semi_kb.metrics(db, user.id)
    except SemiKbError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    latest = db.scalar(select(Run).where(Run.user_id == user.id).order_by(Run.created_at.desc()))
    return {"metrics": metrics, "latest_run": run_payload(latest) if latest else None}


@app.post("/api/runs", status_code=202)
async def start_run(payload: RunCreate, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    if len(payload.agents) > settings.max_agent_count:
        raise HTTPException(status_code=422, detail=f"最多 {settings.max_agent_count} 个子 Agent")
    active = db.scalar(select(Run).where(Run.user_id == user.id, Run.status.in_(["pending", "running", "paused", "between_rounds", "stopping_after_round", "cancelling"])))
    if active:
        raise HTTPException(status_code=409, detail=f"已有持续任务 {active.id} 正在运行，请先停止后再启动新任务")
    api_key = user_api_key(db, user.id)
    if not api_key:
        raise HTTPException(status_code=424, detail="未配置模型 API Key")
    try:
        available = await llm_service.list_models(api_key)
    except ExternalServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    requested_models = {payload.model_id} | {agent.model_override for agent in payload.agents if agent.model_override}
    missing_models = sorted(requested_models - {item["id"] for item in available})
    if missing_models:
        raise HTTPException(status_code=422, detail="以下模型当前不可用：" + "、".join(missing_models))
    run = create_run(db, user.id, payload)
    orchestrator.start(run.id)
    return run_payload(run)


@app.get("/api/runs")
def list_runs(limit: int = Query(default=50, ge=1, le=200), user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    rows = db.scalars(select(Run).where(Run.user_id == user.id).order_by(Run.created_at.desc()).limit(limit)).all()
    return {"items": [run_payload(row) for row in rows]}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    run = db.get(Run, run_id)
    if not run or run.user_id != user.id:
        raise HTTPException(status_code=404, detail="任务不存在")
    return run_payload(run)


@app.post("/api/runs/{run_id}/pause")
def pause_run(run_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    run = db.get(Run, run_id)
    if not run or run.user_id != user.id:
        raise HTTPException(status_code=404, detail="任务不存在")
    if run.status not in {"running", "between_rounds", "stopping_after_round"}:
        raise HTTPException(status_code=409, detail="当前状态不能暂停")
    orchestrator.pause(run_id)
    run.status = "paused"
    db.commit()
    return {"status": "paused"}


@app.post("/api/runs/{run_id}/resume")
def resume_run(run_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    run = db.get(Run, run_id)
    if not run or run.user_id != user.id:
        raise HTTPException(status_code=404, detail="任务不存在")
    if run.status == "needs_attention":
        run.status = "pending"; run.error = None; db.commit(); orchestrator.start(run_id)
        return {"status": "pending"}
    if run.status != "paused":
        raise HTTPException(status_code=409, detail="当前状态不能恢复")
    orchestrator.resume(run_id); run.status = "running"
    db.commit()
    return {"status": "running"}


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    run = db.get(Run, run_id)
    if not run or run.user_id != user.id:
        raise HTTPException(status_code=404, detail="任务不存在")
    orchestrator.cancel(run_id)
    run.status = "cancelling"
    db.commit()
    return {"status": "cancelling"}


def _request_round_stop(run: Run, db: Session, additional_rounds: int) -> dict:
    current = db.scalar(select(func.max(RunRound.round_number)).where(RunRound.run_id == run.id)) or 0
    if run.status not in {"pending", "running", "paused", "between_rounds", "stopping_after_round"}:
        raise HTTPException(status_code=409, detail="任务当前不在持续运行状态")
    target = orchestrator.request_stop_after(run.id, int(current) or 1, additional_rounds)
    run.status = "stopping_after_round"
    db.commit()
    return {"status": "stopping_after_round", "current_round": int(current), "stop_after_round": target}


@app.post("/api/runs/{run_id}/stop-after-current-round")
def stop_after_current_round(run_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    run = db.get(Run, run_id)
    if not run or run.user_id != user.id:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _request_round_stop(run, db, 0)


@app.post("/api/runs/{run_id}/stop-after-next-round")
def stop_after_next_round(run_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    run = db.get(Run, run_id)
    if not run or run.user_id != user.id:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _request_round_stop(run, db, 1)


@app.get("/api/runs/{run_id}/rounds")
def run_rounds(run_id: str, limit: int = Query(default=100, ge=1, le=1000), user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    run = db.get(Run, run_id)
    if not run or run.user_id != user.id:
        raise HTTPException(status_code=404, detail="任务不存在")
    rows = db.scalars(select(RunRound).where(RunRound.run_id == run_id).order_by(RunRound.round_number.desc()).limit(limit)).all()
    items = []
    for row in rows:
        iterations = db.scalars(select(AgentIteration).where(AgentIteration.run_id == run_id, AgentIteration.round_number == row.round_number).order_by(AgentIteration.agent_id)).all()
        items.append({
            "round_number": row.round_number, "status": row.status, "current_stage": row.current_stage,
            "started_at": row.started_at, "completed_at": row.completed_at, "duration_seconds": row.duration_seconds,
            "metrics_before": json_load(row.metrics_before_json, {}), "metrics_after": json_load(row.metrics_after_json, {}),
            "validation": json_load(row.validation_json, {}), "artifacts": json_load(row.artifacts_json, {}), "error": row.error,
            "agents": [{"agent_id": item.agent_id, "status": item.status, "duration_seconds": item.duration_seconds, "evidence": json_load(item.evidence_json, []), "output": json_load(item.output_json, {}), "error": item.error} for item in iterations],
        })
    return {"items": items, "total": len(run.rounds)}


@app.post("/api/runs/{run_id}/retry", status_code=202)
async def retry_run(run_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    old = db.get(Run, run_id)
    if not old or old.user_id != user.id:
        raise HTTPException(status_code=404, detail="任务不存在")
    payload = RunCreate.model_validate(json_load(old.config_json, {}))
    run = create_run(db, user.id, payload)
    orchestrator.start(run.id)
    return run_payload(run)


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, request: Request, after: int = 0, user: User = Depends(current_user), db: Session = Depends(get_db)) -> StreamingResponse:
    run = db.get(Run, run_id)
    if not run or run.user_id != user.id:
        raise HTTPException(status_code=404, detail="任务不存在")
    async def stream():
        cursor = after
        idle = 0
        while not await request.is_disconnected():
            with SessionLocal() as session:
                events = session.scalars(select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.id > cursor).order_by(RunEvent.id)).all()
                current = session.get(Run, run_id)
                for event in events:
                    cursor = event.id
                    data = {"id": event.id, "type": event.event_type, "level": event.level, "message": event.message, "payload": json_load(event.payload_json, {}), "created_at": event.created_at.isoformat()}
                    yield f"id: {event.id}\nevent: {event.event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                if events:
                    idle = 0
                else:
                    idle += 1
                    yield ": keepalive\n\n"
                if current and current.status in {"completed", "failed", "cancelled", "interrupted", "needs_attention"} and idle >= 2:
                    break
            await asyncio.sleep(0.8)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/loop")
def get_loop(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    row = db.get(LoopSetting, user.id) or LoopSetting(user_id=user.id)
    if db.get(LoopSetting, user.id) is None:
        db.add(row); db.commit()
    return {"enabled": row.enabled, "interval_minutes": row.interval_minutes, "run_config": json_load(row.run_config_json, {}), "next_run_at": row.next_run_at}


@app.put("/api/loop")
def update_loop(payload: LoopUpdate, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    row = db.get(LoopSetting, user.id) or LoopSetting(user_id=user.id)
    row.enabled = payload.enabled
    row.interval_minutes = payload.interval_minutes
    if payload.run_config:
        row.run_config_json = payload.run_config.model_dump_json()
    if payload.enabled:
        if not json_load(row.run_config_json, {}):
            raise HTTPException(status_code=422, detail="开启 Loop 前必须保存任务配置")
        row.next_run_at = datetime.now(timezone.utc) + timedelta(minutes=row.interval_minutes)
    else:
        row.next_run_at = None
    db.add(row); db.commit()
    return {"enabled": row.enabled, "interval_minutes": row.interval_minutes, "next_run_at": row.next_run_at}


@app.get("/api/ontology/metrics")
async def ontology_metrics(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    return await semi_kb.metrics(db, user.id)


@app.get("/api/ontology/entities")
def ontology_entities(search: str = "", limit: int = Query(100, ge=1, le=500), user: User = Depends(current_user)) -> dict:
    graph = Graph()
    for path in sorted((settings.semi_kb_root / "ontology" / "modules").glob("*.ttl")):
        graph.parse(path, format="turtle")
    items = []
    kinds = [(OWL.Class, "Class"), (OWL.ObjectProperty, "ObjectProperty"), (OWL.DatatypeProperty, "DatatypeProperty")]
    for rdf_type, kind in kinds:
        for subject in graph.subjects(RDF.type, rdf_type):
            label = next(graph.objects(subject, RDFS.label), None)
            item = {"iri": str(subject), "label": str(label) if label else str(subject), "kind": kind}
            if not search or search.casefold() in (item["iri"] + item["label"]).casefold():
                items.append(item)
    return {"items": items[:limit], "total": len(items)}


def yaml_catalog(pattern: str) -> list[dict]:
    items = []
    for path in sorted(settings.semi_kb_root.glob(pattern)):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            doc = None
        items.append({"path": path.relative_to(settings.semi_kb_root).as_posix(), "name": path.stem, "document": doc})
    return items


@app.get("/api/knowledge/facts")
def knowledge_facts(user: User = Depends(current_user)) -> dict:
    return {"items": yaml_catalog("kb/**/*.yaml")}


@app.get("/api/business-models")
def business_models(user: User = Depends(current_user)) -> dict:
    return {"items": yaml_catalog("business/models/*.yaml")}


@app.get("/api/simulations")
def simulations(user: User = Depends(current_user)) -> dict:
    return {"items": yaml_catalog("simulation/scenarios/*.yaml")}


@app.get("/api/scenario-articles")
def scenario_articles(user: User = Depends(current_user)) -> dict:
    path = settings.semi_kb_root / "knowledge" / "articles" / "current-scenarios.md"
    return {"content": path.read_text(encoding="utf-8") if path.is_file() else "", "path": path.relative_to(settings.semi_kb_root).as_posix()}


@app.get("/api/report-settings")
def get_report_settings(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    row = db.get(ReportSetting, user.id) or ReportSetting(user_id=user.id)
    if db.get(ReportSetting, user.id) is None:
        db.add(row); db.commit()
    return {"enabled": row.enabled, "generate_time": row.generate_time, "approval_required": row.approval_required, "reminder_timeout_minutes": row.reminder_timeout_minutes, "email_sender": row.email_sender, "email_recipient": row.email_recipient}


@app.put("/api/report-settings")
def update_report_settings(payload: ReportSettingsUpdate, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    row = db.get(ReportSetting, user.id) or ReportSetting(user_id=user.id)
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    db.add(row); db.commit()
    return payload.model_dump()


@app.get("/api/reports/daily/{report_date}")
def get_report(report_date: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    report = db.scalar(select(DailyReport).where(DailyReport.user_id == user.id, DailyReport.report_date == report_date))
    if not report:
        raise HTTPException(status_code=404, detail="日报不存在")
    return {"id": report.id, "report_date": report.report_date, "status": report.status, "approval_required": report.approval_required, "content": report.content, "metrics_snapshot": json_load(report.metrics_snapshot_json, {}), "validation": json_load(report.validation_json, {}), "generated_at": report.generated_at, "approved_at": report.approved_at, "sent_at": report.sent_at}


@app.post("/api/reports/daily/{report_date}/generate", status_code=202)
async def create_daily_report(report_date: str, model_id: str | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    preference = db.get(UserPreference, user.id)
    selected = model_id or (preference.default_model_id if preference else None)
    if not selected:
        raise HTTPException(status_code=422, detail="请先选择模型或设置默认模型")
    report = db.scalar(select(DailyReport).where(DailyReport.user_id == user.id, DailyReport.report_date == report_date))
    if not report:
        setting = db.get(ReportSetting, user.id) or ReportSetting(user_id=user.id)
        report = DailyReport(user_id=user.id, report_date=report_date, status="generating", approval_required=setting.approval_required)
        db.add(report); db.commit(); db.refresh(report)
    else:
        report.status = "generating"; db.commit()
    asyncio.create_task(generate_report_job(user.id, report_date, selected))
    return {"id": report.id, "status": report.status}


@app.patch("/api/reports/daily/{report_date}")
def update_report(report_date: str, payload: ReportContentUpdate, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    report = db.scalar(select(DailyReport).where(DailyReport.user_id == user.id, DailyReport.report_date == report_date))
    if not report:
        raise HTTPException(status_code=404, detail="日报不存在")
    report.content = payload.content
    validation = validate_report(payload.content, json_load(report.metrics_snapshot_json, {}))
    report.validation_json = json.dumps(validation, ensure_ascii=False)
    db.add(DailyReportRevision(report_id=report.id, content=payload.content, editor_user_id=user.id))
    db.commit()
    return {"status": report.status, "validation": validation}


@app.post("/api/reports/daily/{report_date}/approve")
def approve_report(report_date: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    report = db.scalar(select(DailyReport).where(DailyReport.user_id == user.id, DailyReport.report_date == report_date))
    if not report:
        raise HTTPException(status_code=404, detail="日报不存在")
    validation = validate_report(report.content, json_load(report.metrics_snapshot_json, {}))
    if not validation["passed"]:
        raise HTTPException(status_code=422, detail=validation["errors"])
    report.status = "approved"
    report.approved_at = datetime.now(timezone.utc)
    db.commit()
    return {"status": "approved"}


@app.post("/api/reports/daily/{report_date}/send")
async def send_daily_report(report_date: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    report = db.scalar(select(DailyReport).where(DailyReport.user_id == user.id, DailyReport.report_date == report_date))
    if not report:
        raise HTTPException(status_code=404, detail="日报不存在")
    if report.approval_required and report.status != "approved":
        raise HTTPException(status_code=409, detail="该日报需要先审核通过")
    try:
        response = await send_report(db, user.id, report)
    except (ValueError, NotificationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "sent", "response": response}


@app.post("/api/report-settings/test-wecom")
async def test_wecom(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    try:
        result = await send_wecom(db, user.id, "## SEMI-KB 控制台连接测试\n> 企业微信机器人配置有效。")
    except NotificationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return result


@app.post("/api/report-settings/test-email")
async def test_email(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    try:
        await send_email_reminder(db, user.id, "SEMI-KB 邮件连接测试", "QQ SMTP 配置有效。")
    except NotificationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"status": "sent"}


@app.post("/api/exports", status_code=202)
async def start_export(payload: ExportCreate, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    job = ExportJob(id="export-" + uuid.uuid4().hex[:16], user_id=user.id, kind=payload.kind)
    db.add(job); db.commit()
    asyncio.create_task(export_job_task(job.id))
    return {"id": job.id, "status": job.status, "kind": job.kind}


async def export_job_task(job_id: str) -> None:
    with SessionLocal() as db:
        job = db.get(ExportJob, job_id)
        if job:
            await create_export(db, job)


@app.get("/api/exports/{job_id}")
def get_export(job_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    job = db.get(ExportJob, job_id)
    if not job or job.user_id != user.id:
        raise HTTPException(status_code=404, detail="导出任务不存在")
    return {"id": job.id, "kind": job.kind, "status": job.status, "error": job.error, "download_url": f"/api/exports/{job.id}/download" if job.status == "completed" else None}


@app.get("/api/exports/{job_id}/download")
def download_export(job_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    job = db.get(ExportJob, job_id)
    if not job or job.user_id != user.id or job.status != "completed" or not job.path:
        raise HTTPException(status_code=404, detail="导出文件不可用")
    path = Path(job.path)
    if not path.is_file() or path.parent.resolve() != (settings.data_dir / "artifacts").resolve():
        raise HTTPException(status_code=404, detail="导出文件不存在")
    return FileResponse(path, filename=path.name, media_type="application/zip")
