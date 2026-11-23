"""提成规则计算桩：真实环境调用规则表引擎（S2 提成规则模块）。

这里实现单条公式：提成额 = 回款额(分) × 合同提成率(bp) / 10000，向下取整到分。
重算时由本模块产出 trace，说明这是规则计算过程（与人工调整的 trace 区分）。
"""

from __future__ import annotations

from app.models import Contract, Payment

RULE_VERSION = "rule-table-stub/2026.1"


def compute_commission(contract: Contract, payment: Payment) -> tuple[int, dict]:
    rate_bp = int(contract.commission_rate_bp)
    base_cents = int(payment.paid_amount_cents)
    amount_cents = base_cents * rate_bp // 10_000
    trace = {
        "origin": "rule_calculation",
        "rule_version": RULE_VERSION,
        "rule_id": "R-CONTRACT-RATE",
        "inputs": {
            "payment_id": str(payment.id),
            "paid_amount_cents": base_cents,
            "contract_id": str(contract.id),
            "commission_rate_bp": rate_bp,
        },
        "steps": [
            {"op": "multiply", "expr": "paid_amount_cents * commission_rate_bp", "value": base_cents * rate_bp},
            {"op": "floor_div", "expr": "/ 10000 (向下取整到分)", "value": amount_cents},
        ],
        "output_cents": amount_cents,
    }
    return amount_cents, trace
