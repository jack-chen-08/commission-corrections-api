"""验收用例：逐条覆盖工单 AC1–AC4，附带权限与边界回归。

AC1 未审批的调整不得计入月结单合计数
AC2 审批人不得审批自己提交的调整（返回 403）
AC3 已锁定月份的重算生成差额调整记录，原记录保持 locked
AC4 审计日志包含调整前后金额与审批人
"""

from __future__ import annotations

import pytest

from commission_corrections import service
from commission_corrections.models import CommissionRecord

P = "/api/admin/commission/corrections"
ORG = "org-1"


def h(uid: str, role: str = "sales", org: str = ORG) -> dict:
    return {"X-User-Id": uid, "X-User-Role": role, "X-Org-Id": org}


def adj_body(**over) -> dict:
    body = {
        "contract_id": "c-1001",
        "salesperson_id": "sp-01",
        "payment_id": "pay-1001",
        "amount_cents": 100_000,
        "reason_category": "rule_override",
        "note": "客户口头承诺特殊折扣，主管已批",
        "month": "2026-11",
    }
    body.update(over)
    return body


def seed_system(db, *, payment_id="pay-lock", amount=15_000, month="2026-10",
                locked=False, base=400_000, tiers=((0, 500),), discount_bp=0) -> str:
    record = CommissionRecord(
        org_id=ORG,
        contract_id="c-9001",
        payment_id=payment_id,
        salesperson_id="sp-01",
        kind="system",
        amount_cents=amount,
        month=month,
        status="approved",
        locked=locked,
        input_snapshot={
            "base_cents": base,
            "cumulative_before_cents": 0,
            "tiers": [list(t) for t in tiers],
            "discount_bp": discount_bp,
            "discount_factors": [],
        },
    )
    db.add(record)
    db.commit()
    return record.id


# --------------------------------------------------------------------------- #
# AC1
# --------------------------------------------------------------------------- #
def test_ac1_pending_adjustment_not_counted_in_monthly_statement(client):
    created = client.post(f"{P}/adjustments", headers=h("u-sales"), json=adj_body())
    assert created.status_code == 201, created.text
    adj_id = created.json()["id"]
    assert created.json()["status"] == "pending"

    st = client.get(f"{P}/monthly-statement?month=2026-11", headers=h("u-auditor")).json()
    assert st["total_cents"] == 0, "未审批的调整不得计入月结单合计"
    assert st["excluded_pending_cents"] == 100_000

    approved = client.post(f"{P}/adjustments/{adj_id}/approve", headers=h("u-sup", "sales_supervisor"))
    assert approved.status_code == 200, approved.text
    assert approved.json()["record"]["status"] == "approved"

    st2 = client.get(f"{P}/monthly-statement?month=2026-11", headers=h("u-auditor")).json()
    assert st2["total_cents"] == 100_000, "审批通过后计入合计"
    assert st2["excluded_pending_cents"] == 0


def test_ac1_rejected_adjustment_never_counted(client):
    adj_id = client.post(f"{P}/adjustments", headers=h("u-sales"), json=adj_body()).json()["id"]
    rej = client.post(f"{P}/adjustments/{adj_id}/reject?reason=no-basis", headers=h("u-sup", "sales_supervisor"))
    assert rej.status_code == 200
    st = client.get(f"{P}/monthly-statement?month=2026-11", headers=h("u-auditor")).json()
    assert st["total_cents"] == 0


# --------------------------------------------------------------------------- #
# AC2
# --------------------------------------------------------------------------- #
def test_ac2_applicant_cannot_approve_own_adjustment(client):
    body = adj_body(amount_cents=50_000)
    adj_id = client.post(f"{P}/adjustments", headers=h("u-sup", "sales_supervisor"), json=body).json()["id"]

    resp = client.post(f"{P}/adjustments/{adj_id}/approve", headers=h("u-sup", "sales_supervisor"))
    assert resp.status_code == 403, "申请人审批自己的调整必须 403"
    assert resp.json()["code"] == "forbidden_self_approval"

    # 状态未被改动，仍未计入月结
    st = client.get(f"{P}/monthly-statement?month=2026-11", headers=h("u-auditor")).json()
    assert st["total_cents"] == 0
    assert st["excluded_pending_cents"] == 50_000


def test_ac2_applicant_cannot_reject_own_adjustment(client):
    adj_id = client.post(f"{P}/adjustments", headers=h("u-admin", "org_admin"), json=adj_body()).json()["id"]
    resp = client.post(f"{P}/adjustments/{adj_id}/reject", headers=h("u-admin", "org_admin"))
    assert resp.status_code == 403
    assert resp.json()["code"] == "forbidden_self_approval"


def test_ac2_underprivileged_role_cannot_approve(client):
    adj_id = client.post(f"{P}/adjustments", headers=h("u-sales"), json=adj_body()).json()["id"]
    resp = client.post(f"{P}/adjustments/{adj_id}/approve", headers=h("u-sales2", "sales"))
    assert resp.status_code == 403
    assert resp.json()["code"] == "forbidden_role"


# --------------------------------------------------------------------------- #
# AC3
# --------------------------------------------------------------------------- #
def test_ac3_locked_month_recalc_creates_delta_and_keeps_original_locked(client, db):
    original_id = seed_system(db, payment_id="pay-lock", amount=15_000, month="2026-10")
    lock = client.post(f"{P}/month-locks?month=2026-10", headers=h("u-admin", "org_admin"))
    assert lock.status_code == 201, lock.text

    resp = client.post(f"{P}/recalculate", headers=h("u-admin", "org_admin"), json={"payment_id": "pay-lock"})
    assert resp.status_code == 200, resp.text
    delta = resp.json()["record"]

    # 重算结果 400_000 分 × 5% = 20_000 分；差额 20_000 - 15_000 = 5_000 分
    assert delta["kind"] == "adjustment"
    assert delta["amount_cents"] == 5_000
    assert delta["month"] == service.current_month(), "差额必须落到当前未锁月"
    assert delta["month"] != "2026-10"
    assert delta["supersedes_id"] == original_id
    assert delta["status"] == "approved"

    db.expire_all()
    original = db.get(CommissionRecord, original_id)
    assert original.locked is True, "已锁定月份的原记录必须保持 locked"
    assert original.voided_at is None, "已锁定月份的原记录不得被作废"
    assert original.amount_cents == 15_000, "原记录金额不得被改写"


def test_ac3_unlocked_month_recalc_voids_original_and_supersedes(client, db):
    original_id = seed_system(db, payment_id="pay-open", amount=15_000, month="2026-11")
    resp = client.post(f"{P}/recalculate", headers=h("u-admin", "org_admin"), json={"payment_id": "pay-open"})
    assert resp.status_code == 200, resp.text
    new = resp.json()["record"]

    assert new["amount_cents"] == 20_000
    assert new["month"] == "2026-11"
    assert new["supersedes_id"] == original_id

    db.expire_all()
    original = db.get(CommissionRecord, original_id)
    assert original.voided_at is not None
    assert original.voided_reason == "recalculated"

    # 月结合计只算新记录，作废记录不重复计入
    st = client.get(f"{P}/monthly-statement?month=2026-11", headers=h("u-auditor")).json()
    assert st["total_cents"] == 20_000


# --------------------------------------------------------------------------- #
# AC4
# --------------------------------------------------------------------------- #
def test_ac4_audit_log_has_before_after_amounts_and_approver(client):
    adj_id = client.post(f"{P}/adjustments", headers=h("u-sales"), json=adj_body(amount_cents=80_000)).json()["id"]
    resp = client.post(f"{P}/adjustments/{adj_id}/approve", headers=h("u-sup", "sales_supervisor"))
    assert resp.status_code == 200

    logs = resp.json()["audit"]
    approve_logs = [log for log in logs if log["action"] == "approve"]
    assert len(approve_logs) == 1, "审批必须留下一条审计"
    log = approve_logs[0]
    assert log["before_amount_cents"] == 0
    assert log["after_amount_cents"] == 80_000
    assert log["approver_id"] == "u-sup"
    assert log["actor_id"] == "u-sales"


def test_ac4_recalc_audit_records_old_and_new_amount(client):
    resp = client.post(f"{P}/recalculate", headers=h("u-admin", "org_admin"), json={"payment_id": "missing"})
    assert resp.status_code == 404

    body = adj_body()
    adj_id = client.post(f"{P}/adjustments", headers=h("u-sales"), json=body).json()["id"]
    rej = client.post(f"{P}/adjustments/{adj_id}/reject?reason=dup", headers=h("u-sup", "sales_supervisor")).json()
    reject_logs = [log for log in rej["audit"] if log["action"] == "reject"]
    assert reject_logs and reject_logs[0]["approver_id"] == "u-sup"


# --------------------------------------------------------------------------- #
# 边界与输入校验
# --------------------------------------------------------------------------- #
def test_amount_limit_from_env(client, monkeypatch):
    monkeypatch.setenv("COMM_ADJUST_MAX_CENTS", "1000")
    resp = client.post(f"{P}/adjustments", headers=h("u-sales"), json=adj_body(amount_cents=1001))
    assert resp.status_code == 422
    assert resp.json()["code"] == "limit_exceeded"


@pytest.mark.parametrize("bad", [{"amount_cents": 0}, {"note": ""}, {"reason_category": "made_up"},
                                 {"month": "2026-13"}])
def test_invalid_payload_rejected(client, bad):
    resp = client.post(f"{P}/adjustments", headers=h("u-sales"), json=adj_body(**bad))
    assert resp.status_code == 422


def test_double_approve_conflicts(client):
    adj_id = client.post(f"{P}/adjustments", headers=h("u-sales"), json=adj_body()).json()["id"]
    assert client.post(f"{P}/adjustments/{adj_id}/approve", headers=h("u-sup", "sales_supervisor")).status_code == 200
    again = client.post(f"{P}/adjustments/{adj_id}/approve", headers=h("u-sup2", "org_admin"))
    assert again.status_code == 409
    assert again.json()["code"] == "invalid_state"


def test_adjustment_into_locked_month_rejected(client):
    assert client.post(f"{P}/month-locks?month=2026-10", headers=h("u-admin", "org_admin")).status_code == 201
    resp = client.post(f"{P}/adjustments", headers=h("u-sales"), json=adj_body(month="2026-10"))
    assert resp.status_code == 409
    assert resp.json()["code"] == "invalid_state"


def test_cross_org_isolation(client):
    adj_id = client.post(f"{P}/adjustments", headers=h("u-sales", org="org-1"), json=adj_body()).json()["id"]
    resp = client.post(f"{P}/adjustments/{adj_id}/approve", headers=h("u-sup", "sales_supervisor", org="org-2"))
    assert resp.status_code == 404, "跨 org 不得触碰他方记录"
