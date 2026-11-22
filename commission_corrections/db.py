"""数据库连接与会话工厂。

所有连接参数只从环境变量读取（客户统一技术约定：配置与密钥不进代码）：

- ``DATABASE_URL``：SQLAlchemy URL。生产 PostgreSQL 16，
  例 ``postgresql+psycopg://user:pass@host:5432/member_space``；
  本地/测试可 ``sqlite+pysqlite:///:memory:``。
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    """全部业务表的声明式基类。"""


def database_url() -> str:
    return os.environ.get("DATABASE_URL", "sqlite+pysqlite:///./commission.db")


def build_engine(url: str | None = None) -> Engine:
    url = url or database_url()
    if url.startswith("sqlite"):
        # 单测用内存库时必须 StaticPool，否则每条连接一个空库
        from sqlalchemy.pool import StaticPool

        return create_engine(
            url,
            future=True,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    return create_engine(url, future=True, pool_pre_ping=True)


def get_sessionmaker(engine: Engine | None = None) -> sessionmaker:
    return sessionmaker(bind=engine or build_engine(), autoflush=False, expire_on_commit=False)
