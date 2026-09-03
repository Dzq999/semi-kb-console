from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qsl, urlparse
from zoneinfo import ZoneInfo

import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Cookie, Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from rdflib import Graph, RDF, RDFS, URIRef
from rdflib.namespace import OWL
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .db import Base, SessionLocal, engine, get_db
from .migrations import upgrade_database
from .models import AgentIteration, AgentRun, Article, ArticleAsset, ArticleRevision, ArticleSetting, ArticleTopic, BusinessBaselineSchedule, BusinessDraft, DailyReport, DailyReportRevision, EncryptedCredential, ExportJob, LoopSetting, NotificationRecord, ReportSetting, Run, RunEvent, RunRound, User, UserPreference
from .schemas import ArticleGenerateRequest, ArticleSettingsUpdate, ArticleUpdate, BaselineScheduleUpdate, BatchApproveRequest, BusinessDraftRequest, CredentialUpdate, DefaultModelUpdate, ExportCreate, LoginRequest, LoopUpdate, ReportContentUpdate, ReportSettingsUpdate, RunCreate, RunResumeRequest, SetupRequest
from .security import decrypt_secret, encrypt_secret, hash_password, new_session_token, verify_password
from .services.exports import create_export, recover_export_jobs
from .services.llm import ExternalServiceError, llm_service, user_api_key
from .services.notifications import NotificationError, send_email_reminder, send_wecom
from .services.wechat_publisher import WechatPublisherError, wechat_publisher
from .services.orchestrator import orchestrator
from .services.checkpoints import checkpoint_runtime
from .services.reports import generate_report, send_report, validate_report
from .services.articles import _markdown_to_wechat_html, discover_topics, generate_article, generate_daily_batch, validate_article
from .services.business_assistant import discard_draft_files, draft_business_baseline, draft_dir_path, generate_baseline_batch, load_draft_documents
from .services.semi_kb import SemiKbError, semi_kb


def prepare_recoverable_runs(db: Session) -> list[str]:
    recover_ids: list[str] = []
    # Older workers could leave a pending parent row after its round failed.
    # Reconcile that durable state on startup so the UI does not show a spinner
    # for a task that has already stopped and needs operator attention.
    for run in db.scalars(select(Run).where(Run.status == "pending")).all():
        latest_round = max(run.rounds, key=lambda item: item.round_number, default=None)
        if latest_round and latest_round.status == "failed":
            run.status = "needs_attention"
            run.current_stage = "round_failed"
            run.error = run.error or f"第 {latest_round.round_number} 轮失败，任务已停止，请检查校验原因"
            run.completed_at = run.completed_at or latest_round.completed_at or datetime.now(timezone.utc)
    # A cancellation is terminal across a backend restart. Resuming it would
    # silently restart a task the operator explicitly stopped.
    for run in db.scalars(select(Run).where(Run.status == "cancelling")).all():
        run.status = "cancelled"
        run.current_stage = "cancelled"
        run.completed_at = run.completed_at or datetime.now(timezone.utc)
        run.worker_id = None
        run.heartbeat_at = run.completed_at
        run.error = run.error or "后端重启时完成停止"
    interrupted = db.scalars(select(Run).where(Run.status.in_(["running", "recovering", "paused", "between_rounds", "stopping_after_round"]))).all()
    for run in interrupted:
        if run.orchestrator_engine != "langgraph" or not settings.auto_resume_runs:
            run.status = "interrupted"
            run.error = "后端重启导致 legacy 运行中断，可从运行历史重新启动"
        elif run.pause_requested:
            run.status = "paused"
            run.worker_id = None
        else:
            run.status = "recovering"
            run.recovery_count = int(run.recovery_count or 0) + 1
            run.worker_id = None
            recover_ids.append(run.id)
    db.commit()
    return recover_ids


def ensure_article_settings(db: Session) -> None:
    existing = {row.user_id for row in db.scalars(select(ArticleSetting)).all()}
    users = db.scalars(select(User)).all()
    missing = [ArticleSetting(user_id=user.id) for user in users if user.id not in existing]
    if missing:
        db.add_all(missing)
        db.commit()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    upgrade_database()
    Base.metadata.create_all(engine)
    await checkpoint_runtime.startup()
    with SessionLocal() as db:
        recover_ids = prepare_recoverable_runs(db)
        export_ids = recover_export_jobs(db)
        ensure_article_settings(db)
    scheduler.add_job(scheduler_tick, "interval", seconds=60, id="scheduler-tick", max_instances=1, coalesce=True, replace_existing=True)
    scheduler.start()
    for run_id in recover_ids:
        orchestrator.start(run_id)
    for export_id in export_ids:
        asyncio.create_task(export_job_task(export_id))
    try:
        yield
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        await checkpoint_runtime.shutdown()


app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[settings.frontend_origin], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
scheduler = AsyncIOScheduler(timezone=settings.timezone)
sessions: dict[str, int] = {}
model_cache: dict[str, dict[str, object]] = {}


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


_REFERENCE_SENSITIVE_QUERY_KEYS = {
    "access_token", "api_key", "apikey", "auth", "authorization", "key",
    "password", "passwd", "secret", "signature", "sig", "token",
}


def _public_reference_url(value: object) -> str | None:
    """Return only a public URL safe to render as an external reference."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if len(candidate) > 2048:
        return None
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    hostname = parsed.hostname.casefold().rstrip(".")
    if hostname in {"localhost", "localhost.localdomain"}:
        return None
    try:
        address = ip_address(hostname)
    except ValueError:
        address = None
    if address and (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved):
        return None
    if any(key.casefold() in _REFERENCE_SENSITIVE_QUERY_KEYS for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
        return None
    return candidate


def _run_references(db: Session, run_id: str) -> list[dict]:
    """Flatten AgentIteration evidence for a run while retaining provenance."""
    rows = db.scalars(
        select(AgentIteration)
        .where(AgentIteration.run_id == run_id)
        .order_by(AgentIteration.round_number.desc(), AgentIteration.agent_id)
    ).all()
    agents = {item.id: item for item in db.scalars(select(AgentRun).where(AgentRun.run_id == run_id)).all()}
    references: list[dict] = []
    seen: dict[str, int] = {}
    for iteration in rows:
        agent = agents.get(iteration.agent_id)
        for evidence in json_load(iteration.evidence_json, []):
            if not isinstance(evidence, dict):
                continue
            url = _public_reference_url(evidence.get("url"))
            if not url:
                continue
            source_type = str(evidence.get("source_type") or "web").casefold()
            if source_type not in {"web", "web_search", "external"}:
                continue
            existing_index = seen.get(url)
            provenance = {"round": iteration.round_number, "agent_id": iteration.agent_id, "agent_name": agent.name if agent else iteration.agent_id}
            if existing_index is not None:
                existing = references[existing_index]
                if provenance not in existing["provenance"]:
                    existing["provenance"].append(provenance)
                continue
            seen[url] = len(references)
            references.append({
                "title": str(evidence.get("title") or url)[:300],
                "url": url,
                "source_type": source_type,
                "fetch_status": str(evidence.get("fetch_status") or "unknown"),
                "excerpt": str(evidence.get("excerpt") or "")[:1200],
                "retrieved_at": evidence.get("retrieved_at"),
                "provenance": [provenance],
            })
    return references


def run_payload(run: Run) -> dict:
    config = json_load(run.config_json, {})
    current_round = max((item.round_number for item in run.rounds), default=0)
    return {
        "id": run.id, "model_id": run.model_id, "status": run.status, "current_stage": run.current_stage,
        "progress": run.progress, "created_at": run.created_at, "started_at": run.started_at,
        "completed_at": run.completed_at, "error": run.error, "current_round": current_round,
        "rounds_completed": sum(item.status in {"completed", "completed_partial", "completed_no_change"} for item in run.rounds),
        "continuous": config.get("continuous", True), "publish_changes": config.get("publish_changes", False),
        "round_interval_seconds": config.get("round_interval_seconds", 5),
        "max_consecutive_round_failures": config.get("max_consecutive_round_failures", 3),
        "auto_repair": config.get("auto_repair", True),
        "max_auto_repair_attempts": config.get("max_auto_repair_attempts", 1),
        "repair_follow_failure_threshold": config.get("repair_follow_failure_threshold", True),
        "stop_after_round": run.stop_after_round,
        "orchestrator_engine": run.orchestrator_engine,
        "checkpoint_backend": checkpoint_runtime.backend,
        "checkpoint_thread_id": run.checkpoint_thread_id,
        "heartbeat_at": run.heartbeat_at,
        "recovery_count": run.recovery_count,
        "pause_requested": run.pause_requested,
        "cancel_requested": run.cancel_requested,
        "agents": [{"id": a.id, "name": a.name, "role": a.role, "domain": a.domain, "source_mode": a.source_mode, "model_id": a.model_id, "status": a.status, "duration_seconds": a.duration_seconds, "error": a.error, "output": json_load(a.output_json, {})} for a in run.agents],
    }


def create_run(db: Session, user_id: int, request: RunCreate) -> Run:
    run_id = "run-" + uuid.uuid4().hex[:16]
    config = request.model_dump()
    config["orchestrator_engine"] = settings.orchestrator_engine
    run = Run(
        id=run_id, user_id=user_id, model_id=request.model_id,
        config_json=json.dumps(config, ensure_ascii=False), orchestrator_engine=settings.orchestrator_engine,
        checkpoint_thread_id=f"{run_id}:round:1",
    )
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


async def generate_article_job(user_id: int, topic_id: int, model_id: str, approval_required: bool, auto_visuals: bool, image_model_id: str | None = None, image_count: int = 1, auto_repair: bool = True, max_repair_attempts: int = 3) -> None:
    with SessionLocal() as db:
        topic = db.get(ArticleTopic, topic_id)
        if not topic:
            return
        try:
            await generate_article(db, user_id, topic, model_id, approval_required, auto_visuals, image_model_id, image_count, auto_repair=auto_repair, max_repair_attempts=max_repair_attempts)
        except Exception as exc:
            article = db.scalar(select(Article).where(Article.user_id == user_id, Article.topic_id == topic_id, Article.status == "generating").order_by(Article.id.desc()).limit(1))
            if article:
                article.status = "blocked"
                article.generation_stage = "生成失败"
                article.generation_progress = 100
                article.generation_error = type(exc).__name__
            topic.status = "blocked"
            db.commit()


async def generate_article_batch_job(user_id: int, model_id: str, approval_required: bool, auto_visuals: bool, image_model_id: str | None, image_count: int, count: int, generation_date: str) -> None:
    with SessionLocal() as db:
        setting = db.get(ArticleSetting, user_id) or ArticleSetting(user_id=user_id)
        setting.approval_required = approval_required
        setting.auto_visuals = auto_visuals
        try:
            articles = await generate_daily_batch(db, user_id, setting, model_id, image_model_id, image_count, count, generation_date)
            setting.last_generated_date = generation_date if articles else None
            db.commit()
        except Exception:
            # Individual articles are isolated by generate_daily_batch; this guard keeps the scheduler alive.
            db.rollback()


async def baseline_batch_job(user_id: int, count: int, model_id: str, generation_date: str) -> None:
    """定时批量起草经营基线草案。单份由 generate_baseline_batch 内部隔离；此守卫保 scheduler 存活。"""
    with SessionLocal() as db:
        setting = db.get(BusinessBaselineSchedule, user_id)
        try:
            results = await generate_baseline_batch(db, user_id, count, model_id)
            if setting and not any(item.get("status") in {"validated", "invalid"} for item in results):
                # 一份都没落成草案（全批异常）→ 允许下一次 tick 重试，不锁死当日。
                setting.last_generated_date = None
                db.commit()
        except Exception:
            db.rollback()
            if setting:
                setting.last_generated_date = None
                db.commit()


async def scheduler_tick() -> None:
    zone = ZoneInfo(settings.timezone)
    now = datetime.now(zone)
    with SessionLocal() as db:
        for row in db.scalars(select(ReportSetting).where(ReportSetting.enabled.is_(True))).all():
            if row.generate_time == now.strftime("%H:%M"):
                date_text = now.date().isoformat()
                preference = db.get(UserPreference, row.user_id)
                trigger_key = f"{date_text}T{row.generate_time}"
                # Reuse the daily row so a later schedule on the same day can
                # regenerate/send a fresh report; deduplicate only this minute.
                if row.last_trigger_key != trigger_key and preference and preference.default_model_id:
                    row.last_trigger_key = trigger_key
                    db.commit()
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
            active = db.scalar(select(func.count(Run.id)).where(Run.user_id == loop.user_id, Run.status.in_(["pending", "running", "recovering", "paused", "between_rounds", "stopping_after_round", "cancelling"])))
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
        for setting in db.scalars(select(ArticleSetting).where(ArticleSetting.enabled.is_(True))).all():
            try:
                zone = ZoneInfo(setting.timezone or settings.timezone)
            except Exception:
                zone = ZoneInfo(settings.timezone)
            local_now = datetime.now(zone)
            if setting.generate_time != local_now.strftime("%H:%M") or setting.last_generated_date == local_now.date().isoformat():
                continue
            preference = db.get(UserPreference, setting.user_id)
            if not preference or not preference.default_model_id:
                continue
            discover_topics(db, setting.user_id)
            topics = db.scalars(select(ArticleTopic).where(ArticleTopic.user_id == setting.user_id, ArticleTopic.status == "qualified", ArticleTopic.used_at.is_(None)).order_by(ArticleTopic.priority_score.desc(), ArticleTopic.novelty_score.desc(), ArticleTopic.created_at.asc()).limit(setting.daily_article_count)).all()
            if len(topics) < setting.daily_article_count:
                discover_topics(db, setting.user_id)
                topics = db.scalars(select(ArticleTopic).where(ArticleTopic.user_id == setting.user_id, ArticleTopic.status == "qualified", ArticleTopic.used_at.is_(None)).order_by(ArticleTopic.priority_score.desc(), ArticleTopic.novelty_score.desc(), ArticleTopic.created_at.asc()).limit(setting.daily_article_count)).all()
            if not topics:
                continue
            setting.last_generated_date = local_now.date().isoformat()
            db.commit()
            asyncio.create_task(generate_article_batch_job(setting.user_id, setting.article_model_id or preference.default_model_id, setting.approval_required, setting.auto_visuals, setting.image_model_id, setting.image_count, setting.daily_article_count, local_now.date().isoformat()))
        for setting in db.scalars(select(BusinessBaselineSchedule).where(BusinessBaselineSchedule.enabled.is_(True))).all():
            try:
                zone = ZoneInfo(setting.timezone or settings.timezone)
            except Exception:
                zone = ZoneInfo(settings.timezone)
            local_now = datetime.now(zone)
            if setting.generate_time != local_now.strftime("%H:%M") or setting.last_generated_date == local_now.date().isoformat():
                continue
            preference = db.get(UserPreference, setting.user_id)
            model_id = setting.llm_model_id or (preference.default_model_id if preference else None)
            if not model_id:
                continue
            # 先占位当日去重，再异步起草：定时只起草+校验，落盘仍由人批量采纳（治理边界不破）。
            setting.last_generated_date = local_now.date().isoformat()
            db.commit()
            asyncio.create_task(baseline_batch_job(setting.user_id, setting.daily_count, model_id, local_now.date().isoformat()))


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok", "app": settings.app_name, "engine_root": str(settings.engine_root),
        "max_agents": settings.max_agent_count, "database": engine.dialect.name,
        "orchestrator_engine": settings.orchestrator_engine, "checkpoint_backend": checkpoint_runtime.backend,
    }


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
    db.add(ArticleSetting(user_id=user.id))
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
    cache_key = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    cached = model_cache.setdefault(cache_key, {"items": [], "fetched_at": None})
    fetched_at = cached.get("fetched_at")
    stale = not fetched_at or datetime.now(timezone.utc) - fetched_at > timedelta(minutes=5)
    try:
        if refresh or stale or not cached["items"]:
            cached["items"] = await llm_service.list_models(api_key)
            cached["fetched_at"] = datetime.now(timezone.utc)
    except ExternalServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    items = cached["items"]
    if search:
        items = [item for item in items if search.casefold() in item["id"].casefold()]
    preference = db.get(UserPreference, user.id)
    return {"items": items, "total": len(items), "default_model_id": preference.default_model_id, "fetched_at": cached["fetched_at"]}


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
        model_cache.clear()
    return {"kind": payload.kind, "configured": True, "masked_hint": hint}


@app.delete("/api/credentials/{kind}", status_code=204)
def delete_credential(kind: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> Response:
    row = db.scalar(select(EncryptedCredential).where(EncryptedCredential.user_id == user.id, EncryptedCredential.kind == kind))
    if row:
        db.delete(row)
        db.commit()
    if kind == "llm_api_key":
        model_cache.clear()
    return Response(status_code=204)


def _stored_credential(db: Session, user_id: int, kind: str) -> str:
    row = db.scalar(select(EncryptedCredential).where(EncryptedCredential.user_id == user_id, EncryptedCredential.kind == kind))
    if not row:
        raise HTTPException(status_code=424, detail=f"未配置 {kind}")
    try:
        return decrypt_secret(row.ciphertext)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="凭据无法解密，请重新配置") from exc


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
    active = db.scalar(select(Run).where(Run.user_id == user.id, Run.status.in_(["pending", "running", "recovering", "paused", "between_rounds", "stopping_after_round", "cancelling"])))
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
    if run.status not in {"running", "recovering", "between_rounds", "stopping_after_round"}:
        raise HTTPException(status_code=409, detail="当前状态不能暂停")
    orchestrator.pause(run_id)
    run.status = "paused"
    db.commit()
    return {"status": "paused"}


@app.post("/api/runs/{run_id}/resume")
async def resume_run(run_id: str, payload: RunResumeRequest | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    run = db.get(Run, run_id)
    if not run or run.user_id != user.id:
        raise HTTPException(status_code=404, detail="任务不存在")
    if run.status in {"needs_attention", "pending", "paused"}:
        if payload and any(value is not None for value in (payload.max_consecutive_round_failures, payload.auto_repair, payload.max_auto_repair_attempts, payload.repair_follow_failure_threshold)):
            config = json_load(run.config_json, {})
            if payload.max_consecutive_round_failures is not None:
                config["max_consecutive_round_failures"] = payload.max_consecutive_round_failures
            if payload.auto_repair is not None:
                config["auto_repair"] = payload.auto_repair
            if payload.max_auto_repair_attempts is not None:
                config["max_auto_repair_attempts"] = payload.max_auto_repair_attempts
            if payload.repair_follow_failure_threshold is not None:
                config["repair_follow_failure_threshold"] = payload.repair_follow_failure_threshold
            run.config_json = json.dumps(config, ensure_ascii=False)
        run.error = None; run.pause_requested = False; run.cancel_requested = False
        if run.status == "paused":
            orchestrator.resume(run_id); run.status = "running"; db.commit()
            return {"status": "running"}
        run.status = "pending"; db.commit(); orchestrator.start(run_id)
        return {"status": "pending"}
    raise HTTPException(status_code=409, detail="当前状态不能恢复")


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
    if run.status not in {"pending", "running", "recovering", "paused", "between_rounds", "stopping_after_round"}:
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
            "checkpoint_id": row.checkpoint_id, "resumed_count": row.resumed_count,
            "node_attempts": json_load(row.node_attempts_json, {}), "quarantined_files": json_load(row.quarantined_files_json, []),
            "agents": [{
                "agent_id": item.agent_id, "status": item.status, "duration_seconds": item.duration_seconds,
                "attempt_count": item.attempt_count, "checkpoint_id": item.checkpoint_id, "input_hash": item.input_hash,
                "evidence": json_load(item.evidence_json, []), "output": json_load(item.output_json, {}), "error": item.error,
            } for item in iterations],
        })
    return {"items": items, "total": len(run.rounds)}


@app.get("/api/runs/{run_id}/references")
def run_references(run_id: str, limit: int = Query(default=500, ge=1, le=5000), user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Return safe, deduplicated web references collected by every Agent iteration."""
    run = db.get(Run, run_id)
    if not run or run.user_id != user.id:
        raise HTTPException(status_code=404, detail="任务不存在")
    references = _run_references(db, run_id)
    return {"items": references[:limit], "total": len(references)}


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
    for path in sorted((settings.engine_root / "ontology" / "modules").glob("*.ttl")):
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


@app.get("/api/ontology/tree")
def ontology_tree(user: User = Depends(current_user)) -> dict:
    """类层级视图：逐模块解析，返回每个 owl:Class 的 rdfs:subClassOf 父类与来源模块。

    仅读取现有本体，不改动任何数据；前端据此把 488 个类还原成 subClassOf 树。
    """
    nodes: dict[str, dict] = {}
    for path in sorted((settings.engine_root / "ontology" / "modules").glob("*.ttl")):
        graph = Graph()
        graph.parse(path, format="turtle")
        module = path.stem
        for subject in graph.subjects(RDF.type, OWL.Class):
            if not isinstance(subject, URIRef):
                continue  # 跳过匿名类 / owl:Restriction 等空节点
            iri = str(subject)
            node = nodes.get(iri)
            if node is None:
                node = {"iri": iri, "label": "", "parents": [], "module": module}
                nodes[iri] = node
            label = next(graph.objects(subject, RDFS.label), None)
            if label and not node["label"]:
                node["label"] = str(label)
            for parent in graph.objects(subject, RDFS.subClassOf):
                if isinstance(parent, URIRef):
                    parent_iri = str(parent)
                    if parent_iri != iri and parent_iri not in node["parents"]:
                        node["parents"].append(parent_iri)
    for node in nodes.values():
        if not node["label"]:
            node["label"] = node["iri"].rsplit(":", 1)[-1].rsplit("/", 1)[-1]
    ordered = sorted(nodes.values(), key=lambda item: item["iri"])
    return {"nodes": ordered, "total": len(ordered)}


def yaml_catalog(pattern: str) -> list[dict]:
    items = []
    for path in sorted(settings.engine_root.glob(pattern)):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            doc = None
        items.append({"path": path.relative_to(settings.engine_root).as_posix(), "name": path.stem, "document": doc})
    return items


@app.get("/api/knowledge/facts")
def knowledge_facts(user: User = Depends(current_user)) -> dict:
    return {"items": yaml_catalog("kb/**/*.yaml")}


@app.get("/api/business-models")
def business_models(user: User = Depends(current_user)) -> dict:
    return {"items": yaml_catalog("business/models/*.yaml")}


def _draft_summary(row: BusinessDraft) -> dict:
    validation = json_load(row.validation_json, {})
    return {
        "draft_id": row.id, "status": row.status, "intent": row.intent, "domain": row.domain,
        "summary": row.summary, "llm_model_id": row.llm_model_id,
        "validation": validation, "promoted_paths": json_load(row.promoted_paths_json, {}),
        "created_at": row.created_at, "approved_at": row.approved_at, "rejected_at": row.rejected_at,
    }


@app.post("/api/business-models/draft")
async def draft_business_model(payload: BusinessDraftRequest, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """独立经营协作 Agent：人触发 LLM 起草完整三件套基线并做引擎门禁校验（不落线上）。"""
    try:
        return await draft_business_baseline(db, user, payload.intent, payload.domain, payload.model_id)
    except ExternalServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (ValueError, SemiKbError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/business-models/drafts")
def list_business_drafts(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    rows = db.scalars(select(BusinessDraft).where(BusinessDraft.user_id == user.id).order_by(BusinessDraft.created_at.desc())).all()
    return {"items": [_draft_summary(row) for row in rows]}


@app.get("/api/business-models/drafts/{draft_id}")
def get_business_draft(draft_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    row = db.get(BusinessDraft, draft_id)
    if not row or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="草案不存在")
    detail = _draft_summary(row)
    try:
        detail["documents"] = load_draft_documents(draft_id)
    except SemiKbError:
        detail["documents"] = None  # 已丢弃/晋升后磁盘目录可能已不在
    return detail


@app.post("/api/business-models/drafts/{draft_id}/approve")
async def approve_business_draft(draft_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """人点『采纳为基线』：复检门禁通过后晋升三件套到线上 business/{templates,datasets,models}/。"""
    row = db.get(BusinessDraft, draft_id)
    if not row or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="草案不存在")
    if row.status == "approved":
        return _draft_summary(row)
    validation = json_load(row.validation_json, {})
    if not validation.get("passed"):
        raise HTTPException(status_code=422, detail=validation.get("errors", ["草案未通过引擎校验，无法采纳为基线"]))
    try:
        promoted = await semi_kb.promote_business_draft(draft_dir_path(draft_id))
    except SemiKbError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    row.status = "approved"
    row.approved_at = datetime.now(timezone.utc)
    row.promoted_paths_json = json.dumps(promoted, ensure_ascii=False)
    db.commit()
    discard_draft_files(draft_id)
    return _draft_summary(row)


@app.post("/api/business-models/drafts/{draft_id}/reject")
def reject_business_draft(draft_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    row = db.get(BusinessDraft, draft_id)
    if not row or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="草案不存在")
    if row.status != "approved":
        row.status = "rejected"
        row.rejected_at = datetime.now(timezone.utc)
        db.commit()
    discard_draft_files(draft_id)
    return _draft_summary(row)


@app.post("/api/business-models/drafts/approve-batch")
async def approve_business_drafts_batch(payload: BatchApproveRequest, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """批量采纳：对每个草案复检归属+门禁通过后串行晋升。串行是必须的——promote 内部持
    _candidate_lock，且每次晋升都会改动全库门禁看到的 business/models/。逐个隔离，部分成功不影响其余。"""
    results: list[dict] = []
    for draft_id in payload.draft_ids:
        row = db.get(BusinessDraft, draft_id)
        if not row or row.user_id != user.id:
            results.append({"draft_id": draft_id, "ok": False, "error": "草案不存在"})
            continue
        if row.status == "approved":
            results.append({"draft_id": draft_id, "ok": True, "status": "approved", "skipped": True})
            continue
        validation = json_load(row.validation_json, {})
        if not validation.get("passed"):
            results.append({"draft_id": draft_id, "ok": False, "error": "未通过引擎校验，无法采纳"})
            continue
        try:
            promoted = await semi_kb.promote_business_draft(draft_dir_path(draft_id))
        except (SemiKbError, ValueError) as exc:
            db.rollback()
            results.append({"draft_id": draft_id, "ok": False, "error": str(exc)})
            continue
        row.status = "approved"
        row.approved_at = datetime.now(timezone.utc)
        row.promoted_paths_json = json.dumps(promoted, ensure_ascii=False)
        db.commit()
        discard_draft_files(draft_id)
        results.append({"draft_id": draft_id, "ok": True, "status": "approved"})
    return {"results": results, "approved": sum(1 for item in results if item.get("ok"))}


@app.get("/api/business-models/schedule")
def get_baseline_schedule(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    row = db.get(BusinessBaselineSchedule, user.id) or BusinessBaselineSchedule(user_id=user.id)
    if db.get(BusinessBaselineSchedule, user.id) is None:
        db.add(row); db.commit()
    return {"enabled": row.enabled, "generate_time": row.generate_time, "timezone": row.timezone, "daily_count": row.daily_count, "llm_model_id": row.llm_model_id, "domain_strategy": row.domain_strategy, "last_generated_date": row.last_generated_date}


@app.put("/api/business-models/schedule")
def update_baseline_schedule(payload: BaselineScheduleUpdate, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    row = db.get(BusinessBaselineSchedule, user.id) or BusinessBaselineSchedule(user_id=user.id)
    row.enabled = payload.enabled
    row.generate_time = payload.generate_time
    row.daily_count = payload.daily_count
    row.llm_model_id = payload.llm_model_id
    row.domain_strategy = payload.domain_strategy
    db.add(row); db.commit()
    return {"enabled": row.enabled, "generate_time": row.generate_time, "timezone": row.timezone, "daily_count": row.daily_count, "llm_model_id": row.llm_model_id, "domain_strategy": row.domain_strategy, "last_generated_date": row.last_generated_date}


@app.get("/api/simulations")
def simulations(user: User = Depends(current_user)) -> dict:
    return {"items": yaml_catalog("simulation/scenarios/*.yaml")}


@app.get("/api/scenario-articles")
def scenario_articles(user: User = Depends(current_user)) -> dict:
    path = settings.engine_root / "knowledge" / "articles" / "current-scenarios.md"
    return {"content": path.read_text(encoding="utf-8") if path.is_file() else "", "path": path.relative_to(settings.engine_root).as_posix()}


def _scenario_knowledge_paths() -> list[Path]:
    root = settings.engine_root
    paths: list[Path] = []
    current = root / "knowledge" / "articles" / "current-scenarios.md"
    if current.is_file():
        paths.append(current)
    paths.extend(sorted((root / "knowledge" / "articles" / "agent-rounds").glob("*.md")))
    return paths


@app.get("/api/scenario-knowledge")
def scenario_knowledge(user: User = Depends(current_user)) -> dict:
    """List human-readable scenario knowledge products, excluding公众号草稿。"""
    zone = ZoneInfo(settings.timezone)
    today = datetime.now(zone).date()
    items = []
    for path in _scenario_knowledge_paths():
        try:
            stat = path.stat()
            updated_at = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
            items.append({
                "path": path.relative_to(settings.engine_root).as_posix(),
                "name": path.stem,
                "size": stat.st_size,
                "updated_at": updated_at,
                "today_added": updated_at.astimezone(zone).date() == today,
            })
        except OSError:
            continue
    items.sort(key=lambda item: item["updated_at"], reverse=True)
    return {"items": items, "total": len(items), "today_added": sum(1 for item in items if item["today_added"])}


@app.get("/api/scenario-knowledge/file")
def scenario_knowledge_file(path: str = Query(..., min_length=1), user: User = Depends(current_user)) -> dict:
    candidate = _safe_catalog_path(path)
    allowed = set(_scenario_knowledge_paths())
    if candidate not in allowed or candidate.suffix.lower() != ".md":
        raise HTTPException(status_code=403, detail="不允许访问该场景知识产物")
    content = candidate.read_text(encoding="utf-8")
    return {"path": candidate.relative_to(settings.engine_root).as_posix(), "content": content, "size": len(content)}


def topic_payload(topic: ArticleTopic) -> dict:
    return {"id": topic.id, "title": topic.title, "domain": topic.domain, "customer_role": topic.customer_role, "pain_point": topic.pain_point, "business_context": topic.business_context, "evidence": json_load(topic.evidence_json, []), "priority_score": topic.priority_score, "novelty_score": topic.novelty_score, "status": topic.status, "created_at": topic.created_at, "used_at": topic.used_at}


@app.get("/api/article-topics")
def list_article_topics(status_filter: str | None = Query(default=None, alias="status"), user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    query = select(ArticleTopic).where(ArticleTopic.user_id == user.id).order_by(ArticleTopic.priority_score.desc(), ArticleTopic.created_at.desc())
    if status_filter:
        query = query.where(ArticleTopic.status == status_filter)
    return {"items": [topic_payload(topic) for topic in db.scalars(query.limit(200)).all()]}


@app.post("/api/article-topics/discover")
def discover_article_topics(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    return {"created": discover_topics(db, user.id)}


@app.post("/api/article-topics/{topic_id}/queue")
def queue_article_topic(topic_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    topic = db.get(ArticleTopic, topic_id)
    if not topic or topic.user_id != user.id:
        raise HTTPException(status_code=404, detail="主题不存在")
    topic.status = "qualified"; topic.used_at = None; db.commit()
    return topic_payload(topic)


@app.post("/api/article-topics/{topic_id}/reject")
def reject_article_topic(topic_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    topic = db.get(ArticleTopic, topic_id)
    if not topic or topic.user_id != user.id:
        raise HTTPException(status_code=404, detail="主题不存在")
    topic.status = "rejected"; db.commit()
    return topic_payload(topic)


def article_payload(article: Article, db: Session) -> dict:
    topic = db.get(ArticleTopic, article.topic_id) if article.topic_id else None
    assets = db.scalars(select(ArticleAsset).where(ArticleAsset.article_id == article.id)).all()
    return {"id": article.id, "topic": topic_payload(topic) if topic else None, "title": article.title, "subtitle": article.subtitle, "status": article.status, "generation_stage": article.generation_stage, "generation_progress": article.generation_progress, "generation_error": article.generation_error, "repair_attempts": article.repair_attempts, "approval_required": article.approval_required, "content_markdown": article.content_markdown, "content_html": _markdown_to_wechat_html(article.content_markdown, article.id, assets), "validation": json_load(article.validation_json, {}), "metrics_snapshot": json_load(article.metrics_snapshot_json, {}), "word_count": article.word_count, "ai_tone_score": article.ai_tone_score, "factual_score": article.factual_score, "generation_date": article.generation_date, "sequence_no": article.sequence_no, "article_model_id": article.article_model_id, "image_model_id": article.image_model_id, "cover_prompt": article.cover_prompt, "generated_at": article.generated_at, "approved_at": article.approved_at, "published_at": article.published_at, "wechat_status": article.wechat_status, "wechat_draft_media_id": article.wechat_draft_media_id, "wechat_last_error": article.wechat_last_error, "wechat_sent_at": article.wechat_sent_at, "assets": [{"id": a.id, "asset_type": a.asset_type, "file_path": a.file_path, "mime_type": a.mime_type, "caption": a.caption} for a in assets]}


@app.get("/api/articles")
def list_articles(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    items = db.scalars(select(Article).where(Article.user_id == user.id, Article.deleted_at.is_(None)).order_by(Article.created_at.desc()).limit(100)).all()
    return {"items": [article_payload(article, db) for article in items]}


@app.post("/api/articles/generate", status_code=202)
async def create_article(payload: ArticleGenerateRequest, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    preference = db.get(UserPreference, user.id)
    model_id = payload.model_id or (preference.default_model_id if preference else None)
    if not model_id:
        raise HTTPException(status_code=422, detail="请先设置默认模型")
    if payload.topic_id:
        topic = db.get(ArticleTopic, payload.topic_id)
    else:
        discover_topics(db, user.id)
        topic = db.scalar(select(ArticleTopic).where(ArticleTopic.user_id == user.id, ArticleTopic.status == "qualified", ArticleTopic.used_at.is_(None)).order_by(ArticleTopic.priority_score.desc()).limit(1))
    if not topic or topic.user_id != user.id:
        raise HTTPException(status_code=404, detail="没有可生成的主题")
    setting = db.get(ArticleSetting, user.id) or ArticleSetting(user_id=user.id)
    db.add(setting); db.commit()
    asyncio.create_task(generate_article_job(user.id, topic.id, setting.article_model_id or model_id, setting.approval_required, setting.auto_visuals, setting.image_model_id, setting.image_count, setting.auto_repair, setting.max_repair_attempts))
    return {"topic_id": topic.id, "status": "generating"}


@app.get("/api/articles/{article_id}")
def get_article(article_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    article = db.get(Article, article_id)
    if not article or article.user_id != user.id or article.deleted_at:
        raise HTTPException(status_code=404, detail="文章不存在")
    return article_payload(article, db)


@app.delete("/api/articles/{article_id}", status_code=204)
def delete_article(article_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> Response:
    article = db.get(Article, article_id)
    if not article or article.user_id != user.id or article.deleted_at:
        raise HTTPException(status_code=404, detail="文章不存在")
    article.deleted_at = datetime.now(timezone.utc); article.status = "deleted"; db.commit()
    return Response(status_code=204)


@app.patch("/api/articles/{article_id}")
def update_article(article_id: int, payload: ArticleUpdate, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    article = db.get(Article, article_id)
    if not article or article.user_id != user.id or article.deleted_at:
        raise HTTPException(status_code=404, detail="文章不存在")
    if payload.content_markdown is not None:
        topic = db.get(ArticleTopic, article.topic_id) if article.topic_id else ArticleTopic(title=article.title, user_id=user.id)
        article.content_markdown = payload.content_markdown
        assets = db.scalars(select(ArticleAsset).where(ArticleAsset.article_id == article.id)).all()
        article.content_html = _markdown_to_wechat_html(payload.content_markdown, article.id, assets)
        validation = validate_article(article.content_markdown, topic, json_load(topic.evidence_json, []), article.title)
        article.validation_json = json.dumps(validation, ensure_ascii=False); article.word_count = validation["word_count"]; article.ai_tone_score = validation["ai_tone_score"]; article.factual_score = validation["factual_score"]
        db.add(ArticleRevision(article_id=article.id, content_markdown=payload.content_markdown, editor_user_id=user.id, revision_note=payload.revision_note))
    if payload.title is not None:
        article.title = payload.title
    article.status = "waiting_approval" if json_load(article.validation_json, {}).get("passed") and article.approval_required else article.status
    db.commit(); return article_payload(article, db)


@app.post("/api/articles/{article_id}/validate")
def validate_article_endpoint(article_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    article = db.get(Article, article_id)
    if not article or article.user_id != user.id or article.deleted_at:
        raise HTTPException(status_code=404, detail="文章不存在")
    topic = db.get(ArticleTopic, article.topic_id)
    result = validate_article(article.content_markdown, topic or ArticleTopic(title=article.title, user_id=user.id), json_load(topic.evidence_json, []) if topic else [], article.title)
    result = {**result, "repair_attempts": article.repair_attempts}
    article.validation_json = json.dumps(result, ensure_ascii=False)
    if result["passed"] and article.approval_required:
        article.status = "waiting_approval"
        article.generation_stage = "校验通过，等待审核"
    elif not result["passed"]:
        article.status = "blocked"
        article.generation_stage = "需用户修改"
    db.commit(); return result


@app.post("/api/articles/{article_id}/approve")
def approve_article(article_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    article = db.get(Article, article_id)
    if not article or article.user_id != user.id or article.deleted_at:
        raise HTTPException(status_code=404, detail="文章不存在")
    validation = json_load(article.validation_json, {})
    if not validation.get("passed"):
        raise HTTPException(status_code=422, detail=validation.get("errors", ["文章校验未通过"]))
    article.status = "approved"; article.approved_at = datetime.now(timezone.utc); db.commit(); return {"status": article.status}


@app.post("/api/articles/{article_id}/wechat-draft")
async def send_article_to_wechat_draft(article_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    article = db.get(Article, article_id)
    if not article or article.user_id != user.id or article.deleted_at:
        raise HTTPException(status_code=404, detail="文章不存在")
    if article.wechat_status == "sending":
        raise HTTPException(status_code=409, detail="文章正在发送到公众号草稿箱")
    validation = json_load(article.validation_json, {})
    if article.status in {"generating", "blocked", "deleted"} or not validation.get("passed"):
        raise HTTPException(status_code=422, detail="文章必须完成生成且通过校验后才能发送到公众号草稿箱")
    if article.wechat_status == "sent_to_draft" and article.wechat_draft_media_id:
        return article_payload(article, db)
    app_id = _stored_credential(db, user.id, "wechat_app_id")
    app_secret = _stored_credential(db, user.id, "wechat_app_secret")
    assets = db.scalars(select(ArticleAsset).where(ArticleAsset.article_id == article.id).order_by(ArticleAsset.id)).all()
    article.wechat_status = "sending"
    article.wechat_last_error = None
    db.commit()
    try:
        result = await wechat_publisher.create_draft(
            app_id=app_id,
            app_secret=app_secret,
            title=article.title,
            digest=article.subtitle,
            content_html=_markdown_to_wechat_html(article.content_markdown, article.id, assets),
            assets=assets,
        )
    except WechatPublisherError as exc:
        article.wechat_status = "failed"
        article.wechat_last_error = str(exc)[:500]
        db.commit()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    article.wechat_status = "sent_to_draft"
    article.wechat_draft_media_id = result["media_id"]
    article.wechat_sent_at = datetime.now(timezone.utc)
    article.wechat_response_json = json.dumps(result, ensure_ascii=False)
    db.commit()
    return article_payload(article, db)


@app.get("/api/articles/{article_id}/assets/{asset_id}")
def article_asset(article_id: int, asset_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> FileResponse:
    article = db.get(Article, article_id)
    asset = db.get(ArticleAsset, asset_id)
    if not article or article.user_id != user.id or not asset or asset.article_id != article.id:
        raise HTTPException(status_code=404, detail="文章图片不存在")
    path = Path(asset.file_path).resolve()
    root = (settings.engine_root / "knowledge" / "articles").resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="文章图片文件不存在")
    return FileResponse(path, media_type=asset.mime_type, filename=path.name)


@app.get("/api/article-settings")
def get_article_settings(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    row = db.get(ArticleSetting, user.id) or ArticleSetting(user_id=user.id)
    db.add(row); db.commit()
    return {"enabled": row.enabled, "frequency": row.frequency, "generate_time": row.generate_time, "timezone": row.timezone, "approval_required": row.approval_required, "auto_visuals": row.auto_visuals, "daily_article_count": row.daily_article_count, "article_model_id": row.article_model_id, "image_model_id": row.image_model_id, "image_count": row.image_count, "auto_repair": row.auto_repair, "max_repair_attempts": row.max_repair_attempts, "last_generated_date": row.last_generated_date}


@app.put("/api/article-settings")
def update_article_settings(payload: ArticleSettingsUpdate, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    row = db.get(ArticleSetting, user.id) or ArticleSetting(user_id=user.id)
    for key, value in payload.model_dump().items(): setattr(row, key, value)
    db.add(row); db.commit(); return {"enabled": row.enabled, "frequency": row.frequency, "generate_time": row.generate_time, "timezone": row.timezone, "approval_required": row.approval_required, "auto_visuals": row.auto_visuals, "daily_article_count": row.daily_article_count, "article_model_id": row.article_model_id, "image_model_id": row.image_model_id, "image_count": row.image_count, "auto_repair": row.auto_repair, "max_repair_attempts": row.max_repair_attempts, "last_generated_date": row.last_generated_date}


@app.post("/api/article-settings/run-now", status_code=202)
async def run_article_now(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    result = create_article(ArticleGenerateRequest(), user, db)
    return await result


def _safe_catalog_path(relative_path: str) -> Path:
    root = settings.engine_root.resolve()
    candidate = (root / relative_path).resolve()
    if root not in candidate.parents or not candidate.is_file():
        raise HTTPException(status_code=404, detail="知识库文件不存在")
    return candidate


@app.get("/api/knowledge/file")
def knowledge_file(path: str = Query(..., min_length=1), user: User = Depends(current_user)) -> dict:
    candidate = _safe_catalog_path(path)
    if not (candidate.suffix.lower() in {".yaml", ".yml", ".json", ".ttl", ".md"} and ("kb" in candidate.parts or "business" in candidate.parts or "simulation" in candidate.parts)):
        raise HTTPException(status_code=403, detail="不允许访问该文件")
    text = candidate.read_text(encoding="utf-8")
    return {"path": candidate.relative_to(settings.engine_root).as_posix(), "content": text, "size": len(text)}


@app.get("/api/report-settings")
def get_report_settings(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    row = db.get(ReportSetting, user.id) or ReportSetting(user_id=user.id)
    if db.get(ReportSetting, user.id) is None:
        db.add(row); db.commit()
    return {"enabled": row.enabled, "generate_time": row.generate_time, "approval_required": row.approval_required, "reminder_timeout_minutes": row.reminder_timeout_minutes, "email_sender": row.email_sender, "email_recipient": row.email_recipient, "last_trigger_key": row.last_trigger_key}


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
    return {"id": job.id, "kind": job.kind, "status": job.status, "progress": job.progress, "total_files": job.total_files, "processed_files": job.processed_files, "error": job.error, "created_at": job.created_at, "started_at": job.started_at, "completed_at": job.completed_at, "attempt_count": job.attempt_count, "download_url": f"/api/exports/{job.id}/download" if job.status == "completed" else None}


@app.get("/api/exports")
def list_exports(limit: int = Query(50, ge=1, le=200), user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    jobs = db.scalars(select(ExportJob).where(ExportJob.user_id == user.id).order_by(ExportJob.created_at.desc()).limit(limit)).all()
    return {"items": [{"id": j.id, "kind": j.kind, "status": j.status, "progress": j.progress, "total_files": j.total_files, "processed_files": j.processed_files, "error": j.error, "created_at": j.created_at, "started_at": j.started_at, "completed_at": j.completed_at, "attempt_count": j.attempt_count, "download_url": f"/api/exports/{j.id}/download" if j.status == "completed" else None} for j in jobs]}


@app.post("/api/exports/{job_id}/retry", status_code=202)
async def retry_export(job_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    job = db.get(ExportJob, job_id)
    if not job or job.user_id != user.id:
        raise HTTPException(status_code=404, detail="导出任务不存在")
    if job.status not in {"failed", "cancelled"}:
        raise HTTPException(status_code=409, detail="当前状态不可重试")
    job.status = "queued"; job.error = None; job.progress = 0; job.path = None; job.completed_at = None
    db.commit(); asyncio.create_task(export_job_task(job.id))
    return {"id": job.id, "status": job.status}


@app.post("/api/exports/{job_id}/cancel")
def cancel_export(job_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    job = db.get(ExportJob, job_id)
    if not job or job.user_id != user.id:
        raise HTTPException(status_code=404, detail="导出任务不存在")
    if job.status in {"completed", "failed", "cancelled"}:
        return {"id": job.id, "status": job.status}
    job.status = "cancelled"; job.completed_at = datetime.now(timezone.utc); db.commit()
    return {"id": job.id, "status": job.status}


@app.get("/api/exports/{job_id}/download")
def download_export(job_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    job = db.get(ExportJob, job_id)
    if not job or job.user_id != user.id or job.status != "completed" or not job.path:
        raise HTTPException(status_code=404, detail="导出文件不可用")
    path = Path(job.path)
    if not path.is_file() or path.parent.resolve() != (settings.data_dir / "artifacts").resolve():
        raise HTTPException(status_code=404, detail="导出文件不存在")
    return FileResponse(path, filename=path.name, media_type="application/zip")


# 静态文件服务：提供前端构建产物
frontend_dist = Path(__file__).parent.parent.parent / "frontend" / "dist"
if frontend_dist.is_dir():
    app.mount("/assets", StaticFiles(directory=str(frontend_dist / "assets")), name="assets")

    @app.get("/")
    @app.get("/{full_path:path}")
    def serve_frontend(full_path: str = ""):
        # API 路由已经处理，这里只处理前端页面
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
        index_file = frontend_dist / "index.html"
        if index_file.is_file():
            return FileResponse(index_file)
        raise HTTPException(status_code=404, detail="Frontend not built")
