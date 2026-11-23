from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends
from sqlalchemy.orm import Session

from app.db import get_session_factory
from app.iam import Principal, principal_from_headers


def get_db() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_principal_dep(principal: Principal = Depends(principal_from_headers)) -> Principal:
    return principal
