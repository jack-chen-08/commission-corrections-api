"""身份、角色与数据范围（S2-IAM）的本地桩。

客户工单允许「依赖其他工单的接口时，用接口桩或 mock 先行」。
真实 S2-IAM 落地后只需替换本文件 ``actor_from_headers`` 的实现，
service 层与测试不需要改动。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from fastapi import Header, HTTPException

# 角色阶梯：审批需要「招商主管或以上」
ROLE_RANK: dict[str, int] = {"sales": 10, "sales_supervisor": 20, "org_admin": 30}
APPROVER_MIN_RANK = 20


@dataclass(frozen=True)
class Actor:
    user_id: str
    role: str
    org_id: str
    site_id: str | None = None

    @property
    def rank(self) -> int:
        return ROLE_RANK.get(self.role, 0)

    @property
    def can_approve(self) -> bool:
        return self.rank >= APPROVER_MIN_RANK


def make_actor(user_id: str, role: str = "sales", org_id: str = "org-demo") -> Actor:
    return Actor(user_id=user_id, role=role, org_id=org_id)


def actor_from_headers(
    x_user_id: str = Header(..., alias="X-User-Id"),
    x_user_role: str = Header("sales", alias="X-User-Role"),
    x_org_id: str = Header("org-demo", alias="X-Org-Id"),
    x_site_id: str | None = Header(None, alias="X-Site-Id"),
) -> Actor:
    """从请求头解析调用者。生产环境由 S2-IAM 网关注入，不在本服务内签发凭证。"""

    if not x_user_id:
        raise HTTPException(status_code=401, detail="missing X-User-Id")
    if x_user_role not in ROLE_RANK:
        raise HTTPException(status_code=403, detail=f"unknown role: {x_user_role}")
    return Actor(user_id=x_user_id, role=x_user_role, org_id=x_org_id, site_id=x_site_id)


def max_adjust_cents() -> int:
    """单条调整绝对值上限，只从环境变量读取。"""

    return int(os.environ.get("COMM_ADJUST_MAX_CENTS", "5000000"))
