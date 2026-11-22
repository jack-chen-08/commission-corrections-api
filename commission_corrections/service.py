"""提成更正业务层：人工调整（双人复核）、重算引擎、月结汇总、审计留痕。

不依赖 HTTP，可被 FastAPI、定时任务、CLI 复用；全部金额为「分」整数。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .engine import compute_gross
from .iam import Actor, max_adjust_cents
from .models import AuditLog, CommissionRecord, MonthLock, utcnow
from .schemas import AdjustmentCreate, ReasonCategory, StatementLine, StatementOut

ADJUSTMENT = "adjustment"
SYSTEM = "system"

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
VOIDED = "voided"


class DomainError(Exception):
    """业务错误：由 API 层翻译为 HTTP 响应。"""

    def __init__(self, status_code: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def current_month(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


def _next_month(month: str) -> str:
    year, mon = (int(x) for x in month.split("-"))
    return f"{year + 1:04d}-01" if mon == 12 else f"{year:04d}-{mon + 1:02d}"


def is_month_locked(session: Session, org_id: str, month: str) -> bool:
    return (
        session.scalar(
            select(func.count()).select_from(MonthLock).where(
                MonthLock.org_id == org_id, MonthLock.month == month
            )
        )
        or 0
    ) > 0


def first_open_month(session: Session, org_id: str, start: str | None = None) -> str:
    """返回 start（含）起第一个未锁账的月份，用于承接已锁定月份的重算差额。"""

    month = start or current_month()
    for _ in range(24):
        if not is_month_locked(session, org_id, month):
            return month
        month = _next_month(month)
    raise DomainError(409, "invalid_state", "no open month available for posting correction")


def _audit(
    session: Session,
    *,
    org_id: str,
    entity_type: str,
    entity_id: str,
    action: str,
    before: int | None,
    after: int | None,
    actor_id: str,
    approver_id: str | None = None,
    detail: dict | None = None,
) -> AuditLog:
    log = AuditLog(
        org_id=org_id,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        before_amount_cents=before,
        after_amount_cents=after,
        actor_id=actor_id,
        approver_id=approver_id,
        detail=detail or {},
    )
    session.add(log)
    return log


def _get_scoped(session: Session, model, record_id: str, org_id: str):
    obj = session.get(model, record_id)
    if obj is None or obj.org_id != org_id:
        raise DomainError(404, "not_found", f"{model.__name__} {record_id} not found")
    return obj


# --------------------------------------------------------------------------- #
# 1. 人工调整：提交（pending，不计入月结）
# --------------------------------------------------------------------------- #
def create_adjustment(session: Session, actor: Actor, payload: AdjustmentCreate) -> CommissionRecord:
    limit = max_adjust_cents()
    if abs(payload.amount_cents) > limit:
        raise DomainError(
            422, "limit_exceeded", f"|amount_cents| exceeds COMM_ADJUST_MAX_CENTS={limit}"
        )

    month = payload.month or current_month()
    if is_month_locked(session, actor.org_id, month):
        # 已锁账月份的更正不能直接记在该月，必须落到当前未锁月（差额制）
        raise DomainError(
            409,
            "invalid_state",
            f"month {month} is locked; post the correction into {first_open_month(session, actor.org_id)}",
        )

    record = CommissionRecord(
        org_id=actor.org_id,
        site_id=actor.site_id,
        contract_id=payload.contract_id,
        payment_id=payload.payment_id,
        salesperson_id=payload.salesperson_id,
        kind=ADJUSTMENT,
        amount_cents=payload.amount_cents,
        month=month,
        status=PENDING,
        locked=False,
        reason_category=payload.reason_category.value,
        note=payload.note,
        created_by=actor.user_id,
        trace={
            "submitted_by": actor.user_id,
            "submitted_role": actor.role,
            "submitted_at": utcnow().isoformat(),
            "approval_chain": [],
        },
    )
    session.add(record)
    session.flush()

    _audit(
        session,
        org_id=actor.org_id,
        entity_type=ADJUSTMENT,
        entity_id=record.id,
        action="create",
        before=0,  # 未生效，对月结合计的贡献为 0
        after=record.amount_cents,
        actor_id=actor.user_id,
        detail={"reason_category": payload.reason_category.value, "note": payload.note},
    )
    return record


# --------------------------------------------------------------------------- #
# 2. 审批（AC2：不得自审）
# --------------------------------------------------------------------------- #
def approve_adjustment(session: Session, actor: Actor, adjustment_id: str) -> CommissionRecord:
    record = _get_scoped(session, CommissionRecord, adjustment_id, actor.org_id)
    if record.kind != ADJUSTMENT:
        raise DomainError(409, "invalid_state", "only adjustment records can be approved")
    if record.status != PENDING:
        raise DomainError(409, "invalid_state", f"record status is {record.status}")

    # AC2：申请人不得审批自己提交的调整 —— 优先于角色判断，避免「有权限就能自审」
    if record.created_by == actor.user_id:
        raise DomainError(403, "forbidden_self_approval", "applicant cannot approve own adjustment")
    if not actor.can_approve:
        raise DomainError(403, "forbidden_role", "approval requires sales_supervisor or above")

    record.status = APPROVED
    record.approved_by = actor.user_id
    record.approved_at = utcnow()
    record.updated_at = utcnow()
    trace = dict(record.trace or {})
    trace.setdefault("approval_chain", []).append(
        {"approver": actor.user_id, "role": actor.role, "at": utcnow().isoformat()}
    )
    record.trace = trace

    _audit(
        session,
        org_id=actor.org_id,
        entity_type=ADJUSTMENT,
        entity_id=record.id,
        action="approve",
        before=0,
        after=record.amount_cents,  # AC4：调整前后金额
        actor_id=record.created_by,
        approver_id=actor.user_id,  # AC4：审批人
        detail={"reason_category": record.reason_category, "note": record.note},
    )
    return record


def reject_adjustment(session: Session, actor: Actor, adjustment_id: str, reason: str | None = None) -> CommissionRecord:
    record = _get_scoped(session, CommissionRecord, adjustment_id, actor.org_id)
    if record.kind != ADJUSTMENT:
        raise DomainError(409, "invalid_state", "only adjustment records can be rejected")
    if record.status != PENDING:
        raise DomainError(409, "invalid_state", f"record status is {record.status}")
    if record.created_by == actor.user_id:
        raise DomainError(403, "forbidden_self_approval", "applicant cannot review own adjustment")
    if not actor.can_approve:
        raise DomainError(403, "forbidden_role", "review requires sales_supervisor or above")

    record.status = REJECTED
    record.approved_by = actor.user_id
    record.approved_at = utcnow()
    record.updated_at = utcnow()
    trace = dict(record.trace or {})
    trace.setdefault("approval_chain", []).append(
        {"reviewer": actor.user_id, "role": actor.role, "decision": "rejected", "at": utcnow().isoformat()}
    )
    record.trace = trace

    _audit(
        session,
        org_id=actor.org_id,
        entity_type=ADJUSTMENT,
        entity_id=record.id,
        action="reject",
        before=0,
        after=0,
        actor_id=record.created_by,
        approver_id=actor.user_id,
        detail={"reason": reason, "rejected_amount_cents": record.amount_cents},
    )
    return record


# --------------------------------------------------------------------------- #
# 3. 重算（AC3：锁定月改出差额调整，原记录保持 locked）
# --------------------------------------------------------------------------- #
def recalculate(session: Session, actor: Actor, payment_id: str) -> list[CommissionRecord]:
    """按原始输入快照重算某笔回款的提成。

    返回受影响的记录：未锁账月为 [作废的原记录, 新系统记录]；
    已锁账月为 [原记录（不变）, 新增差额调整]。
    """

    original = session.scalar(
        select(CommissionRecord)
        .where(
            CommissionRecord.org_id == actor.org_id,
            CommissionRecord.payment_id == payment_id,
            CommissionRecord.kind == SYSTEM,
            CommissionRecord.deleted_at.is_(None),
            CommissionRecord.voided_at.is_(None),
        )
        .order_by(CommissionRecord.created_at.desc())
    )
    if original is None:
        raise DomainError(404, "not_found", f"no active system record for payment {payment_id}")

    snapshot = dict(original.input_snapshot or {})
    if not snapshot:
        raise DomainError(409, "invalid_state", "record has no input_snapshot; cannot recompute")

    new_amount, steps = compute_gross(
        base_cents=snapshot["base_cents"],
        cumulative_before_cents=snapshot["cumulative_before_cents"],
        tiers=snapshot["tiers"],
        discount_bp=snapshot.get("discount_bp", 0),
        discount_factors=snapshot.get("discount_factors"),
    )
    old_amount = original.amount_cents

    if is_month_locked(session, actor.org_id, original.month):
        # 已锁账：原记录一字不改（locked 保持 True），差额落到当前未锁月
        delta = new_amount - old_amount
        target_month = first_open_month(session, actor.org_id, current_month())
        correction = CommissionRecord(
            org_id=actor.org_id,
            site_id=original.site_id,
            contract_id=original.contract_id,
            payment_id=original.payment_id,
            salesperson_id=original.salesperson_id,
            kind=ADJUSTMENT,
            amount_cents=delta,
            month=target_month,
            status=APPROVED,  # 系统重算结论，无需人工审批，但必须留痕
            locked=False,
            reason_category="recalculation_delta",
            note=f"recalculation delta for locked month {original.month}",
            supersedes_id=original.id,
            created_by=actor.user_id,
            approved_by="system:recalculate",
            approved_at=utcnow(),
            trace={
                "origin": "recalculate",
                "locked_month": original.month,
                "target_month": target_month,
                "source_record_id": original.id,
                "old_amount_cents": old_amount,
                "new_amount_cents": new_amount,
                "trace_steps": steps,
            },
        )
        session.add(correction)
        session.flush()
        _audit(
            session,
            org_id=actor.org_id,
            entity_type=ADJUSTMENT,
            entity_id=correction.id,
            action="recalculate_delta",
            before=old_amount,
            after=new_amount,
            actor_id=actor.user_id,
            approver_id="system:recalculate",
            detail={
                "source_record_id": original.id,
                "locked_month": original.month,
                "target_month": target_month,
                "delta_cents": delta,
            },
        )
        return [original, correction]

    # 未锁账：作废原记录 + 新增系统记录，supersedes 链指向原记录
    original.voided_at = utcnow()
    original.voided_reason = "recalculated"
    original.updated_at = utcnow()
    _audit(
        session,
        org_id=actor.org_id,
        entity_type=SYSTEM,
        entity_id=original.id,
        action="void",
        before=old_amount,
        after=0,
        actor_id=actor.user_id,
        detail={"reason": "recalculated", "new_amount_cents": new_amount},
    )

    replacement = CommissionRecord(
        org_id=actor.org_id,
        site_id=original.site_id,
        contract_id=original.contract_id,
        payment_id=original.payment_id,
        salesperson_id=original.salesperson_id,
        kind=SYSTEM,
        amount_cents=new_amount,
        month=original.month,
        status=APPROVED,
        locked=False,
        supersedes_id=original.id,
        created_by=actor.user_id,
        approved_by="system:recalculate",
        approved_at=utcnow(),
        input_snapshot=snapshot,
        trace={"origin": "recalculate", "superseded_by_recalc": True, "trace_steps": steps},
    )
    session.add(replacement)
    session.flush()
    _audit(
        session,
        org_id=actor.org_id,
        entity_type=SYSTEM,
        entity_id=replacement.id,
        action="recalculate",
        before=old_amount,
        after=new_amount,
        actor_id=actor.user_id,
        approver_id="system:recalculate",
        detail={"source_record_id": original.id, "month": original.month},
    )
    return [original, replacement]


# --------------------------------------------------------------------------- #
# 4. 月结汇总（AC1：未审批调整不入合计）
# --------------------------------------------------------------------------- #
def monthly_statement(session: Session, org_id: str, month: str) -> StatementOut:
    rows = session.scalars(
        select(CommissionRecord)
        .where(
            CommissionRecord.org_id == org_id,
            CommissionRecord.month == month,
            CommissionRecord.status == APPROVED,
            CommissionRecord.deleted_at.is_(None),
            CommissionRecord.voided_at.is_(None),
        )
        .order_by(CommissionRecord.created_at)
    ).all()

    pending_total = session.scalar(
        select(func.coalesce(func.sum(CommissionRecord.amount_cents), 0)).where(
            CommissionRecord.org_id == org_id,
            CommissionRecord.month == month,
            CommissionRecord.status == PENDING,
            CommissionRecord.deleted_at.is_(None),
        )
    ) or 0

    lines = [
        StatementLine(
            record_id=r.id,
            kind=r.kind,
            salesperson_id=r.salesperson_id,
            contract_id=r.contract_id,
            amount_cents=r.amount_cents,
        )
        for r in rows
    ]
    return StatementOut(
        org_id=org_id,
        month=month,
        total_cents=sum(line.amount_cents for line in lines),
        excluded_pending_cents=int(pending_total),
        lines=lines,
    )


# --------------------------------------------------------------------------- #
# 5. 锁账（管理动作，供月结流程调用）
# --------------------------------------------------------------------------- #
def lock_month(session: Session, actor: Actor, month: str) -> MonthLock:
    if is_month_locked(session, actor.org_id, month):
        raise DomainError(409, "invalid_state", f"month {month} already locked")
    # 锁账时把该月已生效记录标记 locked，便于对外表达「原记录保持 locked」
    for record in session.scalars(
        select(CommissionRecord).where(
            CommissionRecord.org_id == actor.org_id,
            CommissionRecord.month == month,
            CommissionRecord.status == APPROVED,
            CommissionRecord.voided_at.is_(None),
        )
    ):
        record.locked = True
    lock = MonthLock(org_id=actor.org_id, month=month, locked_by=actor.user_id)
    session.add(lock)
    return lock
