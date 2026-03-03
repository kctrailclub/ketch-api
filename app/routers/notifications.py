from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.models import Notification, User

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("/")
def list_notifications(
    unread_only: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = db.query(Notification).filter(Notification.user_id == current_user.user_id)
    if unread_only:
        q = q.filter(Notification.is_read == 0)
    notifications = q.order_by(Notification.created.desc()).all()
    return [
        {
            "notification_id":   n.notification_id,
            "notification_type": n.notification_type,
            "reference_id":      n.reference_id,
            "message":           n.message,
            "is_read":           bool(n.is_read),
            "created":           n.created,
        }
        for n in notifications
    ]


@router.post("/{notification_id}/read")
def mark_read(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    n = db.get(Notification, notification_id)
    if not n or n.user_id != current_user.user_id:
        return {"detail": "Not found"}
    n.is_read = 1
    db.commit()
    return {"detail": "Marked as read"}


@router.post("/read-all")
def mark_all_read(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    db.query(Notification).filter(
        Notification.user_id == current_user.user_id,
        Notification.is_read == 0,
    ).update({"is_read": 1})
    db.commit()
    return {"detail": "All notifications marked as read"}
