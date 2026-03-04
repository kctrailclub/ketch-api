from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_admin
from app.models.models import AuditLog, User

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("/")
def list_audit_logs(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    action: Optional[str] = None,
    entity_type: Optional[str] = None,
    user_id: Optional[int] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
):
    q = db.query(AuditLog)

    if action:
        q = q.filter(AuditLog.action == action)
    if entity_type:
        q = q.filter(AuditLog.entity_type == entity_type)
    if user_id:
        q = q.filter(AuditLog.user_id == user_id)
    if date_from:
        q = q.filter(AuditLog.created >= datetime(date_from.year, date_from.month, date_from.day))
    if date_to:
        next_day = date_to + timedelta(days=1)
        q = q.filter(AuditLog.created < datetime(next_day.year, next_day.month, next_day.day))

    total = q.count()
    logs = (
        q.order_by(AuditLog.created.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": max(1, (total + per_page - 1) // per_page),
        "data": [
            {
                "audit_log_id": l.audit_log_id,
                "user_id":      l.user_id,
                "user_name":    f"{l.user.firstname} {l.user.lastname}" if l.user else "System",
                "action":       l.action,
                "entity_type":  l.entity_type,
                "entity_id":    l.entity_id,
                "summary":      (l.details or {}).get("summary", ""),
                "details":      l.details,
                "ip_address":   l.ip_address,
                "created":      l.created.isoformat() if l.created else None,
            }
            for l in logs
        ],
    }
