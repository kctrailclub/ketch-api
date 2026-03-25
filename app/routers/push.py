from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.push import send_push_to_user
from app.models.models import PushSubscription, User

router = APIRouter(prefix="/push", tags=["push"])


class SubscribeRequest(BaseModel):
    endpoint: str
    keys: dict  # {"p256dh": "...", "auth": "..."}


@router.get("/vapid-public-key")
def get_vapid_public_key():
    if not settings.vapid_public_key:
        raise HTTPException(status_code=404, detail="Push notifications not configured")
    return {"public_key": settings.vapid_public_key}


@router.post("/subscribe")
def subscribe(
    payload: SubscribeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    p256dh = payload.keys.get("p256dh", "")
    auth = payload.keys.get("auth", "")
    if not p256dh or not auth:
        raise HTTPException(status_code=400, detail="Missing encryption keys")

    existing = (
        db.query(PushSubscription)
        .filter(
            PushSubscription.user_id == current_user.user_id,
            PushSubscription.endpoint == payload.endpoint,
        )
        .first()
    )
    if existing:
        existing.p256dh = p256dh
        existing.auth = auth
    else:
        db.add(PushSubscription(
            user_id=current_user.user_id,
            endpoint=payload.endpoint,
            p256dh=p256dh,
            auth=auth,
        ))
    db.commit()
    return {"detail": "Subscribed"}


@router.post("/test")
def test_push(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    subs = db.query(PushSubscription).filter(PushSubscription.user_id == current_user.user_id).count()
    if subs == 0:
        raise HTTPException(status_code=400, detail="No push subscriptions found for your account. Make sure you allowed notifications.")
    sent = send_push_to_user(
        db, current_user.user_id,
        "Test Notification",
        "Push notifications are working!",
        f"{settings.frontend_url}/dashboard",
    )
    return {"detail": f"Sent to {sent} of {subs} device(s)"}


@router.post("/unsubscribe")
def unsubscribe(
    payload: SubscribeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub = (
        db.query(PushSubscription)
        .filter(
            PushSubscription.user_id == current_user.user_id,
            PushSubscription.endpoint == payload.endpoint,
        )
        .first()
    )
    if sub:
        db.delete(sub)
        db.commit()
    return {"detail": "Unsubscribed"}
