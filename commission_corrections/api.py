"""FastAPI 路由层（S6-COMM-016 · 提成人工调整与重算）。

路由前缀沿用客户后台约定：``/api/admin/commission/corrections``。
身份与数据范围由请求头注入（见 ``iam.py``，真实 S2-IAM 落地后替换即可）。
"""

from __future__ import annotations

import os
from typing import Iterator

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from . import service
from .db import build_engine, get_sessionmaker
from .iam import Actor, actor_from_headers
from .schemas import (
    AdjustmentCreate,
    AdjustmentResult,
    AuditOut,
    RecalculateRequest,
    RecordOut,
    StatementOut,
)

PREFIX = "/api/admin/commission/corrections"


def get_db(request: Request) -> Iterator[Session]:
    session: Session = request.app.state.sessionmaker()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _auto_create_default() -> bool:
    """本地/测试便利：默认按模型建表；生产用 Alembic 迁移时设 ``AUTO_CREATE_SCHEMA=0``。"""
    return os.environ.get("AUTO_CREATE_SCHEMA", "1") not in ("0", "false", "False")


def create_app(engine=None, create_all: bool | None = None) -> FastAPI:
    app = FastAPI(
        title="S6-COMM-016 提成人工调整与重算",
        version="1.0.0",
        description="双人复核的人工调整、可追溯的重算引擎、月结汇总与审计留痕。",
    )
    eng = engine or build_engine()
    app.state.sessionmaker = get_sessionmaker(eng)
    if create_all is None:
        create_all = _auto_create_default()
    if create_all:
        from .db import Base

        Base.metadata.create_all(eng)

    @app.exception_handler(service.DomainError)
    async def _domain_error(_: Request, exc: service.DomainError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail, "code": exc.code})

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.post(f"{PREFIX}/adjustments", response_model=RecordOut, status_code=201)
    def post_adjustment(
        payload: AdjustmentCreate,
        actor: Actor = Depends(actor_from_headers),
        db: Session = Depends(get_db),
    ) -> RecordOut:
        record = service.create_adjustment(db, actor, payload)
        return RecordOut.model_validate(record)

    @app.post(f"{PREFIX}/adjustments/{{adjustment_id}}/approve", response_model=AdjustmentResult)
    def post_approve(
        adjustment_id: str,
        actor: Actor = Depends(actor_from_headers),
        db: Session = Depends(get_db),
    ) -> AdjustmentResult:
        record = service.approve_adjustment(db, actor, adjustment_id)
        db.flush()
        logs = [AuditOut.model_validate(log) for log in _audits_for(db, record.id)]
        return AdjustmentResult(record=RecordOut.model_validate(record), audit=logs)

    @app.post(f"{PREFIX}/adjustments/{{adjustment_id}}/reject", response_model=AdjustmentResult)
    def post_reject(
        adjustment_id: str,
        reason: str | None = Query(default=None, max_length=300),
        actor: Actor = Depends(actor_from_headers),
        db: Session = Depends(get_db),
    ) -> AdjustmentResult:
        record = service.reject_adjustment(db, actor, adjustment_id, reason)
        db.flush()
        logs = [AuditOut.model_validate(log) for log in _audits_for(db, record.id)]
        return AdjustmentResult(record=RecordOut.model_validate(record), audit=logs)

    @app.post(f"{PREFIX}/recalculate", response_model=AdjustmentResult)
    def post_recalculate(
        payload: RecalculateRequest,
        actor: Actor = Depends(actor_from_headers),
        db: Session = Depends(get_db),
    ) -> AdjustmentResult:
        records = service.recalculate(db, actor, payload.payment_id)
        db.flush()
        affected = records[-1]
        logs = [AuditOut.model_validate(log) for log in _audits_for(db, affected.id)]
        return AdjustmentResult(record=RecordOut.model_validate(affected), audit=logs)

    @app.get(f"{PREFIX}/monthly-statement", response_model=StatementOut)
    def get_statement(
        month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
        actor: Actor = Depends(actor_from_headers),
        db: Session = Depends(get_db),
    ) -> StatementOut:
        return service.monthly_statement(db, actor.org_id, month)

    @app.post(f"{PREFIX}/month-locks", response_model=dict, status_code=201)
    def post_lock_month(
        month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
        actor: Actor = Depends(actor_from_headers),
        db: Session = Depends(get_db),
    ) -> dict:
        if not actor.can_approve:
            raise service.DomainError(403, "forbidden_role", "locking a month requires sales_supervisor or above")
        lock = service.lock_month(db, actor, month)
        db.flush()
        return {"org_id": lock.org_id, "month": lock.month, "locked_by": lock.locked_by}

    return app


def _audits_for(db: Session, entity_id: str) -> list:
    from sqlalchemy import select

    from .models import AuditLog

    return list(
        db.scalars(
            select(AuditLog).where(AuditLog.entity_id == entity_id).order_by(AuditLog.created_at)
        )
    )


app = create_app()
