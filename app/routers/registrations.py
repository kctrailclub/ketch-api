import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import get_current_admin
from app.core.email import send_invite_email
from app.models.models import RegistrationRequest, User

router = APIRouter(prefix="/registrations", tags=["registrations"])

_rate_cache: dict = {}
RATE_LIMIT  = 3
RATE_WINDOW = 3600

DISPOSABLE_DOMAINS = {
    "mailinator.com", "tempmail.com", "guerrillamail.com", "10minutemail.com",
    "throwam.com", "yopmail.com", "trashmail.com", "fakeinbox.com",
    "sharklasers.com", "spam4.me",
}


class RegistrationSubmitRequest(BaseModel):
    firstname: str
    lastname:  str
    email:     EmailStr
    phone:     str = ""
    honeypot:  str = ""


def _check_rate_limit(ip: str):
    now = datetime.utcnow().timestamp()
    window_start = now - RATE_WINDOW
    hits = _rate_cache.get(ip, [])
    hits = [t for t in hits if t > window_start]
    if len(hits) >= RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Too many registration attempts. Please try again later.")
    hits.append(now)
    _rate_cache[ip] = hits


@router.post("/", status_code=status.HTTP_201_CREATED)
def submit_registration(
    payload: RegistrationSubmitRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    if payload.honeypot:
        return {"detail": "Registration received"}

    ip = request.client.host
    _check_rate_limit(ip)

    domain = payload.email.split("@")[-1].lower()
    if domain in DISPOSABLE_DOMAINS:
        raise HTTPException(status_code=400, detail="Please use a permanent email address.")

    if db.query(User).filter(User.email == payload.email).first():
        raise HTTPException(status_code=400, detail="An account with this email already exists.")

    existing = db.query(RegistrationRequest).filter(
        RegistrationRequest.email == payload.email,
        RegistrationRequest.status == "pending",
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="A registration request for this email is already pending.")

    reg = RegistrationRequest(
        firstname=payload.firstname,
        lastname=payload.lastname,
        email=payload.email,
        phone=payload.phone,
    )
    db.add(reg)
    db.commit()
    return {"detail": "Registration received"}


@router.get("/")
def list_registrations(
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
):
    reqs = db.query(RegistrationRequest).filter(
        RegistrationRequest.status == "pending"
    ).order_by(RegistrationRequest.created).all()
    return [
        {
            "request_id": r.request_id,
            "firstname":  r.firstname,
            "lastname":   r.lastname,
            "email":      r.email,
            "phone":      r.phone,
            "created":    r.created,
        }
        for r in reqs
    ]


@router.post("/{request_id}/approve")
def approve_registration(
    request_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    reg = db.get(RegistrationRequest, request_id)
    if not reg or reg.status != "pending":
        raise HTTPException(status_code=404, detail="Registration request not found")

    if db.query(User).filter(User.email == reg.email).first():
        reg.status = "rejected"
        reg.reviewed_by = admin.user_id
        reg.reviewed_at = datetime.utcnow()
        db.commit()
        raise HTTPException(status_code=400, detail="An account with this email already exists.")

    token   = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=settings.invite_token_expire_hours)

    user = User(
        firstname=reg.firstname,
        lastname=reg.lastname,
        email=reg.email,
        phone=reg.phone,
        password_hash="",
        is_admin=0,
        is_active=1,
        invite_token=token,
        invite_expires=expires,
    )
    db.add(user)

    reg.status      = "approved"
    reg.reviewed_by = admin.user_id
    reg.reviewed_at = datetime.utcnow()
    db.commit()
    db.refresh(user)

    email_sent = True
    try:
        send_invite_email(user.email, user.firstname, token)
    except Exception:
        email_sent = False

    if email_sent:
        return {"detail": "Account created and invite sent to " + user.email}
    else:
        return {"detail": "Account created for " + user.email + ". Invite email could not be sent (SMTP not configured). Use the Resend button to send the invite once SMTP is set up."}


@router.post("/{request_id}/reject")
def reject_registration(
    request_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    reg = db.get(RegistrationRequest, request_id)
    if not reg or reg.status != "pending":
        raise HTTPException(status_code=404, detail="Registration request not found")

    reg.status      = "rejected"
    reg.reviewed_by = admin.user_id
    reg.reviewed_at = datetime.utcnow()
    db.commit()
    return {"detail": "Registration request rejected"}
