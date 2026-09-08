from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


SourceMode = Literal["web", "model_prior", "hybrid"]


class SetupRequest(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=10, max_length=200)


class LoginRequest(SetupRequest):
    pass


class AgentConfig(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    role: str = Field(min_length=1, max_length=120)
    domain: str = Field(min_length=1, max_length=80)
    objective: str = Field(min_length=3, max_length=2000)
    source_mode: SourceMode
    model_override: str | None = Field(default=None, max_length=160)
    timeout_seconds: int = Field(default=300, ge=30, le=3600)
    max_retries: int = Field(default=2, ge=0, le=5)


class RunCreate(BaseModel):
    model_id: str = Field(min_length=1, max_length=160)
    agents: list[AgentConfig] = Field(min_length=1, max_length=10)
    publish_changes: bool = False
    continuous: bool = True
    round_interval_seconds: int = Field(default=5, ge=0, le=86_400)
    max_rounds: int | None = Field(default=None, ge=1, le=1000)  # None = 无限循环
    max_consecutive_round_failures: int = Field(default=3, ge=1, le=20)
    auto_repair: bool = True
    # 每次修复都会重跑整条闸门链（align_sources + capability_validate + apply --check
    # + simulate_check + 逐文件 simulate + 全量 apply），单轮成本以分钟计。默认 1 次：
    # 一次修复不成就走部分发布，把能发的发出去，而不是把整轮拖死在重试上。
    max_auto_repair_attempts: int = Field(default=1, ge=0, le=10)
    repair_follow_failure_threshold: bool = True


class RunResumeRequest(BaseModel):
    max_consecutive_round_failures: int | None = Field(default=None, ge=1, le=20)
    auto_repair: bool | None = None
    max_auto_repair_attempts: int | None = Field(default=None, ge=0, le=10)
    repair_follow_failure_threshold: bool | None = None


class DefaultModelUpdate(BaseModel):
    model_id: str = Field(min_length=1, max_length=160)


class CredentialUpdate(BaseModel):
    kind: Literal["llm_api_key", "wecom_webhook_key", "qq_smtp_auth_code", "wechat_app_id", "wechat_app_secret", "wecom_aibot_id", "wecom_aibot_secret"]
    value: str = Field(min_length=1, max_length=2000)
    masked_hint: str | None = Field(default=None, max_length=120)


class ReportSettingsUpdate(BaseModel):
    enabled: bool = True
    generate_time: str = "18:00"
    approval_required: bool = True
    reminder_timeout_minutes: int = Field(default=10, ge=1, le=1440)
    email_sender: str | None = None
    email_recipient: str | None = None
    email_reminder_enabled: bool = True

    @field_validator("generate_time")
    @classmethod
    def validate_time(cls, value: str) -> str:
        parts = value.split(":")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            raise ValueError("generate_time 必须为 HH:MM")
        hour, minute = map(int, parts)
        if hour > 23 or minute > 59:
            raise ValueError("generate_time 超出范围")
        return f"{hour:02d}:{minute:02d}"


class ReportContentUpdate(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)


class ExportCreate(BaseModel):
    kind: Literal["ontology", "knowledge", "business", "simulation", "scenarios", "complete"]
    filtered: bool = True  # 默认精简导出，只保留策展模块个体
    scope: Literal["all", "manufacturing", "erp"] = "all"  # 源系统一级维度子图


class ArticleGenerateRequest(BaseModel):
    topic_id: int | None = None
    model_id: str | None = None


class ArticleUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=240)
    content_markdown: str | None = Field(default=None, min_length=1, max_length=200_000)
    revision_note: str = Field(default="", max_length=500)


class ArticleSettingsUpdate(BaseModel):
    enabled: bool = True
    frequency: Literal["daily"] = "daily"
    generate_time: str = "18:00"
    timezone: str = "Asia/Shanghai"
    approval_required: bool = True
    auto_visuals: bool = True
    daily_article_count: int = Field(default=1, ge=1, le=10)
    article_model_id: str | None = Field(default=None, max_length=160)
    image_model_id: str | None = Field(default=None, max_length=160)
    image_count: int = Field(default=1, ge=0, le=5)
    auto_repair: bool = True
    max_repair_attempts: int = Field(default=3, ge=0, le=3)

    @field_validator("generate_time")
    @classmethod
    def validate_article_time(cls, value: str) -> str:
        parts = value.split(":")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            raise ValueError("generate_time 必须为 HH:MM")
        hour, minute = map(int, parts)
        if hour > 23 or minute > 59:
            raise ValueError("generate_time 超出范围")
        return f"{hour:02d}:{minute:02d}"


class LoopUpdate(BaseModel):
    enabled: bool
    interval_minutes: int = Field(default=1440, ge=5, le=43_200)
    run_config: RunCreate | None = None


class BusinessDraftRequest(BaseModel):
    intent: str = Field(min_length=4, max_length=2000)
    domain: str = Field(default="manufacturing", max_length=80)
    model_id: str = Field(min_length=1, max_length=160)


class BaselineScheduleUpdate(BaseModel):
    enabled: bool = False
    generate_time: str = "03:00"
    daily_count: int = Field(default=3, ge=1, le=10)
    llm_model_id: str | None = Field(default=None, max_length=160)
    domain_strategy: Literal["gap_hotspot"] = "gap_hotspot"

    @field_validator("generate_time")
    @classmethod
    def validate_baseline_time(cls, value: str) -> str:
        parts = value.split(":")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            raise ValueError("generate_time 必须为 HH:MM")
        hour, minute = map(int, parts)
        if hour > 23 or minute > 59:
            raise ValueError("generate_time 超出范围")
        return f"{hour:02d}:{minute:02d}"


class BatchApproveRequest(BaseModel):
    draft_ids: list[str] = Field(min_length=1, max_length=50)


class ImportAnalyzeRequest(BaseModel):
    model_id: str = Field(min_length=1, max_length=160)


class ImportFileMapping(BaseModel):
    stored_name: str = Field(min_length=1, max_length=200)
    target_class: str | None = Field(default=None, max_length=300)
    entity_keys: list[str] | None = Field(default=None, max_length=32)
    time_field: str | None = Field(default=None, max_length=200)


class ImportMappingUpdate(BaseModel):
    files: list[ImportFileMapping] = Field(min_length=1, max_length=200)


class QaConversationCreate(BaseModel):
    title: str = Field(default="新会话", max_length=240)


class QaAskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    model_id: str | None = Field(default=None, max_length=160)


_ALLOWED_LLM_SCHEMES = ("https://", "http://localhost", "http://127.0.0.1")


class LlmEndpointUpdate(BaseModel):
    """每用户 LLM 端点覆盖；留空/None 表示回退全局 settings 默认。仅允许 https，或本地 http（防 SSRF）。"""

    llm_base_url: str | None = Field(default=None, max_length=300)
    model_catalog_url: str | None = Field(default=None, max_length=300)
    llm_api_style: Literal["anthropic", "openai"] | None = None

    @field_validator("llm_base_url", "model_catalog_url")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        low = value.casefold()
        if not low.startswith(_ALLOWED_LLM_SCHEMES):
            raise ValueError("端点仅允许 https:// 或本地 http://localhost / http://127.0.0.1")
        return value.rstrip("/")
