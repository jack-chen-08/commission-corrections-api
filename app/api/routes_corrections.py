"""提成人工调整与重算接口，前缀 /api/admin/commission/corrections。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_principal_dep
from app.iam import Principal
from app.schemas import (
    AdjustmentCreate,
    ApproveRequest,
    MonthLockRequest,
    RecalculateRequest,
    RejectRequest,
)
from app.service import corrections as svc

router = APIRouter(prefix="/api/admin/commission/corrections", tags=["commission-corrections"])


@router.post("/adjustments", status_code=status.HTTP_201_CREATED)
def create_adjustment(
    payload: AdjustmentCreate,
    session: Session = Depends(get_db),
    principal: Principal = Depends(get_principal_dep),
) -> dict:
    return svc.create_adjustment(session, principal, payload)


@router.post("/adjustments/{record_id}/approve")
def approve_adjustment(
    record_id: uuid.UUID,
    payload: ApproveRequest | None = None,
    session: Session = Depends(get_db),
    principal: Principal = Depends(get_principal_dep),
) -> dict:
    comment = payload.comment if payload else None
    return svc.approve_adjustment(session, principal, record_id, comment)


@router.post("/adjustments/{record_id}/reject")
def reject_adjustment(
    record_id: uuid.UUID,
    payload: RejectRequest,
    session: Session = Depends(get_db),
    principal: Principal = Depends(get_principal_dep),
) -> dict:
    return svc.reject_adjustment(session, principal, record_id, payload.reason)


@router.get("/adjustments")
def list_adjustments(
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_db),
    principal: Principal = Depends(get_principal_dep),
) -> dict:
    items = svc.list_adjustments(session, principal, status_filter, limit)
    return {"items": items, "count": len(items)}


@router.get("/adjustments/{record_id}")
def get_adjustment(
    record_id: uuid.UUID,
    session: Session = Depends(get_db),
    principal: Principal = Depends(get_principal_dep),
) -> dict:
    record = svc._load_record(session, principal.org_id, record_id, kind="adjustment")
    return svc.record_to_dict(session, record)


@router.post("/recalculate", status_code=status.HTTP_201_CREATED)
def recalculate(
    payload: RecalculateRequest,
    session: Session = Depends(get_db),
    principal: Principal = Depends(get_principal_dep),
) -> dict:
    return svc.recalculate(session, principal, payload)


@router.get("/records/{record_id}")
def get_record(
    record_id: uuid.UUID,
    session: Session = Depends(get_db),
    principal: Principal = Depends(get_principal_dep),
) -> dict:
    record = svc._load_record(session, principal.org_id, record_id)
    return svc.record_to_dict(session, record)


@router.get("/monthly-summary")
def monthly_summary(
    year: int = Query(ge=2000, le=2100),
    month: int = Query(ge=1, le=12),
    session: Session = Depends(get_db),
    principal: Principal = Depends(get_principal_dep),
) -> dict:
    return svc.monthly_summary(session, principal, year, month)


@router.get("/audit-logs")
def audit_logs(
    entity_id: uuid.UUID | None = None,
    action: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_db),
    principal: Principal = Depends(get_principal_dep),
) -> dict:
    items = svc.list_audit_logs(session, principal, entity_id, action, limit)
    return {"items": items, "count": len(items)}


@router.post("/month-locks", status_code=status.HTTP_201_CREATED)
def set_month_lock(
    payload: MonthLockRequest,
    session: Session = Depends(get_db),
    principal: Principal = Depends(get_principal_dep),
) -> dict:
    period = svc.parse_period(payload.period_month)
    return svc.set_month_lock(session, principal, period, payload.locked)


@router.get("/health")
def health(response: Response) -> dict:
    response.status_code = status.HTTP_200_OK
    return {"status": "ok", "module": "commission-corrections"}
