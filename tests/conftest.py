"""测试夹具：每个用例一个独立的 in-memory SQLite 库。"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

import pytest
from fastapi.testclient import TestClient

from commission_corrections.api import create_app
from commission_corrections.db import Base, build_engine


@pytest.fixture()
def app():
    engine = build_engine("sqlite+pysqlite:///:memory:")
    application = create_app(engine=engine)
    application.state.test_engine = engine
    yield application
    Base.metadata.drop_all(engine)


@pytest.fixture()
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture()
def db(app):
    """直连会话：用于构造「上游系统已写入」的系统结果记录。"""

    session = app.state.sessionmaker()
    try:
        yield session
        session.commit()
    finally:
        session.close()
