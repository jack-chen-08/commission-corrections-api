"""请求/响应模型（Pydantic v2）。"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ReasonCategory(str, Enum):
    """人工调整的原因类别（客户指定四类，不接受自由文本代替枚举）。"""

    rule_override = "rule_override"  # 规则表外特批
    data_entry_error = "data_entry_error"  # 数据录入错误
    owner_change = "owner_change"  # 业务员归属变更
    other = "other"  # 其他


class AdjustmentCreate(BaseModel):
    contract_id: str = Field(min_length=1, max_length=36)
    salesperson_id: str = Field(min_length=1, max_length=36)
    payment_id: str | None = Field(default=None, max_length=36)
    # 可正可负；单条绝对值上限由环境变量 COMM_ADJUST_MAX_CENTS 控制（默认 5,000,000 分）
    amount_cents: int = Field(description="调整金额，单位分，可正可负，不得为 0")
    reason_category: ReasonCategory
    note: str = Field(min_length=1, max_length=300)
    month: str | None = Field(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")

    @field_validator("amount_cents")
    @classmethod
    def _non_zero(cls, v: int) -> int:
        if v == 0:
            raise ValueError("amount_cents must not be zero")
        return v


class RecalculateRequest(BaseModel):
    payment_id: str = Field(min_length=1, max_length=36)


class RecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: str
    status: str
    amount_cents: int
    month: str
    contract_id: str
    salesperson_id: str
    payment_id: str | None
    locked: bool
    voided_reason: str | None
    supersedes_id: str | None
    approved_by: str | None


class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    action: str
    entity_type: str
    entity_id: str
    before_amount_cents: int | None
    after_amount_cents: int | None
    actor_id: str
    approver_id: str | None


class AdjustmentResult(BaseModel):
    record: RecordOut
    audit: list[AuditOut]


class StatementLine(BaseModel):
    record_id: str
    kind: str
    salesperson_id: str
    contract_id: str
    amount_cents: int


class StatementOut(BaseModel):
    org_id: str
    month: str
    total_cents: int
    excluded_pending_cents: int = Field(description="同月未审批调整的合计（仅披露，不计入 total）")
    lines: list[StatementLine]


class ErrorOut(BaseModel):
    detail: str
    code: Literal[
        "not_found",
        "forbidden_self_approval",
        "forbidden_role",
        "conflict",
        "limit_exceeded",
        "invalid_state",
    ]
