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
    # 每用户可覆盖的 LLM 端点（非机密）；留空回退全局 settings。由系统设置页维护。
    llm_base_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    model_catalog_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    llm_api_style: Mapped[str | None] = mapped_column(String(16), nullable=True)


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
    # 本轮成功收尾时产出的、面向下一轮的结构化优化方向（确定性启发式，非 LLM 反思）。
    # 下一轮 gap_analysis 的 augment_gap 会读回并注入 gap，使“轮成功→方向→下一轮”成为显式闭环。
    next_direction_json: Mapped[str] = mapped_column(Text, default="{}")
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
    last_trigger_key: Mapped[str | None] = mapped_column(String(32), nullable=True)


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


class BusinessDraft(Base):
    """独立经营协作 Agent 起草的一份经营基线草案（template+dataset+model 三件套）。

    引擎真源是磁盘上的 business/drafts/<id>/ 目录；本行仅供 UI 列表/状态/用户隔离，
    人点『采纳为基线』后才由 promote_business_draft 落到线上 business/{templates,datasets,models}/。
    """

    __tablename__ = "business_drafts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(24), default="drafted", index=True)
    intent: Mapped[str] = mapped_column(Text, default="")
    domain: Mapped[str] = mapped_column(String(80), default="manufacturing")
    llm_model_id: Mapped[str] = mapped_column(String(160), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    validation_json: Mapped[str] = mapped_column(Text, default="{}")
    promoted_paths_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BusinessBaselineSchedule(Base):
    """每日定时·自动起草经营基线的调度设置（仿 ArticleSetting 的定时+数量样式）。

    治理边界：定时只做『起草+引擎门禁校验』，产物是 BusinessDraft 行（validated/invalid），
    落盘仍由人在待采纳列表里批量采纳（promote_business_draft）——数值假设由人判断，不自动晋升。
    """

    __tablename__ = "business_baseline_schedules"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    generate_time: Mapped[str] = mapped_column(String(5), default="03:00")
    timezone: Mapped[str] = mapped_column(String(80), default="Asia/Shanghai")
    daily_count: Mapped[int] = mapped_column(Integer, default=3)
    llm_model_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    domain_strategy: Mapped[str] = mapped_column(String(40), default="gap_hotspot")
    last_generated_date: Mapped[str | None] = mapped_column(String(10), nullable=True)


class ImportJob(Base):
    """一次素材导入任务（可含多份结构化文件）：上传→模型建议映射→人工改→引擎门禁校验→人工采纳。

    与经营基线草案同一信任模型（[[semi-kb ...]] 的 draft→gate→adopt）：原件 sha256 锁定存
    imports/drafts/<id>/，模型只产出 manifest 草案（target_class/entity_keys 建议），一切过
    imported_ingest.py 真门禁，人点『采纳』才合入线上 sources/internal/imported/。绝不自动
    写入推理层/current.ttl；restricted 素材可暂存+记元数据，但禁止采纳入库。

    引擎真源是磁盘上的 imports/drafts/<id>/ 目录；本行仅供 UI 列表/状态/用户隔离。
    """

    __tablename__ = "import_jobs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    # uploaded → analyzed → validated / invalid → adopted / rejected
    status: Mapped[str] = mapped_column(String(24), default="uploaded", index=True)
    channel: Mapped[str] = mapped_column(String(32), default="structured_table")
    classification: Mapped[str] = mapped_column(String(32), default="internal_confidential")
    llm_model_id: Mapped[str] = mapped_column(String(160), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    # 待入库文件清单（含 sha256 与人工可改的 target_class/entity_keys 映射建议）。
    manifest_json: Mapped[str] = mapped_column(Text, default="{}")
    validation_json: Mapped[str] = mapped_column(Text, default="{}")
    adopted_paths_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    adopted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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
    generation_stage: Mapped[str] = mapped_column(String(80), default="queued")
    generation_progress: Mapped[int] = mapped_column(Integer, default=0)
    generation_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    repair_attempts: Mapped[int] = mapped_column(Integer, default=0)
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
    wechat_status: Mapped[str] = mapped_column(String(24), default="not_sent")
    wechat_draft_media_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    wechat_last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    wechat_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    wechat_response_json: Mapped[str] = mapped_column(Text, default="{}")
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
    auto_repair: Mapped[bool] = mapped_column(Boolean, default=True)
    max_repair_attempts: Mapped[int] = mapped_column(Integer, default=3)
    last_generated_date: Mapped[str | None] = mapped_column(String(10), nullable=True)


class QaConversation(Base):
    """用户与知识库问答助手的一次多轮会话。

    答案由 qa 服务综合经营模型/仿真场景/项目知识库(含 vFab 可引用来源)接地生成，
    每条消息的引用来源存于 QaMessage.citations_json，供 UI 展示可追溯出处。
    """

    __tablename__ = "qa_conversations"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(240), default="新会话")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    messages: Mapped[list["QaMessage"]] = relationship(back_populates="conversation", cascade="all, delete-orphan")


class QaMessage(Base):
    __tablename__ = "qa_messages"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("qa_conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user / assistant
    content: Mapped[str] = mapped_column(Text, default="")
    citations_json: Mapped[str] = mapped_column(Text, default="[]")
    model_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    grounded: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    conversation: Mapped[QaConversation] = relationship(back_populates="messages")
