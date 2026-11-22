#!/usr/bin/env python
"""真实 HTTP 冒烟客户端：对运行中的服务打完整链路，并断言 AC 结果。

用法：python scripts/smoke_client.py --port 8137 --db ./smoke.db
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, ".")
from commission_corrections.models import CommissionRecord  # noqa: E402

P = "/api/admin/commission/corrections"
ORG = "org-smoke"
ok = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global ok
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {label}{('  ' + extra) if extra else ''}")
    if not cond:
        raise SystemExit(f"smoke failed: {label}")
    ok += 1


def h(uid: str, role: str = "sales") -> dict:
    return {"X-User-Id": uid, "X-User-Role": role, "X-Org-Id": ORG}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8137)
    ap.add_argument("--db", default="./smoke.db")
    args = ap.parse_args()
    base = f"http://127.0.0.1:{args.port}"
    month = dt.datetime.now().strftime("%Y-%m")
    c = httpx.Client(base_url=base, timeout=10)

    print(f"服务: {base}   库: {args.db}   月份: {month}")
    print("\n[1] 健康检查与建表")
    check("GET /healthz == 200", c.get("/healthz").status_code == 200)

    print("\n[2] AC1 未审批调整不计入月结单")
    r = c.post(f"{P}/adjustments", headers=h("sales-01"), json={
        "contract_id": "c-1001", "salesperson_id": "sp-01", "payment_id": "pay-1001",
        "amount_cents": 100_000, "reason_category": "rule_override",
        "note": "客户口头承诺特殊折扣，主管已批", "month": month})
    check("提交调整 201", r.status_code == 201, f"id={r.json().get('id','')[:8]}")
    adj_id = r.json()["id"]
    st = c.get(f"{P}/monthly-statement", params={"month": month}, headers=h("auditor")).json()
    check("月结合计 = 0", st["total_cents"] == 0, f"pending={st['excluded_pending_cents']}")

    print("\n[3] AC2 申请人不得自审（403）")
    r = c.post(f"{P}/adjustments", headers=h("sup-01", "sales_supervisor"), json={
        "contract_id": "c-1001", "salesperson_id": "sp-01", "amount_cents": 30_000,
        "reason_category": "data_entry_error", "note": "金额录错", "month": month})
    self_id = r.json()["id"]
    r = c.post(f"{P}/adjustments/{self_id}/approve", headers=h("sup-01", "sales_supervisor"))
    check("自审返回 403", r.status_code == 403, f"code={r.json().get('code')}")

    print("\n[4] AC4 他人审批生效并留痕")
    r = c.post(f"{P}/adjustments/{adj_id}/approve", headers=h("sup-02", "sales_supervisor"))
    check("审批 200", r.status_code == 200)
    log = [x for x in r.json()["audit"] if x["action"] == "approve"][0]
    check("审计含前后金额与审批人",
          log["before_amount_cents"] == 0 and log["after_amount_cents"] == 100_000
          and log["approver_id"] == "sup-02")
    st = c.get(f"{P}/monthly-statement", params={"month": month}, headers=h("auditor")).json()
    check("月结合计 = 100000", st["total_cents"] == 100_000)

    print("\n[5] AC3 已锁定月份重算 → 差额调整，原记录保持 locked")
    eng = create_engine(f"sqlite+pysqlite:///{args.db}", future=True)
    Session = sessionmaker(bind=eng, expire_on_commit=False)
    with Session() as s:
        rec = CommissionRecord(
            org_id=ORG, contract_id="c-9001", payment_id="pay-lock", salesperson_id="sp-01",
            kind="system", amount_cents=15_000, month="2026-10", status="approved", locked=False,
            input_snapshot={"base_cents": 400_000, "cumulative_before_cents": 0,
                            "tiers": [[0, 500]], "discount_bp": 0, "discount_factors": []})
        s.add(rec)
        s.commit()
        orig_id = rec.id

    r = c.post(f"{P}/month-locks", params={"month": "2026-10"}, headers=h("admin", "org_admin"))
    check("锁账 2026-10 → 201", r.status_code == 201)
    r = c.post(f"{P}/recalculate", headers=h("admin", "org_admin"), json={"payment_id": "pay-lock"})
    check("重算 200", r.status_code == 200, r.text[:120] if r.status_code != 200 else "")
    delta = r.json()["record"]
    check("差额记录 20000-15000=5000 分", delta["amount_cents"] == 5_000,
          f"month={delta['month']} kind={delta['kind']}")
    with Session() as s:
        orig = s.get(CommissionRecord, orig_id)
        check("原记录 locked=True 且未作废且金额未改",
              orig.locked is True and orig.voided_at is None and orig.amount_cents == 15_000)

    print(f"\n全部 {ok} 项断言通过（真实 HTTP，非 TestClient）")


if __name__ == "__main__":
    main()
