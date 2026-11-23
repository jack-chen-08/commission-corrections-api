from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://comm:comm_local_pw@127.0.0.1:55432/commission")
os.environ.setdefault("COMM_ADJUST_MAX_CENTS", "5000000")
os.environ.setdefault("COMM_CURRENT_PERIOD", "2026-11")
os.environ.setdefault("APP_ENV", "test")

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.db import dispose_engine, get_engine, get_session_factory  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.models import CommissionRecord, Contract, MonthLock, Payment  # noqa: E402

ORG_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_ORG_ID = uuid.UUID("99999999-9999-4999-8999-999999999999")
SITE_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
APPLICANT_ID = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
APPROVER_ID = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
ADMIN_ID = uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
SALESPERSON_ID = uuid.UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")

PERIOD_LOCKED = date(2026, 9, 1)
PERIOD_CURRENT = date(2026, 11, 1)


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(cfg, "head")
    yield
    dispose_engine()


@pytest.fixture()
def db():
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE audit_logs, commission_records, month_locks, payments, contracts RESTART IDENTITY CASCADE"
            )
        )
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(db):
    with TestClient(fastapi_app) as test_client:
        yield test_client


def auth_headers(
    user_id: uuid.UUID = APPLICANT_ID,
    roles: str = "sales_rep",
    org_id: uuid.UUID = ORG_ID,
) -> dict[str, str]:
    return {
        "X-User-Id": str(user_id),
        "X-Org-Id": str(org_id),
        "X-Site-Id": str(SITE_ID),
        "X-Roles": roles,
    }


APPLICANT = auth_headers(APPLICANT_ID, "sales_rep")
APPROVER = auth_headers(APPROVER_ID, "channel_supervisor")
ADMIN = auth_headers(ADMIN_ID, "platform_admin")


def seed_contract(
    db,
    *,
    org_id: uuid.UUID = ORG_ID,
    rate_bp: int = 1000,
    contract_no: str = "FICT-2026-0001",
    salesperson_id: uuid.UUID = SALESPERSON_ID,
) -> Contract:
    contract = Contract(
        org_id=org_id,
        site_id=SITE_ID,
        contract_no=contract_no,
        salesperson_id=salesperson_id,
        amount_cents=1_000_000,
        commission_rate_bp=rate_bp,
        signed_at=date(2026, 8, 15),
        created_by=ADMIN_ID,
    )
    db.add(contract)
    db.commit()
    return contract


def seed_payment(db, contract: Contract, paid_amount_cents: int, paid_at: date = PERIOD_LOCKED) -> Payment:
    payment = Payment(
        org_id=contract.org_id,
        site_id=SITE_ID,
        contract_id=contract.id,
        paid_amount_cents=paid_amount_cents,
        paid_at=paid_at,
        created_by=ADMIN_ID,
    )
    db.add(payment)
    db.commit()
    return payment


def seed_base_record(
    db,
    contract: Contract,
    payment: Payment | None,
    amount_cents: int,
    period: date = PERIOD_CURRENT,
    salesperson_id: uuid.UUID = SALESPERSON_ID,
) -> CommissionRecord:
    record = CommissionRecord(
        org_id=contract.org_id,
        site_id=SITE_ID,
        contract_id=contract.id,
        payment_id=payment.id if payment else None,
        salesperson_id=salesperson_id,
        period_month=period,
        kind="base",
        origin="calculation",
        amount_cents=amount_cents,
        status="active",
        trace={"origin": "rule_calculation", "rule_id": "R-CONTRACT-RATE"},
        approval_chain=[],
        created_by=ADMIN_ID,
    )
    db.add(record)
    db.commit()
    return record


def lock_period(db, period: date, org_id: uuid.UUID = ORG_ID) -> MonthLock:
    lock = MonthLock(
        org_id=org_id,
        site_id=SITE_ID,
        period_month=period,
        locked=True,
        locked_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        locked_by=ADMIN_ID,
        created_by=ADMIN_ID,
    )
    db.add(lock)
    db.commit()
    return lock


def adjustment_payload(contract_id, **overrides) -> dict:
    payload = {
        "contract_id": str(contract_id),
        "salesperson_id": str(SALESPERSON_ID),
        "amount_cents": 500_000,
        "reason_category": "数据录入错误",
        "note": "虚构测试：合同金额录入时少记一位，按财务复核结果补差。",
        "period_month": PERIOD_CURRENT.strftime("%Y-%m"),
    }
    payload.update(overrides)
    return payload
