"""初始表结构：合同/回款只读副本、提成台账、月结锁、审计日志。

Revision ID: 0001_init
Revises:
Create Date: 2026-11-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_init"
down_revision = None
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "contracts",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("org_id", UUID, nullable=False),
        sa.Column("site_id", UUID, nullable=False),
        sa.Column("contract_no", sa.String(64), nullable=False),
        sa.Column("salesperson_id", UUID, nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("commission_rate_bp", sa.BigInteger(), nullable=False),
        sa.Column("signed_at", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_contracts_org_id", "contracts", ["org_id"])

    op.create_table(
        "payments",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("org_id", UUID, nullable=False),
        sa.Column("site_id", UUID, nullable=False),
        sa.Column("contract_id", UUID, sa.ForeignKey("contracts.id"), nullable=False),
        sa.Column("paid_amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("paid_at", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_payments_org_id", "payments", ["org_id"])
    op.create_index("ix_payments_contract_id", "payments", ["contract_id"])

    op.create_table(
        "month_locks",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("org_id", UUID, nullable=False),
        sa.Column("site_id", UUID, nullable=False),
        sa.Column("period_month", sa.Date(), nullable=False),
        sa.Column("locked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", UUID, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "uq_month_locks_org_period", "month_locks", ["org_id", "period_month"], unique=True
    )

    op.create_table(
        "commission_records",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("org_id", UUID, nullable=False),
        sa.Column("site_id", UUID, nullable=False),
        sa.Column("contract_id", UUID, sa.ForeignKey("contracts.id"), nullable=False),
        sa.Column("payment_id", UUID, sa.ForeignKey("payments.id"), nullable=True),
        sa.Column("salesperson_id", UUID, nullable=False),
        sa.Column("period_month", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("origin", sa.String(24), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("voided_reason", sa.String(32), nullable=True),
        sa.Column("supersedes_id", UUID, sa.ForeignKey("commission_records.id"), nullable=True),
        sa.Column("reason_category", sa.String(32), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("trace", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("approval_chain", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('base','adjustment')", name="ck_commission_records_kind"),
        sa.CheckConstraint(
            "status IN ('pending','active','voided')", name="ck_commission_records_status"
        ),
        sa.CheckConstraint(
            "origin IN ('calculation','manual','recalculation_delta')",
            name="ck_commission_records_origin",
        ),
        sa.CheckConstraint(
            "reason_category IS NULL OR reason_category IN "
            "('规则表外特批','数据录入错误','业务员归属变更','其他')",
            name="ck_commission_records_reason",
        ),
    )
    op.create_index(
        "ix_commission_records_org_period", "commission_records", ["org_id", "period_month"]
    )
    op.create_index("ix_commission_records_payment", "commission_records", ["payment_id"])
    op.create_index("ix_commission_records_status", "commission_records", ["status"])

    op.create_table(
        "audit_logs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("org_id", UUID, nullable=False),
        sa.Column("site_id", UUID, nullable=False),
        sa.Column("entity_type", sa.String(48), nullable=False),
        sa.Column("entity_id", UUID, nullable=False),
        sa.Column("action", sa.String(48), nullable=False),
        sa.Column("before_amount_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("after_amount_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("actor_id", UUID, nullable=False),
        sa.Column("approver_id", UUID, nullable=True),
        sa.Column("detail", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_audit_logs_entity", "audit_logs", ["entity_type", "entity_id"])
    op.create_index("ix_audit_logs_org_created", "audit_logs", ["org_id", "created_at"])


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("commission_records")
    op.drop_table("month_locks")
    op.drop_table("payments")
    op.drop_table("contracts")
