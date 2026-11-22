"""业务表模型。

统一字段约定（客户技术约定）：``id``(UUID 字符串)、``org_id``、``site_id``、
``created_at``、``created_by``、``updated_at``、``deleted_at``（软删除）；
金额一律以「分」存整数。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CommissionRecord(TimestampMixin, Base):
    """提成记录：系统计算结果（kind=system）与人工调整/差额（kind=adjustment）同表。

    ``status`` 语义（决定是否计入月结单，见 AC1）：

    - ``pending``：人工调整已提交、待第二人审批 —— **不计入月结单**
    - ``approved``：生效 —— 计入月结单
    - ``rejected``：被驳回 —— 不计入
    - ``voided``：被重算作废（``voided_reason=recalculated``）—— 不计入
    """

    __tablename__ = "commission_record"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False)
    site_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    contract_id: Mapped[str] = mapped_column(String(36), nullable=False)
    payment_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    salesperson_id: Mapped[str] = mapped_column(String(36), nullable=False)

    # system = 规则计算得出；adjustment = 人工调整或重算差额
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="system")
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[str] = mapped_column(String(7), nullable=False)  # YYYY-MM

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    voided_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    supersedes_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    reason_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 重算所需的输入快照（阶梯/折扣/本笔基数/当月累计），避免依赖上游表在此刻的现值
    input_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 人工调整记录 trace 的是人工输入与审批链，而不是规则计算过程
    trace: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    approved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_commission_org_month_status", "org_id", "month", "status"),
        Index("ix_commission_payment", "org_id", "payment_id"),
    )


class MonthLock(Base):
    """月度锁账表：某 org 某月一旦锁定，重算不得作废原记录（改出差额调整）。"""

    __tablename__ = "commission_month_lock"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False)
    month: Mapped[str] = mapped_column(String(7), nullable=False)
    locked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    locked_by: Mapped[str] = mapped_column(String(64), nullable=False, default="system")

    __table_args__ = (UniqueConstraint("org_id", "month", name="uq_month_lock_org_month"),)


class AuditLog(Base):
    """审计日志：两类更正操作都必须留下「修改前后金额 / 操作人 / 审批人」。"""

    __tablename__ = "commission_audit_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False)

    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)  # commission_record / adjustment
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)  # create/approve/reject/void/recalculate

    before_amount_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    after_amount_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)

    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    approver_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (Index("ix_audit_org_entity", "org_id", "entity_type", "entity_id"),)
