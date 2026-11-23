"""请求 / 响应模型。金额单位为分，全部为整数。"""

from __future__ import annotations

import uuid
from enum import Enum

from pydantic import BaseModel, Field, field_validator


class ReasonCategory(str, Enum):
    EXCEPTION = "规则表外特批"
    DATA_ERROR = "数据录入错误"
    OWNER_CHANGE = "业务员归属变更"
    OTHER = "其他"


def _clean_note(value: str, field_name: str, max_len: int = 300) -> str:
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} 不能为空")
    if len(text) > max_len:
        raise ValueError(f"{field_name} 长度不得超过 {max_len} 字，实际 {len(text)} 字")
    return text


class AdjustmentCreate(BaseModel):
    contract_id: uuid.UUID
    salesperson_id: uuid.UUID
    payment_id: uuid.UUID | None = None
    amount_cents: int = Field(strict=True, description="可正可负，单位分")
    reason_category: ReasonCategory
    note: str = Field(description="说明，≤300 字")
    period_month: str | None = Field(default=None, description="YYYY-MM，缺省取当前月")

    @field_validator("note")
    @classmethod
    def _check_note(cls, value: str) -> str:
        return _clean_note(value, "note")

    @field_validator("period_month")
    @classmethod
    def _check_period(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = value.split("-")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            raise ValueError("period_month 需为 YYYY-MM")
        year, month = int(parts[0]), int(parts[1])
        if not (2000 <= year <= 2100 and 1 <= month <= 12):
            raise ValueError("period_month 超出合理范围")
        return f"{year:04d}-{month:02d}"


class ApproveRequest(BaseModel):
    comment: str | None = None

    @field_validator("comment")
    @classmethod
    def _check_comment(cls, value: str | None) -> str | None:
        if value is None or value.strip() == "":
            return None
        return _clean_note(value, "comment")


class RejectRequest(BaseModel):
    reason: str

    @field_validator("reason")
    @classmethod
    def _check_reason(cls, value: str) -> str:
        return _clean_note(value, "reason")


class RecalculateRequest(BaseModel):
    payment_id: uuid.UUID
    correction_reason: str | None = Field(default=None, description="更正说明，≤300 字")

    @field_validator("correction_reason")
    @classmethod
    def _check_reason(cls, value: str | None) -> str | None:
        if value is None or value.strip() == "":
            return None
        return _clean_note(value, "correction_reason")


class MonthLockRequest(BaseModel):
    period_month: str
    locked: bool

    @field_validator("period_month")
    @classmethod
    def _check_period(cls, value: str) -> str:
        parts = value.split("-")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            raise ValueError("period_month 需为 YYYY-MM")
        return f"{int(parts[0]):04d}-{int(parts[1]):02d}"
