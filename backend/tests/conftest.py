from __future__ import annotations

import os
import tempfile
from pathlib import Path

TEST_DIR = Path(tempfile.mkdtemp(prefix="semi-kb-console-tests-"))
os.environ["SEMI_KB_CONSOLE_DATA"] = str(TEST_DIR)
os.environ["DATABASE_URL"] = f"sqlite:///{(TEST_DIR / 'test.db').as_posix()}"
os.environ["SEMI_KB_ROOT"] = r"D:\AI_Coding\semi-kb"

import pytest
from fastapi.testclient import TestClient

from app.db import Base, engine
from app.main import app, sessions


@pytest.fixture(autouse=True)
def clean_db():
    sessions.clear()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def client():
    with TestClient(app) as value:
        yield value


@pytest.fixture
def authenticated(client: TestClient):
    response = client.post("/api/auth/setup", json={"username": "admin", "password": "strong-password"})
    assert response.status_code == 201
    return client

