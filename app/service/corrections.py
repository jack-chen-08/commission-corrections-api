"""提成更正领域服务：人工调整（双人审批）+ 重算（作废重出 / 差额调整）。"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit import write_audit
from app.calculator import compute_commission
from app.config import get_settings
from app.iam import (
    APPROVER_MIN_RANK,
    RECALC_MIN_RANK,
    ApiError,
    Principal,
    require_rank,
)
from app.models import (
    KIND_ADJUSTMENT,
    KIND_BASE,
    STATUS_ACTIVE,
    STATUS_PENDING,
    STATUS_VOIDED,
    CommissionRecord,
    Contract,
    MonthLock,
    Payment,
)
from app.schemas import AdjustmentCreate, RecalculateRequest

VOID_REASON_RECALCULATED = "recalculated"
VOID_REASON_REJECTED = "rejected"

ENTITY_RECORD = "commission_record"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def parse_period(label: str) -> date:
    year_s, month_s = label.split("-")
    return date(int(year_s), int(month_s), 1)


def period_label(value: date) -> str:
    return value.strftime("%Y-%m")


def is_period_locked(session: Session, org_id: uuid.UUID, period: date) -> bool:
    row = session.execute(
        select(MonthLock).where(
            MonthLock.org_id == org_id,
            MonthLock.period_month == period,
            MonthLock.deleted_at.is_(None),
        )
    ).scalars().first()
    return bool(row and row.locked)


def period_total_cents(session: Session, org_id: uuid.UUID, period: date) -> int:
    """月结单口径：只累计 status=active 的记录，pending 调整不计入。"""
    total = session.execute(
        select(func.coalesce(func.sum(CommissionRecord.amount_cents), 0)).where(
            CommissionRecord.org_id == org_id,
            CommissionRecord.period_month == period,
            CommissionRecord.status == STATUS_ACTIVE,
            CommissionRecord.deleted_at.is_(None),
        )
    ).scalar_one()
    return int(total)


def _pending_total_cents(session: Session, org_id: uuid.UUID, period: date) -> int:
    total = session.execute(
        select(func.coalesce(func.sum(CommissionRecord.amount_cents), 0)).where(
            CommissionRecord.org_id == org_id,
            CommissionRecord.period_month == period,
            CommissionRecord.status == STATUS_PENDING,
            CommissionRecord.deleted_at.is_(None),
        )
    ).scalar_one()
    return int(total)


def _load_contract(session: Session, org_id: uuid.UUID, contract_id: uuid.UUID) -> Contract:
    contract = session.execute(
        select(Contract).where(
            Contract.id == contract_id,
            Contract.org_id == org_id,
            Contract.deleted_at.is_(None),
        )
    ).scalars().first()
    if contract is None:
        raise ApiError(404, "CONTRACT_NOT_FOUND", "合同不存在或不在当前数据范围内")
    return contract


def _load_payment(session: Session, org_id: uuid.UUID, payment_id: uuid.UUID) -> Payment:
    payment = session.execute(
        select(Payment).where(
            Payment.id == payment_id,
            Payment.org_id == org_id,
            Payment.deleted_at.is_(None),
        )
    ).scalars().first()
    if payment is None:
        raise ApiError(404, "PAYMENT_NOT_FOUND", "回款记录不存在或不在当前数据范围内")
    return payment


def _load_record(
    session: Session, org_id: uuid.UUID, record_id: uuid.UUID, *, kind: str | None = None
) -> CommissionRecord:
    stmt = select(CommissionRecord).where(
        CommissionRecord.id == record_id,
        CommissionRecord.org_id == org_id,
        CommissionRecord.deleted_at.is_(None),
    )
    if kind is not None:
        stmt = stmt.where(CommissionRecord.kind == kind)
    record = session.execute(stmt).scalars().first()
    if record is None:
        raise ApiError(404, "RECORD_NOT_FOUND", "记录不存在或不在当前数据范围内")
    return record


def record_to_dict(session: Session, record: CommissionRecord) -> dict:
    return {
        "id": str(record.id),
        "org_id": str(record.org_id),
        "site_id": str(record.site_id),
        "contract_id": str(record.contract_id),
        "payment_id": str(record.payment_id) if record.payment_id else None,
        "salesperson_id": str(record.salesperson_id),
        "period_month": period_label(record.period_month),
        "kind": record.kind,
        "origin": record.origin,
        "amount_cents": record.amount_cents,
        "status": record.status,
        "voided_reason": record.voided_reason,
        "supersedes_id": str(record.supersedes_id) if record.supersedes_id else None,
        "reason_category": record.reason_category,
        "note": record.note,
        "trace": record.trace,
        "approval_chain": record.approval_chain,
        "month_locked": is_period_locked(session, record.org_id, record.period_month),
        "created_at": _iso(record.created_at),
        "created_by": str(record.created_by) if record.created_by else None,
        "updated_at": _iso(record.updated_at),
    }


def create_adjustment(session: Session, principal: Principal, payload: AdjustmentCreate) -> dict:
    require_rank(principal, 1, "NO_ROLE", "当前身份无提成调整申请权限")

    settings = get_settings()
    contract = _load_contract(session, principal.org_id, payload.contract_id)

    if payload.amount_cents == 0:
        raise ApiError(422, "AMOUNT_ZERO", "调整金额不能为 0")
    if abs(payload.amount_cents) > settings.adjust_max_cents:
        raise ApiError(
            422,
            "AMOUNT_EXCEEDS_LIMIT",
            "单条调整金额超出上限",
            limit_cents=settings.adjust_max_cents,
            amount_cents=payload.amount_cents,
        )

    period = parse_period(payload.period_month) if payload.period_month else settings.current_period
    if is_period_locked(session, principal.org_id, period):
        raise ApiError(
            409,
            "PERIOD_LOCKED",
            "目标月份已锁定，不能新增调整，请改为当前未锁定月份",
            period_month=period_label(period),
        )

    if payload.payment_id is not None:
        payment = _load_payment(session, principal.org_id, payload.payment_id)
        if payment.contract_id != contract.id:
            raise ApiError(422, "PAYMENT_CONTRACT_MISMATCH", "回款不属于该合同")

    now = _now()
    submit_step = {
        "step": 1,
        "action": "submitted",
        "actor_id": str(principal.user_id),
        "actor_roles": list(principal.roles),
        "at": now.isoformat(),
        "amount_cents": payload.amount_cents,
    }
    trace = {
        "origin": "manual_input",
        "calculation": {
            "applied": False,
            "note": "人工调整不经过规则表计算，金额由申请人直接给定并留痕",
        },
        "inputs": {
            "contract_id": str(contract.id),
            "contract_no": contract.contract_no,
            "salesperson_id": str(payload.salesperson_id),
            "contract_salesperson_id": str(contract.salesperson_id),
            "salesperson_changed": str(payload.salesperson_id) != str(contract.salesperson_id),
            "payment_id": str(payload.payment_id) if payload.payment_id else None,
            "period_month": period_label(period),
            "amount_cents": payload.amount_cents,
            "reason_category": payload.reason_category.value,
            "note": payload.note,
        },
        "approval_chain": [submit_step],
    }

    record = CommissionRecord(
        org_id=principal.org_id,
        site_id=principal.site_id,
        contract_id=contract.id,
        payment_id=payload.payment_id,
        salesperson_id=payload.salesperson_id,
        period_month=period,
        kind=KIND_ADJUSTMENT,
        origin="manual",
        amount_cents=payload.amount_cents,
        status=STATUS_PENDING,
        reason_category=payload.reason_category.value,
        note=payload.note,
        trace=trace,
        approval_chain=[submit_step],
        created_by=principal.user_id,
    )
    session.add(record)
    session.flush()

    write_audit(
        session,
        org_id=principal.org_id,
        site_id=principal.site_id,
        entity_type=ENTITY_RECORD,
        entity_id=record.id,
        action="commission.adjustment.create",
        actor_id=principal.user_id,
        approver_id=None,
        before_amount_cents=0,
        after_amount_cents=payload.amount_cents,
        detail={
            "contract_id": str(contract.id),
            "salesperson_id": str(payload.salesperson_id),
            "period_month": period_label(period),
            "reason_category": payload.reason_category.value,
            "note": payload.note,
            "status": STATUS_PENDING,
            "counted_in_monthly_close": False,
            "period_total_before_cents": period_total_cents(session, principal.org_id, period),
            "period_total_after_cents": period_total_cents(session, principal.org_id, period),
        },
    )
    return record_to_dict(session, record)


def _guard_pending(record: CommissionRecord, session: Session) -> None:
    if record.status != STATUS_PENDING:
        raise ApiError(
            409,
            "NOT_PENDING",
            "该调整不处于待审批状态，不能重复处理",
            current_status=record.status,
        )
    if is_period_locked(session, record.org_id, record.period_month):
        raise ApiError(
            409,
            "PERIOD_LOCKED",
            "该调整所属月份已锁定，不能再审批入账",
            period_month=period_label(record.period_month),
        )


def approve_adjustment(
    session: Session, principal: Principal, record_id: uuid.UUID, comment: str | None
) -> dict:
    require_rank(
        principal,
        APPROVER_MIN_RANK,
        "APPROVER_RANK_TOO_LOW",
        "审批人需为招商主管或以上",
    )
    record = _load_record(session, principal.org_id, record_id, kind=KIND_ADJUSTMENT)
    _guard_pending(record, session)

    if record.created_by == principal.user_id:
        raise ApiError(403, "SELF_APPROVAL_FORBIDDEN", "不得审批本人提交的调整，需第二人审批")

    period = record.period_month
    total_before = period_total_cents(session, record.org_id, period)

    now = _now()
    step = {
        "step": len(record.approval_chain) + 1,
        "action": "approved",
        "actor_id": str(principal.user_id),
        "actor_roles": list(principal.roles),
        "at": now.isoformat(),
        "comment": comment,
    }
    chain = list(record.approval_chain) + [step]
    record.approval_chain = chain
    record.status = STATUS_ACTIVE
    trace = dict(record.trace or {})
    trace["approval_chain"] = chain
    record.trace = trace
    session.flush()

    total_after = period_total_cents(session, record.org_id, period)
    write_audit(
        session,
        org_id=record.org_id,
        site_id=record.site_id,
        entity_type=ENTITY_RECORD,
        entity_id=record.id,
        action="commission.adjustment.approve",
        actor_id=record.created_by,
        approver_id=principal.user_id,
        before_amount_cents=0,
        after_amount_cents=record.amount_cents,
        detail={
            "applicant_id": str(record.created_by),
            "approved_by": str(principal.user_id),
            "approver_roles": list(principal.roles),
            "comment": comment,
            "period_month": period_label(period),
            "reason_category": record.reason_category,
            "status_before": STATUS_PENDING,
            "status_after": STATUS_ACTIVE,
            "counted_in_monthly_close": True,
            "period_total_before_cents": total_before,
            "period_total_after_cents": total_after,
        },
    )
    return record_to_dict(session, record)


def reject_adjustment(
    session: Session, principal: Principal, record_id: uuid.UUID, reason: str
) -> dict:
    require_rank(
        principal,
        APPROVER_MIN_RANK,
        "APPROVER_RANK_TOO_LOW",
        "审批人需为招商主管或以上",
    )
    record = _load_record(session, principal.org_id, record_id, kind=KIND_ADJUSTMENT)
    if record.status != STATUS_PENDING:
        raise ApiError(
            409, "NOT_PENDING", "该调整不处于待审批状态", current_status=record.status
        )
    if record.created_by == principal.user_id:
        raise ApiError(403, "SELF_APPROVAL_FORBIDDEN", "不得处理本人提交的调整")

    period = record.period_month
    total_before = period_total_cents(session, record.org_id, period)
    now = _now()
    step = {
        "step": len(record.approval_chain) + 1,
        "action": "rejected",
        "actor_id": str(principal.user_id),
        "actor_roles": list(principal.roles),
        "at": now.isoformat(),
        "reason": reason,
    }
    chain = list(record.approval_chain) + [step]
    record.approval_chain = chain
    record.status = STATUS_VOIDED
    record.voided_reason = VOID_REASON_REJECTED
    trace = dict(record.trace or {})
    trace["approval_chain"] = chain
    record.trace = trace
    session.flush()

    total_after = period_total_cents(session, record.org_id, period)
    write_audit(
        session,
        org_id=record.org_id,
        site_id=record.site_id,
        entity_type=ENTITY_RECORD,
        entity_id=record.id,
        action="commission.adjustment.reject",
        actor_id=record.created_by,
        approver_id=principal.user_id,
        before_amount_cents=record.amount_cents,
        after_amount_cents=0,
        detail={
            "applicant_id": str(record.created_by),
            "rejected_by": str(principal.user_id),
            "reject_reason": reason,
            "period_month": period_label(period),
            "status_after": STATUS_VOIDED,
            "voided_reason": VOID_REASON_REJECTED,
            "counted_in_monthly_close": False,
            "period_total_before_cents": total_before,
            "period_total_after_cents": total_after,
        },
    )
    return record_to_dict(session, record)


def _active_base_for_payment(
    session: Session, org_id: uuid.UUID, payment_id: uuid.UUID
) -> CommissionRecord:
    record = session.execute(
        select(CommissionRecord)
        .where(
            CommissionRecord.org_id == org_id,
            CommissionRecord.payment_id == payment_id,
            CommissionRecord.kind == KIND_BASE,
            CommissionRecord.status == STATUS_ACTIVE,
            CommissionRecord.deleted_at.is_(None),
        )
        .order_by(CommissionRecord.created_at.desc(), CommissionRecord.id.desc())
    ).scalars().first()
    if record is None:
        history = session.execute(
            select(CommissionRecord).where(
                CommissionRecord.org_id == org_id,
                CommissionRecord.payment_id == payment_id,
                CommissionRecord.deleted_at.is_(None),
            )
        ).scalars().first()
        if history is not None:
            raise ApiError(
                409,
                "NO_ACTIVE_RECORD",
                "该回款当前没有生效的提成记录，无法重算",
            )
        raise ApiError(404, "RECORD_NOT_FOUND", "该回款没有可重算的提成记录")
    return record


def recalculate(session: Session, principal: Principal, payload: RecalculateRequest) -> dict:
    require_rank(
        principal,
        RECALC_MIN_RANK,
        "RECALC_RANK_TOO_LOW",
        "重算操作需招商主管或以上",
    )
    settings = get_settings()
    payment = _load_payment(session, principal.org_id, payload.payment_id)
    contract = _load_contract(session, principal.org_id, payment.contract_id)
    original = _active_base_for_payment(session, principal.org_id, payment.id)
    original_dict_before = record_to_dict(session, original)

    new_amount, rule_trace = compute_commission(contract, payment)
    locked = is_period_locked(session, principal.org_id, original.period_month)

    if locked:
        delta = new_amount - original.amount_cents
        result = {
            "mode": "delta_in_current_period",
            "original": original_dict_before,
            "delta_cents": delta,
            "result_record": None,
            "current_period": period_label(settings.current_period),
        }
        if delta == 0:
            write_audit(
                session,
                org_id=original.org_id,
                site_id=original.site_id,
                entity_type=ENTITY_RECORD,
                entity_id=original.id,
                action="commission.recalculate",
                actor_id=principal.user_id,
                approver_id=None,
                before_amount_cents=original.amount_cents,
                after_amount_cents=new_amount,
                detail={
                    "mode": "no_change",
                    "month_locked": True,
                    "delta_cents": 0,
                    "original_record_id": str(original.id),
                    "payment_id": str(payment.id),
                    "correction_reason": payload.correction_reason,
                },
            )
            return result

        current_period = settings.current_period
        if is_period_locked(session, principal.org_id, current_period):
            raise ApiError(
                409,
                "CURRENT_PERIOD_LOCKED",
                "原记录月份与当前月份均已锁定，需先解锁再重算",
            )

        note = (
            f"重算差额：回款 {str(payment.id)[:8]} 原 {original.amount_cents} 分 → "
            f"新 {new_amount} 分（原记录月份 {period_label(original.period_month)} 已锁定）"
        )
        note = note[:300]
        now = _now()
        delta_record = CommissionRecord(
            org_id=original.org_id,
            site_id=original.site_id,
            contract_id=original.contract_id,
            payment_id=original.payment_id,
            salesperson_id=original.salesperson_id,
            period_month=current_period,
            kind=KIND_ADJUSTMENT,
            origin="recalculation_delta",
            amount_cents=delta,
            status=STATUS_ACTIVE,
            reason_category="其他",
            note=note,
            trace={
                "origin": "recalculation_delta",
                "calculation": {"applied": True, "note": "差额由规则重算结果减去原记录金额得出"},
                "original_record_id": str(original.id),
                "original_amount_cents": original.amount_cents,
                "recalculated_amount_cents": new_amount,
                "delta_cents": delta,
                "original_period_month": period_label(original.period_month),
                "settled_period_month": period_label(current_period),
                "rule_trace": rule_trace,
            },
            approval_chain=[
                {
                    "step": 1,
                    "action": "system_generated",
                    "actor_id": str(principal.user_id),
                    "actor_roles": list(principal.roles),
                    "at": now.isoformat(),
                    "basis": "recalculate_on_locked_period",
                }
            ],
            created_by=principal.user_id,
        )
        session.add(delta_record)
        session.flush()

        write_audit(
            session,
            org_id=original.org_id,
            site_id=original.site_id,
            entity_type=ENTITY_RECORD,
            entity_id=delta_record.id,
            action="commission.recalculate",
            actor_id=principal.user_id,
            approver_id=None,
            before_amount_cents=original.amount_cents,
            after_amount_cents=new_amount,
            detail={
                "mode": "delta_in_current_period",
                "month_locked": True,
                "original_record_id": str(original.id),
                "original_period_month": period_label(original.period_month),
                "delta_record_id": str(delta_record.id),
                "delta_cents": delta,
                "settled_period_month": period_label(current_period),
                "payment_id": str(payment.id),
                "contract_id": str(contract.id),
                "correction_reason": payload.correction_reason,
                "operator_roles": list(principal.roles),
            },
        )
        result["result_record"] = record_to_dict(session, delta_record)
        return result

    original.status = STATUS_VOIDED
    original.voided_reason = VOID_REASON_RECALCULATED
    trace = dict(original.trace or {})
    trace["void"] = {
        "voided_reason": VOID_REASON_RECALCULATED,
        "voided_by": str(principal.user_id),
        "voided_at": _now().isoformat(),
        "correction_reason": payload.correction_reason,
    }
    original.trace = trace

    new_record = CommissionRecord(
        org_id=original.org_id,
        site_id=original.site_id,
        contract_id=original.contract_id,
        payment_id=original.payment_id,
        salesperson_id=contract.salesperson_id,
        period_month=original.period_month,
        kind=KIND_BASE,
        origin="calculation",
        amount_cents=new_amount,
        status=STATUS_ACTIVE,
        supersedes_id=original.id,
        trace=rule_trace,
        approval_chain=[],
        created_by=principal.user_id,
    )
    session.add(new_record)
    session.flush()

    write_audit(
        session,
        org_id=original.org_id,
        site_id=original.site_id,
        entity_type=ENTITY_RECORD,
        entity_id=new_record.id,
        action="commission.recalculate",
        actor_id=principal.user_id,
        approver_id=None,
        before_amount_cents=original.amount_cents,
        after_amount_cents=new_amount,
        detail={
            "mode": "replaced",
            "month_locked": False,
            "original_record_id": str(original.id),
            "new_record_id": str(new_record.id),
            "original_status_after": STATUS_VOIDED,
            "voided_reason": VOID_REASON_RECALCULATED,
            "supersedes_id": str(original.id),
            "delta_cents": new_amount - original.amount_cents,
            "payment_id": str(payment.id),
            "contract_id": str(contract.id),
            "correction_reason": payload.correction_reason,
            "operator_roles": list(principal.roles),
        },
    )
    return {
        "mode": "replaced",
        "original": record_to_dict(session, original),
        "result_record": record_to_dict(session, new_record),
        "delta_cents": new_amount - original_dict_before["amount_cents"],
        "current_period": period_label(settings.current_period),
    }


def monthly_summary(
    session: Session, principal: Principal, year: int, month: int
) -> dict:
    period = date(year, month, 1)
    rows = session.execute(
        select(CommissionRecord.status, func.count(), func.coalesce(func.sum(CommissionRecord.amount_cents), 0))
        .where(
            CommissionRecord.org_id == principal.org_id,
            CommissionRecord.period_month == period,
            CommissionRecord.deleted_at.is_(None),
        )
        .group_by(CommissionRecord.status)
    ).all()
    by_status = {status: {"count": int(count), "amount_cents": int(total)} for status, count, total in rows}
    total = period_total_cents(session, principal.org_id, period)
    return {
        "org_id": str(principal.org_id),
        "period_month": period_label(period),
        "locked": is_period_locked(session, principal.org_id, period),
        "total_cents": total,
        "active_count": by_status.get(STATUS_ACTIVE, {}).get("count", 0),
        "pending_total_cents": by_status.get(STATUS_PENDING, {}).get("amount_cents", 0),
        "pending_count": by_status.get(STATUS_PENDING, {}).get("count", 0),
        "voided_total_cents": by_status.get(STATUS_VOIDED, {}).get("amount_cents", 0),
        "voided_count": by_status.get(STATUS_VOIDED, {}).get("count", 0),
        "by_status": by_status,
    }


def list_adjustments(
    session: Session, principal: Principal, status: str | None, limit: int
) -> list[dict]:
    stmt = (
        select(CommissionRecord)
        .where(
            CommissionRecord.org_id == principal.org_id,
            CommissionRecord.kind == KIND_ADJUSTMENT,
            CommissionRecord.deleted_at.is_(None),
        )
        .order_by(CommissionRecord.created_at.desc())
        .limit(limit)
    )
    if status:
        stmt = stmt.where(CommissionRecord.status == status)
    rows = session.execute(stmt).scalars().all()
    return [record_to_dict(session, row) for row in rows]


def list_audit_logs(
    session: Session, principal: Principal, entity_id: uuid.UUID | None, action: str | None, limit: int
) -> list[dict]:
    from app.models import AuditLog

    stmt = (
        select(AuditLog)
        .where(AuditLog.org_id == principal.org_id, AuditLog.deleted_at.is_(None))
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
    )
    if entity_id:
        stmt = stmt.where(AuditLog.entity_id == entity_id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    rows = session.execute(stmt).scalars().all()
    return [
        {
            "id": str(row.id),
            "entity_type": row.entity_type,
            "entity_id": str(row.entity_id),
            "action": row.action,
            "before_amount_cents": row.before_amount_cents,
            "after_amount_cents": row.after_amount_cents,
            "actor_id": str(row.actor_id),
            "approver_id": str(row.approver_id) if row.approver_id else None,
            "detail": row.detail,
            "created_at": _iso(row.created_at),
        }
        for row in rows
    ]


def set_month_lock(
    session: Session, principal: Principal, period: date, locked: bool
) -> dict:
    from app.iam import MONTH_LOCK_MIN_RANK

    require_rank(
        principal,
        MONTH_LOCK_MIN_RANK,
        "MONTH_LOCK_RANK_TOO_LOW",
        "月结锁定需平台管理员",
    )
    row = session.execute(
        select(MonthLock).where(
            MonthLock.org_id == principal.org_id,
            MonthLock.period_month == period,
            MonthLock.deleted_at.is_(None),
        )
    ).scalars().first()
    if row is None:
        row = MonthLock(
            org_id=principal.org_id,
            site_id=principal.site_id,
            period_month=period,
            locked=locked,
            locked_at=_now() if locked else None,
            locked_by=principal.user_id if locked else None,
            created_by=principal.user_id,
        )
        session.add(row)
    else:
        row.locked = locked
        row.locked_at = _now() if locked else None
        row.locked_by = principal.user_id if locked else None
    session.flush()
    write_audit(
        session,
        org_id=principal.org_id,
        site_id=principal.site_id,
        entity_type="month_lock",
        entity_id=row.id,
        action="commission.month_lock.update",
        actor_id=principal.user_id,
        approver_id=None,
        before_amount_cents=0,
        after_amount_cents=0,
        detail={"period_month": period_label(period), "locked": locked},
    )
    return {
        "period_month": period_label(period),
        "locked": bool(row.locked),
        "locked_by": str(row.locked_by) if row.locked_by else None,
        "locked_at": _iso(row.locked_at),
    }
