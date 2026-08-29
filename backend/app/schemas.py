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
    max_consecutive_round_failures: int = Field(default=3, ge=1, le=20)


class DefaultModelUpdate(BaseModel):
    model_id: str = Field(min_length=1, max_length=160)


class CredentialUpdate(BaseModel):
    kind: Literal["llm_api_key", "wecom_webhook_key", "qq_smtp_auth_code"]
    value: str = Field(min_length=1, max_length=2000)
    masked_hint: str | None = Field(default=None, max_length=120)


class ReportSettingsUpdate(BaseModel):
    enabled: bool = True
    generate_time: str = "18:00"
    approval_required: bool = True
    reminder_timeout_minutes: int = Field(default=10, ge=1, le=1440)
    email_sender: str | None = None
    email_recipient: str | None = None

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


class LoopUpdate(BaseModel):
    enabled: bool
    interval_minutes: int = Field(default=1440, ge=5, le=43_200)
    run_config: RunCreate | None = None
