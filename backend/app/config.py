from __future__ import annotations

import os
import asyncio
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv


if os.name == "nt":
    # psycopg async connections require a selector loop on Windows.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

@dataclass(frozen=True)
class Settings:
    app_name: str = "SEMI-KB Console"
    host: str = os.getenv("SEMI_KB_HOST", "127.0.0.1")
    port: int = int(os.getenv("SEMI_KB_PORT", "8765"))
    data_dir: Path = Path(os.getenv("SEMI_KB_CONSOLE_DATA", PROJECT_ROOT / "data"))
    # The console owns this engine/data root.  A legacy SEMI_KB_ROOT value is
    # intentionally ignored at runtime; use scripts\migrate-engine.ps1 for a
    # one-time import from an older checkout.
    engine_root: Path = Path(os.getenv("SEMI_KB_ENGINE_ROOT", PROJECT_ROOT / "data" / "engine"))
    llm_base_url: str = os.getenv("LLM_BASE_URL", "https://4sapi.org/v1").rstrip("/")
    model_catalog_url: str = os.getenv("MODEL_CATALOG_URL", "https://4sapi.org/v1/models")
    llm_api_key_env: str = os.getenv("LLM_API_KEY_ENV", "4SAPI_API_KEY")
    # 文本补全走哪套契约：anthropic=原生 /v1/messages（Claude 模型走此路更稳，system 顶层、max_tokens 必填）；
    # openai=兼容 /v1/chat/completions（旧行为）。中转站两套都暴露 Claude 模型，切 openai 即可即时回退。
    llm_api_style: str = os.getenv("LLM_API_STYLE", "anthropic").casefold()
    # 原生 /v1/messages 的 max_tokens 必填；旧兼容路径不传时由中转取模型上限。给足以免长产物被截断。
    llm_max_tokens: int = min(64_000, max(256, int(os.getenv("LLM_MAX_TOKENS", "16384"))))
    wechat_api_base_url: str = os.getenv("WECHAT_API_BASE_URL", "https://api.weixin.qq.com").rstrip("/")
    wechat_account_name: str = os.getenv("WECHAT_ACCOUNT_NAME", "墨言yyy")
    max_agent_count: int = min(10, max(1, int(os.getenv("MAX_AGENT_COUNT", "10"))))
    max_provider_concurrency: int = max(1, int(os.getenv("MAX_PROVIDER_CONCURRENCY", "5")))
    research_result_limit: int = min(12, max(1, int(os.getenv("RESEARCH_RESULT_LIMIT", "6"))))
    evidence_chars_per_page: int = min(20_000, max(1_000, int(os.getenv("EVIDENCE_CHARS_PER_PAGE", "6000"))))
    timezone: str = os.getenv("SEMI_KB_TIMEZONE", "Asia/Shanghai")
    frontend_origin: str = os.getenv("FRONTEND_ORIGIN", "http://127.0.0.1:5173")
    orchestrator_engine: str = os.getenv("ORCHESTRATOR_ENGINE", "langgraph").casefold()
    auto_resume_runs: bool = os.getenv("AUTO_RESUME_RUNS", "true").casefold() in {"1", "true", "yes", "on"}
    graph_repair_attempts: int = min(10, max(0, int(os.getenv("GRAPH_REPAIR_ATTEMPTS", "1"))))
    article_repair_attempts: int = min(3, max(0, int(os.getenv("ARTICLE_REPAIR_ATTEMPTS", "3"))))
    worker_heartbeat_seconds: int = min(300, max(5, int(os.getenv("WORKER_HEARTBEAT_SECONDS", "15"))))

    def _postgres_url(self, name_override: str | None = None) -> str | None:
        """Build the psycopg URL from discrete env vars, optionally swapping the
        database name.  Returns None when no postgres credentials are configured.
        A full DATABASE_URL wins only when no name override is requested."""
        explicit = os.getenv("DATABASE_URL")
        if explicit and name_override is None:
            return explicit
        password = os.getenv("SEMI_KB_DB_PASSWORD")
        if not password:
            return None
        user = quote_plus(os.getenv("SEMI_KB_DB_USER", "semi_kb_console"))
        password = quote_plus(password)
        host = os.getenv("SEMI_KB_DB_HOST", "127.0.0.1")
        port = int(os.getenv("SEMI_KB_DB_PORT", "5432"))
        name = quote_plus(name_override or os.getenv("SEMI_KB_DB_NAME", "semi_kb_console"))
        return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{name}"

    @property
    def database_url(self) -> str:
        return self._postgres_url() or f"sqlite:///{(self.data_dir / 'console.db').as_posix()}"

    @property
    def checkpoint_database_url(self) -> str | None:
        # 1) explicit full URL wins; 2) else a dedicated checkpoint DB *name*
        # reuses the main credentials (no secret duplication); 3) else fall back
        # to the main postgres DB (legacy shared behaviour).
        explicit = os.getenv("LANGGRAPH_DATABASE_URL")
        if explicit:
            value: str = explicit
        else:
            ckpt_name = os.getenv("LANGGRAPH_DB_NAME")
            if ckpt_name:
                value = self._postgres_url(ckpt_name) or ""
            else:
                value = self.database_url if self.database_url.startswith("postgresql") else ""
        return value.replace("postgresql+psycopg://", "postgresql://", 1) if value else None

    @property
    def llm_api_key(self) -> str | None:
        return os.getenv(self.llm_api_key_env)

    @property
    def semi_kb_root(self) -> Path:
        """Compatibility alias for older integrations; always local."""
        return self.engine_root


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
(settings.data_dir / "artifacts").mkdir(parents=True, exist_ok=True)
