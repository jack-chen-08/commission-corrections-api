"""Alembic 迁移环境。

连接串**只从环境变量 ``DATABASE_URL`` 读取**（客户统一技术约定：配置与密钥不进代码、
不进版本库）。``alembic.ini`` 里的 ``sqlalchemy.url`` 保持为空，由本文件注入。

用法::

    export DATABASE_URL="postgresql+psycopg://user:pass@host:5432/member_space"
    alembic upgrade head          # 建表
    alembic revision --autogenerate -m "msg"   # 改模型后生成新迁移
    alembic check                 # 模型与库结构是否漂移
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from commission_corrections import models  # noqa: F401  导入即注册全部表
from commission_corrections.db import Base, build_engine, database_url  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    """环境变量优先，其次是 db.database_url() 的本地默认值。"""
    return os.environ.get("DATABASE_URL") or database_url()


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = build_engine(_url())
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            # SQLite 不支持大部分 ALTER，批处理模式让后续迁移也能在本地跑
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
