"""IAM 接口桩：真实环境由 S2-IAM 签发的令牌解析用户、角色与数据范围。

本桩用请求头模拟已鉴权上下文，便于本地联调与自动化测试：
    X-User-Id / X-Org-Id / X-Site-Id / X-Roles(逗号分隔)
对接点见 README「对接点」章节：把 principal_from_headers 换成 S2-IAM 的
令牌校验 + 数据范围解析即可，其余业务代码不需要改动。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from fastapi import HTTPException, Request

# 角色用 ASCII 代码承载（HTTP 头必须是 ASCII），中文名仅用于展示与日志
ROLE_RANKS = {
    "sales_rep": 1,            # 业务员
    "channel_specialist": 2,   # 招商专员
    "channel_supervisor": 3,   # 招商主管
    "regional_manager": 4,     # 区域经理
    "platform_admin": 5,       # 平台管理员
}

ROLE_LABELS = {
    "sales_rep": "业务员",
    "channel_specialist": "招商专员",
    "channel_supervisor": "招商主管",
    "regional_manager": "区域经理",
    "platform_admin": "平台管理员",
}

# 审批人最低角色：招商主管或以上
APPROVER_MIN_RANK = ROLE_RANKS["channel_supervisor"]
# 重算操作最低角色：招商主管或以上
RECALC_MIN_RANK = ROLE_RANKS["channel_supervisor"]
# 月结锁定最低角色：平台管理员
MONTH_LOCK_MIN_RANK = ROLE_RANKS["platform_admin"]


class ApiError(HTTPException):
    def __init__(self, status_code: int, code: str, message: str, **extra):
        detail = {"code": code, "message": message}
        detail.update(extra)
        super().__init__(status_code=status_code, detail=detail)


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID
    org_id: uuid.UUID
    site_id: uuid.UUID
    roles: tuple[str, ...] = field(default_factory=tuple)

    @property
    def rank(self) -> int:
        return max((ROLE_RANKS.get(r, 0) for r in self.roles), default=0)

    @property
    def role_labels(self) -> list[str]:
        return [ROLE_LABELS.get(r, r) for r in self.roles]

    def has_rank(self, required: int) -> bool:
        return self.rank >= required


def _uuid_header(request: Request, name: str) -> uuid.UUID:
    raw = request.headers.get(name)
    if not raw:
        raise ApiError(401, "UNAUTHENTICATED", f"缺少请求头 {name}")
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise ApiError(401, "UNAUTHENTICATED", f"请求头 {name} 不是合法 UUID") from exc


def principal_from_headers(request: Request) -> Principal:
    user_id = _uuid_header(request, "X-User-Id")
    org_id = _uuid_header(request, "X-Org-Id")
    site_id = _uuid_header(request, "X-Site-Id")
    roles_raw = request.headers.get("X-Roles", "")
    roles = tuple(r.strip() for r in roles_raw.split(",") if r.strip())
    if not roles:
        raise ApiError(403, "NO_ROLE", "令牌未携带任何角色")
    unknown = [r for r in roles if r not in ROLE_RANKS]
    if unknown:
        raise ApiError(403, "UNKNOWN_ROLE", "令牌携带未定义角色", roles=unknown)
    return Principal(user_id=user_id, org_id=org_id, site_id=site_id, roles=roles)


def require_rank(principal: Principal, required: int, code: str, message: str) -> None:
    if not principal.has_rank(required):
        raise ApiError(403, code, message, required_rank=required, actual_rank=principal.rank)
