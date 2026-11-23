"""提成人工调整与重算接口验收测试。

每个 AC 对应一个显式命名的用例（见 DELIVERY.md 的 AC 对照表）。
全部数据为虚构数据。
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select

from app.models import AuditLog, CommissionRecord
from tests.conftest import (
    ADMIN,
    APPLICANT,
    APPLICANT_ID,
    APPROVER,
    APPROVER_ID,
    ORG_ID,
    OTHER_ORG_ID,
    PERIOD_CURRENT,
    PERIOD_LOCKED,
    SALESPERSON_ID,
    adjustment_payload,
    auth_headers,
    lock_period,
    seed_base_record,
    seed_contract,
    seed_payment,
)

BASE = "/api/admin/commission/corrections"


def _summary(client, year: int, month: int, headers=APPLICANT) -> dict:
    resp = client.get(f"{BASE}/monthly-summary?year={year}&month={month}", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _create_adjustment(client, contract, headers=APPLICANT, **overrides):
    resp = client.post(
        f"{BASE}/adjustments",
        json=adjustment_payload(contract.id, **overrides),
        headers=headers,
    )
    return resp


# ---------------------------------------------------------------- AC 1
def test_ac1_pending_adjustment_excluded_from_monthly_total(client, db):
    """未审批（pending）的调整不计入月结单合计。"""
    contract = seed_contract(db)
    seed_base_record(db, contract, None, 1_000_000, PERIOD_CURRENT)

    before = _summary(client, 2026, 11)
    assert before["total_cents"] == 1_000_000
    assert before["pending_count"] == 0

    created = _create_adjustment(client, contract, amount_cents=250_000)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "pending"
    assert body["kind"] == "adjustment"
    assert body["month_locked"] is False

    during = _summary(client, 2026, 11)
    assert during["total_cents"] == 1_000_000, "pending 调整不得计入月结合计"
    assert during["pending_total_cents"] == 250_000
    assert during["pending_count"] == 1

    approved = client.post(f"{BASE}/adjustments/{body['id']}/approve", json={"comment": "复核通过"}, headers=APPROVER)
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "active"

    after = _summary(client, 2026, 11)
    assert after["total_cents"] == 1_250_000
    assert after["pending_count"] == 0


# ---------------------------------------------------------------- AC 2
def test_ac2_applicant_cannot_approve_own_adjustment_returns_403(client, db):
    """申请人即使具备审批角色，审批自己的调整也返回 403。"""
    contract = seed_contract(db)
    created = _create_adjustment(client, contract, amount_cents=88_000)
    assert created.status_code == 201, created.text
    record_id = created.json()["id"]

    dual_role_headers = auth_headers(APPLICANT_ID, "sales_rep,channel_supervisor")
    resp = client.post(
        f"{BASE}/adjustments/{record_id}/approve",
        json={"comment": "自己批自己"},
        headers=dual_role_headers,
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == "SELF_APPROVAL_FORBIDDEN"

    db.expire_all()
    record = db.get(CommissionRecord, uuid.UUID(record_id))
    assert record.status == "pending"
    assert [step["action"] for step in record.approval_chain] == ["submitted"]

    rejected = client.post(
        f"{BASE}/adjustments/{record_id}/reject",
        json={"reason": "自审自批不合规"},
        headers=dual_role_headers,
    )
    assert rejected.status_code == 403
    assert rejected.json()["detail"]["code"] == "SELF_APPROVAL_FORBIDDEN"

    second_person = client.post(f"{BASE}/adjustments/{record_id}/approve", json={}, headers=APPROVER)
    assert second_person.status_code == 200, second_person.text


# ---------------------------------------------------------------- AC 3
def test_ac3_recalculate_locked_month_creates_delta_and_keeps_original_locked(client, db):
    """已锁定月份的记录重算后：原记录保持生效（月份仍锁定），差额计入当前未锁定月份。"""
    contract = seed_contract(db, rate_bp=1000)
    payment = seed_payment(db, contract, paid_amount_cents=100_000, paid_at=PERIOD_LOCKED)
    original = seed_base_record(db, contract, payment, 10_000, PERIOD_LOCKED)
    lock_period(db, PERIOD_LOCKED)

    payment.paid_amount_cents = 150_000
    db.commit()

    resp = client.post(
        f"{BASE}/recalculate",
        json={"payment_id": str(payment.id), "correction_reason": "回款金额录入更正：10 万→15 万分"},
        headers=APPROVER,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert body["mode"] == "delta_in_current_period"
    assert body["delta_cents"] == 5_000

    db.expire_all()
    original_after = db.get(CommissionRecord, original.id)
    assert original_after.status == "active", "原记录不得被作废"
    assert original_after.voided_reason is None
    assert original_after.supersedes_id is None
    assert original_after.amount_cents == 10_000

    original_view = body["original"]
    assert original_view["status"] == "active"
    assert original_view["month_locked"] is True
    assert original_view["period_month"] == "2026-09"

    delta = body["result_record"]
    assert delta["kind"] == "adjustment"
    assert delta["origin"] == "recalculation_delta"
    assert delta["status"] == "active"
    assert delta["amount_cents"] == 5_000
    assert delta["period_month"] == "2026-11"
    assert delta["month_locked"] is False
    assert delta["trace"]["original_record_id"] == str(original.id)

    locked_month = _summary(client, 2026, 9)
    assert locked_month["total_cents"] == 10_000
    assert locked_month["locked"] is True

    current_month = _summary(client, 2026, 11)
    assert current_month["total_cents"] == 5_000, "差额应计入当前未锁定月份"


# ---------------------------------------------------------------- AC 4
def test_ac4_audit_log_contains_before_after_amounts_and_approver(client, db):
    """审计日志含调整前后金额、操作人与审批人。"""
    contract = seed_contract(db)
    created = _create_adjustment(client, contract, amount_cents=501_234)
    assert created.status_code == 201, created.text
    record_id = created.json()["id"]

    approved = client.post(f"{BASE}/adjustments/{record_id}/approve", json={"comment": "财务已复核"}, headers=APPROVER)
    assert approved.status_code == 200, approved.text

    resp = client.get(
        f"{BASE}/audit-logs",
        params={"entity_id": record_id, "action": "commission.adjustment.approve"},
        headers=APPROVER,
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1
    entry = items[0]

    assert entry["before_amount_cents"] == 0
    assert entry["after_amount_cents"] == 501_234
    assert entry["actor_id"] == str(APPLICANT_ID), "操作人应为申请人"
    assert entry["approver_id"] == str(APPROVER_ID), "审批人应落库"
    assert entry["detail"]["period_total_before_cents"] == 0
    assert entry["detail"]["period_total_after_cents"] == 501_234
    assert entry["detail"]["counted_in_monthly_close"] is True
    assert entry["detail"]["comment"] == "财务已复核"

    create_logs = client.get(
        f"{BASE}/audit-logs",
        params={"entity_id": record_id, "action": "commission.adjustment.create"},
        headers=APPROVER,
    ).json()["items"]
    assert len(create_logs) == 1
    assert create_logs[0]["before_amount_cents"] == 0
    assert create_logs[0]["after_amount_cents"] == 501_234
    assert create_logs[0]["approver_id"] is None

    db.expire_all()
    rows = db.execute(select(AuditLog).where(AuditLog.entity_id == uuid.UUID(record_id))).scalars().all()
    assert {row.action for row in rows} == {
        "commission.adjustment.create",
        "commission.adjustment.approve",
    }


# ------------------------------------------------- AC 4（重算审计）
def test_ac4_recalc_audit_log_contains_before_after_amounts(client, db):
    """重算必须真的执行一次并留下审计：before/after 金额来自重算前后，而不是只断言 404 与驳回。"""
    contract = seed_contract(db, rate_bp=1000)
    payment = seed_payment(db, contract, paid_amount_cents=100_000, paid_at=PERIOD_LOCKED)
    original = seed_base_record(db, contract, payment, 10_000, PERIOD_LOCKED)
    lock_period(db, PERIOD_LOCKED)

    # 上游回款金额被更正：10 万 → 15 万，重算结果必须随之变化
    payment.paid_amount_cents = 150_000
    db.commit()

    resp = client.post(
        f"{BASE}/recalculate",
        json={"payment_id": str(payment.id), "correction_reason": "回款金额录入更正：10 万→15 万分"},
        headers=APPROVER,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["mode"] == "delta_in_current_period", body
    assert body["delta_cents"] == 5_000, body
    assert body["original"]["amount_cents"] == 10_000

    logs = client.get(
        f"{BASE}/audit-logs",
        params={"entity_id": body["result_record"]["id"], "action": "commission.recalculate"},
        headers=APPROVER,
    )
    assert logs.status_code == 200, logs.text
    items = logs.json()["items"]
    assert len(items) == 1, items
    entry = items[0]
    assert entry["before_amount_cents"] == 10_000, "审计须记录重算前金额"
    assert entry["after_amount_cents"] == 15_000, "审计须记录重算后金额"
    assert entry["detail"]["mode"] == "delta_in_current_period"
    assert entry["detail"]["delta_cents"] == 5_000
    assert entry["detail"]["payment_id"] == str(payment.id)

    db.expire_all()
    original_after = db.get(CommissionRecord, original.id)
    assert original_after.amount_cents == 10_000, "原记录金额不得被改写"


# ---------------------------------------------------------------- 其他场景
def test_recalculate_unlocked_month_voids_original_and_supersedes(client, db):
    contract = seed_contract(db, rate_bp=1200)
    payment = seed_payment(db, contract, paid_amount_cents=100_000, paid_at=PERIOD_CURRENT)
    original = seed_base_record(db, contract, payment, 12_000, PERIOD_CURRENT)

    payment.paid_amount_cents = 90_000
    db.commit()

    resp = client.post(f"{BASE}/recalculate", json={"payment_id": str(payment.id)}, headers=APPROVER)
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert body["mode"] == "replaced"
    assert body["original"]["status"] == "voided"
    assert body["original"]["voided_reason"] == "recalculated"
    assert body["result_record"]["supersedes_id"] == str(original.id)
    assert body["result_record"]["amount_cents"] == 10_800
    assert body["result_record"]["kind"] == "base"
    assert body["result_record"]["trace"]["origin"] == "rule_calculation"

    summary = _summary(client, 2026, 11)
    assert summary["total_cents"] == 10_800, "作废记录不计入合计，新记录计入"

    logs = client.get(
        f"{BASE}/audit-logs",
        params={"action": "commission.recalculate"},
        headers=APPROVER,
    ).json()["items"]
    assert len(logs) == 1
    assert logs[0]["before_amount_cents"] == 12_000
    assert logs[0]["after_amount_cents"] == 10_800
    assert logs[0]["detail"]["voided_reason"] == "recalculated"


def test_adjustment_amount_limit_and_negative_amount(client, db):
    contract = seed_contract(db)

    over = _create_adjustment(client, contract, amount_cents=5_000_001)
    assert over.status_code == 422, over.text
    assert over.json()["detail"]["code"] == "AMOUNT_EXCEEDS_LIMIT"
    assert over.json()["detail"]["limit_cents"] == 5_000_000

    zero = _create_adjustment(client, contract, amount_cents=0)
    assert zero.status_code == 422
    assert zero.json()["detail"]["code"] == "AMOUNT_ZERO"

    negative = _create_adjustment(client, contract, amount_cents=-5_000_000)
    assert negative.status_code == 201, negative.text
    assert negative.json()["amount_cents"] == -5_000_000
    assert negative.json()["status"] == "pending"


def test_adjustment_note_length_limit(client, db):
    contract = seed_contract(db)

    too_long = _create_adjustment(client, contract, note="虚构说明" * 76)  # 304 字
    assert too_long.status_code == 422

    blank = _create_adjustment(client, contract, note="   ")
    assert blank.status_code == 422

    exact = _create_adjustment(client, contract, note="虚" * 300)
    assert exact.status_code == 201, exact.text


def test_reject_keeps_record_out_of_monthly_total(client, db):
    contract = seed_contract(db)
    created = _create_adjustment(client, contract, amount_cents=120_000).json()

    resp = client.post(
        f"{BASE}/adjustments/{created['id']}/reject",
        json={"reason": "缺少财务复核单"},
        headers=APPROVER,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "voided"
    assert resp.json()["voided_reason"] == "rejected"
    assert _summary(client, 2026, 11)["total_cents"] == 0

    again = client.post(
        f"{BASE}/adjustments/{created['id']}/reject",
        json={"reason": "重复处理"},
        headers=APPROVER,
    )
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "NOT_PENDING"

    logs = client.get(
        f"{BASE}/audit-logs",
        params={"entity_id": created["id"], "action": "commission.adjustment.reject"},
        headers=APPROVER,
    ).json()["items"]
    assert len(logs) == 1
    assert logs[0]["before_amount_cents"] == 120_000
    assert logs[0]["after_amount_cents"] == 0
    assert logs[0]["approver_id"] == str(APPROVER_ID)


def test_adjustment_trace_records_manual_input_and_approval_chain(client, db):
    contract = seed_contract(db)
    created = _create_adjustment(client, contract, amount_cents=66_600).json()
    approved = client.post(f"{BASE}/adjustments/{created['id']}/approve", json={"comment": "同意"}, headers=APPROVER)
    assert approved.status_code == 200, approved.text
    body = approved.json()
    trace = body["trace"]

    assert trace["origin"] == "manual_input"
    assert trace["calculation"]["applied"] is False
    assert "rule_version" not in trace and "steps" not in trace
    assert trace["inputs"]["amount_cents"] == 66_600
    assert trace["inputs"]["reason_category"] == "数据录入错误"
    assert [step["action"] for step in trace["approval_chain"]] == ["submitted", "approved"]
    assert trace["approval_chain"][1]["actor_id"] == str(APPROVER_ID)
    assert [step["action"] for step in body["approval_chain"]] == ["submitted", "approved"]


def test_org_scope_isolation(client, db):
    other_contract = seed_contract(db, org_id=OTHER_ORG_ID, contract_no="FICT-2026-9999")
    resp = _create_adjustment(client, other_contract)
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "CONTRACT_NOT_FOUND"

    own_contract = seed_contract(db, contract_no="FICT-2026-0002")
    created = _create_adjustment(client, own_contract).json()
    cross = client.get(f"{BASE}/adjustments/{created['id']}", headers=auth_headers(APPROVER_ID, "channel_supervisor", OTHER_ORG_ID))
    assert cross.status_code == 404


def test_rank_requirements(client, db):
    contract = seed_contract(db)
    payment = seed_payment(db, contract, paid_amount_cents=100_000, paid_at=PERIOD_CURRENT)
    seed_base_record(db, contract, payment, 10_000, PERIOD_CURRENT)

    low_recalc = client.post(f"{BASE}/recalculate", json={"payment_id": str(payment.id)}, headers=APPLICANT)
    assert low_recalc.status_code == 403
    assert low_recalc.json()["detail"]["code"] == "RECALC_RANK_TOO_LOW"

    created = _create_adjustment(client, contract, amount_cents=1_000).json()
    low_approve = client.post(
        f"{BASE}/adjustments/{created['id']}/approve",
        json={},
        headers=auth_headers(APPROVER_ID, "channel_specialist"),
    )
    assert low_approve.status_code == 403
    assert low_approve.json()["detail"]["code"] == "APPROVER_RANK_TOO_LOW"


def test_adjustment_into_locked_month_is_rejected(client, db):
    contract = seed_contract(db)
    lock_period(db, PERIOD_LOCKED)
    resp = _create_adjustment(client, contract, period_month="2026-09")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "PERIOD_LOCKED"


def test_recalculate_without_active_record_returns_409(client, db):
    contract = seed_contract(db)
    payment = seed_payment(db, contract, paid_amount_cents=100_000, paid_at=PERIOD_CURRENT)
    original = seed_base_record(db, contract, payment, 10_000, PERIOD_CURRENT)
    original.status = "voided"
    original.voided_reason = "recalculated"
    db.commit()

    resp = client.post(f"{BASE}/recalculate", json={"payment_id": str(payment.id)}, headers=APPROVER)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "NO_ACTIVE_RECORD"


def test_month_lock_endpoint_requires_admin(client, db):
    denied = client.post(f"{BASE}/month-locks", json={"period_month": "2026-09", "locked": True}, headers=APPROVER)
    assert denied.status_code == 403
    ok = client.post(f"{BASE}/month-locks", json={"period_month": "2026-09", "locked": True}, headers=ADMIN)
    assert ok.status_code == 201, ok.text
    assert ok.json()["locked"] is True
    assert _summary(client, 2026, 9)["locked"] is True
