from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.models.models import AuditLog


def log_action(
    db: Session,
    *,
    user_id: Optional[int],
    action: str,
    entity_type: str,
    entity_id: Optional[int] = None,
    details: Optional[dict] = None,
    ip_address: Optional[str] = None,
) -> None:
    """
    Write a single audit log entry.

    Call AFTER the main operation succeeds but BEFORE db.commit(),
    so the audit log participates in the same transaction.
    """
    db.add(AuditLog(
        user_id=user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=details,
        ip_address=ip_address,
        created=datetime.now(timezone.utc),
    ))
