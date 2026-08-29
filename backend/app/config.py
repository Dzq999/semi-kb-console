from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

@dataclass(frozen=True)
class Settings:
    app_name: str = "SEMI-KB Console"
    host: str = os.getenv("SEMI_KB_HOST", "127.0.0.1")
    port: int = int(os.getenv("SEMI_KB_PORT", "8765"))
    data_dir: Path = Path(os.getenv("SEMI_KB_CONSOLE_DATA", PROJECT_ROOT / "data"))
    semi_kb_root: Path = Path(os.getenv("SEMI_KB_ROOT", r"D:\AI_Coding\semi-kb"))
    llm_base_url: str = os.getenv("LLM_BASE_URL", "https://4sapi.com/v1").rstrip("/")
    model_catalog_url: str = os.getenv("MODEL_CATALOG_URL", "https://4sapi.org/v1/models")
    llm_api_key_env: str = os.getenv("LLM_API_KEY_ENV", "4SAPI_API_KEY")
    max_agent_count: int = min(10, max(1, int(os.getenv("MAX_AGENT_COUNT", "10"))))
    max_provider_concurrency: int = max(1, int(os.getenv("MAX_PROVIDER_CONCURRENCY", "5")))
    research_result_limit: int = min(12, max(1, int(os.getenv("RESEARCH_RESULT_LIMIT", "6"))))
    evidence_chars_per_page: int = min(20_000, max(1_000, int(os.getenv("EVIDENCE_CHARS_PER_PAGE", "6000"))))
    timezone: str = os.getenv("SEMI_KB_TIMEZONE", "Asia/Shanghai")
    frontend_origin: str = os.getenv("FRONTEND_ORIGIN", "http://127.0.0.1:5173")

    @property
    def database_url(self) -> str:
        return os.getenv("DATABASE_URL", f"sqlite:///{(self.data_dir / 'console.db').as_posix()}")

    @property
    def llm_api_key(self) -> str | None:
        return os.getenv(self.llm_api_key_env)


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
(settings.data_dir / "artifacts").mkdir(parents=True, exist_ok=True)
