"""本地演示数据：虚构企业与虚构合同，仅用于 EVIDENCE 中的 curl 演示。

用法：
    python -m app.seed_demo seed            # 造一份已计算的提成记录（2026-09）
    python -m app.seed_demo correct 150000  # 模拟回款金额被更正为 150000 分
"""

from __future__ import annotations

import sys
import uuid
from datetime import date

from sqlalchemy import delete

from app.db import session_scope
from app.models import AuditLog, CommissionRecord, Contract, MonthLock, Payment

DEMO_ORG = uuid.UUID("33333333-3333-4333-8333-333333333333")
DEMO_SITE = uuid.UUID("44444444-4444-4444-8444-444444444444")
DEMO_SALESPERSON = uuid.UUID("55555555-5555-4555-8555-555555555555")
DEMO_OPERATOR = uuid.UUID("66666666-6666-4666-8666-666666666666")
DEMO_CONTRACT = uuid.UUID("77777777-7777-4777-8777-777777777777")
DEMO_PAYMENT = uuid.UUID("88888888-8888-4888-8888-888888888888")
DEMO_RECORD = uuid.UUID("9a9a9a9a-9a9a-4a9a-8a9a-9a9a9a9a9a9a")

PERIOD = date(2026, 9, 1)


def _wipe(session) -> None:
    session.execute(delete(AuditLog).where(AuditLog.org_id == DEMO_ORG))
    session.execute(delete(CommissionRecord).where(CommissionRecord.org_id == DEMO_ORG))
    session.execute(delete(MonthLock).where(MonthLock.org_id == DEMO_ORG))
    session.execute(delete(Payment).where(Payment.org_id == DEMO_ORG))
    session.execute(delete(Contract).where(Contract.org_id == DEMO_ORG))


def seed() -> dict[str, str]:
    with session_scope() as session:
        _wipe(session)
        session.add(
            Contract(
                id=DEMO_CONTRACT,
                org_id=DEMO_ORG,
                site_id=DEMO_SITE,
                contract_no="FICT-DEMO-2026-0001",
                salesperson_id=DEMO_SALESPERSON,
                amount_cents=1_000_000,
                commission_rate_bp=1000,
                signed_at=date(2026, 8, 20),
                created_by=DEMO_OPERATOR,
            )
        )
        session.flush()
        session.add(
            Payment(
                id=DEMO_PAYMENT,
                org_id=DEMO_ORG,
                site_id=DEMO_SITE,
                contract_id=DEMO_CONTRACT,
                paid_amount_cents=100_000,
                paid_at=PERIOD,
                created_by=DEMO_OPERATOR,
            )
        )
        session.flush()
        session.add(
            CommissionRecord(
                id=DEMO_RECORD,
                org_id=DEMO_ORG,
                site_id=DEMO_SITE,
                contract_id=DEMO_CONTRACT,
                payment_id=DEMO_PAYMENT,
                salesperson_id=DEMO_SALESPERSON,
                period_month=PERIOD,
                kind="base",
                origin="calculation",
                amount_cents=10_000,
                status="active",
                trace={
                    "origin": "rule_calculation",
                    "rule_version": "rule-table-stub/2026.1",
                    "inputs": {"paid_amount_cents": 100_000, "commission_rate_bp": 1000},
                    "output_cents": 10_000,
                },
                approval_chain=[],
                created_by=DEMO_OPERATOR,
            )
        )
    return {
        "ORG_ID": str(DEMO_ORG),
        "SITE_ID": str(DEMO_SITE),
        "CONTRACT_ID": str(DEMO_CONTRACT),
        "PAYMENT_ID": str(DEMO_PAYMENT),
        "RECORD_ID": str(DEMO_RECORD),
        "SALESPERSON_ID": str(DEMO_SALESPERSON),
    }


def correct_payment(cents: int) -> None:
    with session_scope() as session:
        payment = session.get(Payment, DEMO_PAYMENT)
        if payment is None:
            raise SystemExit("演示数据不存在，请先执行 python -m app.seed_demo seed")
        payment.paid_amount_cents = cents


def main(argv: list[str]) -> int:
    if not argv or argv[0] == "seed":
        for key, value in seed().items():
            print(f"{key}={value}")
        return 0
    if argv[0] == "correct":
        correct_payment(int(argv[1]))
        print(f"PAYMENT_UPDATED={argv[1]}")
        return 0
    print("用法：python -m app.seed_demo [seed|correct <cents>]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
