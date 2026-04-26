import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import get_current_admin
from app.core.email import send_invite_email, send_registration_confirmation
from app.core.audit import log_action
from app.models.models import Household, RegistrationRequest, User

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

    try:
        send_registration_confirmation(payload.email, payload.firstname)
    except Exception:
        pass  # Don't block registration if email fails

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


class ApproveRequest(BaseModel):
    household_id: int | None = None        # existing household
    create_household: bool = False          # explicitly create new
    waiver: str | None = None              # ISO date string for waiver
    household_address: str | None = None   # address for newly created household


@router.post("/{request_id}/approve")
def approve_registration(
    request_id: int,
    body: ApproveRequest = ApproveRequest(),
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

    # Household assignment: existing, create new, or none
    household_id = body.household_id
    if not household_id and body.create_household:
        last_hh = db.query(Household).order_by(Household.household_id.desc()).first()
        next_hh_id = (last_hh.household_id + 1) if last_hh else 1
        hh = Household(household_code=f"HH-{next_hh_id:04d}", name=reg.lastname, address=body.household_address or "")
        db.add(hh)
        db.flush()
        log_action(db, user_id=admin.user_id, action="auto_create", entity_type="household",
            entity_id=hh.household_id,
            details={"summary": f"Auto-created household '{reg.lastname}' for registration approval"})
        household_id = hh.household_id
        new_hh = hh
    else:
        new_hh = None

    from datetime import date as date_type
    user = User(
        firstname=reg.firstname,
        lastname=reg.lastname,
        email=reg.email,
        phone=reg.phone,
        password_hash="",
        is_admin=0,
        is_active=1,
        waiver=date_type.fromisoformat(body.waiver) if body.waiver else None,
        household_id=household_id,
        invite_token=token,
        invite_expires=expires,
    )
    db.add(user)
    db.flush()

    # Set primary contact on newly created household
    if new_hh:
        new_hh.primary_user_id = user.user_id

    reg.status      = "approved"
    reg.reviewed_by = admin.user_id
    reg.reviewed_at = datetime.utcnow()
    log_action(db, user_id=admin.user_id, action="approve", entity_type="registration", entity_id=request_id,
        details={"summary": f"Approved registration for {reg.firstname} {reg.lastname} ({reg.email})"})
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
    log_action(db, user_id=admin.user_id, action="reject", entity_type="registration", entity_id=request_id,
        details={"summary": f"Rejected registration for {reg.firstname} {reg.lastname} ({reg.email})"})
    db.commit()
    return {"detail": "Registration request rejected"}
