from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserPreference(Base):
    __tablename__ = "user_preferences"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    default_model_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    default_agent_count: Mapped[int] = mapped_column(Integer, default=6)
    timezone: Mapped[str] = mapped_column(String(80), default="Asia/Shanghai")


class EncryptedCredential(Base):
    __tablename__ = "encrypted_credentials"
    __table_args__ = (UniqueConstraint("user_id", "kind"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    ciphertext: Mapped[str] = mapped_column(Text)
    masked_hint: Mapped[str] = mapped_column(String(120), default="已配置")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    model_id: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    current_stage: Mapped[str] = mapped_column(String(64), default="pending")
    progress: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    metrics_before_json: Mapped[str] = mapped_column(Text, default="{}")
    metrics_after_json: Mapped[str] = mapped_column(Text, default="{}")
    orchestrator_engine: Mapped[str] = mapped_column(String(24), default="langgraph")
    pause_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    stop_after_round: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checkpoint_thread_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recovery_count: Mapped[int] = mapped_column(Integer, default=0)
    agents: Mapped[list["AgentRun"]] = relationship(back_populates="run", cascade="all, delete-orphan")
    rounds: Mapped[list["RunRound"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(120))
    domain: Mapped[str] = mapped_column(String(80))
    objective: Mapped[str] = mapped_column(Text)
    source_mode: Mapped[str] = mapped_column(String(24))
    model_id: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    output_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    run: Mapped[Run] = relationship(back_populates="agents")


class RunRound(Base):
    """One durable iteration of a continuous run."""

    __tablename__ = "run_rounds"
    __table_args__ = (UniqueConstraint("run_id", "round_number"),)
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    round_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    current_stage: Mapped[str] = mapped_column(String(64), default="pending")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    metrics_before_json: Mapped[str] = mapped_column(Text, default="{}")
    metrics_after_json: Mapped[str] = mapped_column(Text, default="{}")
    validation_json: Mapped[str] = mapped_column(Text, default="{}")
    artifacts_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    checkpoint_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    resumed_count: Mapped[int] = mapped_column(Integer, default=0)
    node_attempts_json: Mapped[str] = mapped_column(Text, default="{}")
    quarantined_files_json: Mapped[str] = mapped_column(Text, default="[]")
    run: Mapped[Run] = relationship(back_populates="rounds")


class AgentIteration(Base):
    """Preserves every Agent result instead of overwriting the last round."""

    __tablename__ = "agent_iterations"
    __table_args__ = (UniqueConstraint("agent_id", "round_number"),)
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    round_number: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    output_json: Mapped[str] = mapped_column(Text, default="{}")
    candidate_files_json: Mapped[str] = mapped_column(Text, default="[]")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    checkpoint_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


class RunEvent(Base):
    __tablename__ = "run_events"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(40))
    level: Mapped[str] = mapped_column(String(16), default="info")
    message: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class LoopSetting(Base):
    __tablename__ = "loop_settings"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=1440)
    run_config_json: Mapped[str] = mapped_column(Text, default="{}")
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReportSetting(Base):
    __tablename__ = "report_settings"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    generate_time: Mapped[str] = mapped_column(String(5), default="18:00")
    approval_required: Mapped[bool] = mapped_column(Boolean, default=True)
    reminder_timeout_minutes: Mapped[int] = mapped_column(Integer, default=10)
    email_sender: Mapped[str | None] = mapped_column(String(200), nullable=True)
    email_recipient: Mapped[str | None] = mapped_column(String(200), nullable=True)


class DailyReport(Base):
    __tablename__ = "daily_reports"
    __table_args__ = (UniqueConstraint("user_id", "report_date"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    report_date: Mapped[str] = mapped_column(String(10), index=True)
    status: Mapped[str] = mapped_column(String(32), default="scheduled")
    approval_required: Mapped[bool] = mapped_column(Boolean, default=True)
    metrics_snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    model_draft: Mapped[str] = mapped_column(Text, default="")
    content: Mapped[str] = mapped_column(Text, default="")
    validation_json: Mapped[str] = mapped_column(Text, default="{}")
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    send_response_json: Mapped[str] = mapped_column(Text, default="{}")


class DailyReportRevision(Base):
    __tablename__ = "daily_report_revisions"
    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("daily_reports.id"), index=True)
    content: Mapped[str] = mapped_column(Text)
    editor_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NotificationRecord(Base):
    __tablename__ = "notification_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    report_id: Mapped[int | None] = mapped_column(ForeignKey("daily_reports.id"), nullable=True)
    channel: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(24))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ExportJob(Base):
    __tablename__ = "export_jobs"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(24), default="queued")
    progress: Mapped[float] = mapped_column(Float, default=0)
    total_files: Mapped[int] = mapped_column(Integer, default=0)
    processed_files: Mapped[int] = mapped_column(Integer, default=0)
    worker_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    path: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ArticleTopic(Base):
    __tablename__ = "article_topics"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(240))
    domain: Mapped[str] = mapped_column(String(80), default="semiconductor")
    customer_role: Mapped[str] = mapped_column(String(160), default="")
    pain_point: Mapped[str] = mapped_column(Text, default="")
    business_context: Mapped[str] = mapped_column(Text, default="")
    evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    source_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority_score: Mapped[float] = mapped_column(Float, default=0)
    novelty_score: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String(24), default="qualified", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Article(Base):
    __tablename__ = "articles"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    topic_id: Mapped[int | None] = mapped_column(ForeignKey("article_topics.id"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(240))
    subtitle: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    approval_required: Mapped[bool] = mapped_column(Boolean, default=True)
    content_markdown: Mapped[str] = mapped_column(Text, default="")
    content_html: Mapped[str] = mapped_column(Text, default="")
    validation_json: Mapped[str] = mapped_column(Text, default="{}")
    metrics_snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    ai_tone_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    factual_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    generation_date: Mapped[str | None] = mapped_column(String(10), nullable=True, index=True)
    sequence_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    article_model_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    image_model_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    cover_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ArticleAsset(Base):
    __tablename__ = "article_assets"
    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), index=True)
    asset_type: Mapped[str] = mapped_column(String(32))
    file_path: Mapped[str] = mapped_column(Text)
    mime_type: Mapped[str] = mapped_column(String(120), default="image/png")
    caption: Mapped[str] = mapped_column(String(500), default="")
    source_type: Mapped[str] = mapped_column(String(32), default="generated")
    source_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ArticleRevision(Base):
    __tablename__ = "article_revisions"
    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), index=True)
    content_markdown: Mapped[str] = mapped_column(Text)
    editor_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    revision_note: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ArticleSetting(Base):
    __tablename__ = "article_settings"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    frequency: Mapped[str] = mapped_column(String(20), default="daily")
    generate_time: Mapped[str] = mapped_column(String(5), default="18:00")
    timezone: Mapped[str] = mapped_column(String(80), default="Asia/Shanghai")
    approval_required: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_visuals: Mapped[bool] = mapped_column(Boolean, default=True)
    daily_article_count: Mapped[int] = mapped_column(Integer, default=1)
    article_model_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    image_model_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    image_count: Mapped[int] = mapped_column(Integer, default=1)
    last_generated_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
