"""审计日志写入。生产环境在本地落库的同时投递 S2-AUDIT（见 README 对接点）。"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.models import AuditLog


def write_audit(
    session: Session,
    *,
    org_id: uuid.UUID,
    site_id: uuid.UUID,
    entity_type: str,
    entity_id: uuid.UUID,
    action: str,
    actor_id: uuid.UUID,
    approver_id: uuid.UUID | None,
    before_amount_cents: int,
    after_amount_cents: int,
    detail: dict | None = None,
) -> AuditLog:
    row = AuditLog(
        org_id=org_id,
        site_id=site_id,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        actor_id=actor_id,
        approver_id=approver_id,
        before_amount_cents=int(before_amount_cents),
        after_amount_cents=int(after_amount_cents),
        detail=detail or {},
        created_by=actor_id,
    )
    session.add(row)
    session.flush()
    return row
