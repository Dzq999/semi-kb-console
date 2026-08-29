from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config


def upgrade_database() -> None:
    backend_root = Path(__file__).resolve().parents[1]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    command.upgrade(config, "head")
