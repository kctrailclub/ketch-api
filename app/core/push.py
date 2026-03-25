import json
import logging

from pywebpush import webpush, WebPushException
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.models import PushSubscription

log = logging.getLogger(__name__)


def send_push_to_user(db: Session, user_id: int, title: str, body: str, url: str | None = None) -> int:
    """Send a push notification to all subscriptions for a user. Returns count sent."""
    if not settings.vapid_private_key or not settings.vapid_public_key:
        return 0

    subs = db.query(PushSubscription).filter(PushSubscription.user_id == user_id).all()
    if not subs:
        return 0

    payload = json.dumps({
        "title": title,
        "body": body,
        "url": url or settings.frontend_url,
    })

    sent = 0
    for sub in subs:
        subscription_info = {
            "endpoint": sub.endpoint,
            "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
        }
        try:
            webpush(
                subscription_info=subscription_info,
                data=payload,
                vapid_private_key=settings.vapid_private_key,
                vapid_claims={"sub": f"mailto:{settings.email_from}"},
            )
            sent += 1
        except WebPushException as exc:
            if exc.response and exc.response.status_code in (404, 410):
                log.info("Removing expired push subscription %s for user %s", sub.subscription_id, user_id)
                db.delete(sub)
                db.commit()
            else:
                log.error("Push failed for user %s: %s", user_id, exc)
        except Exception as exc:
            log.error("Push failed for user %s: %s", user_id, exc)

    return sent
