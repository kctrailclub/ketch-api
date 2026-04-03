import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session, joinedload

from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import get_current_admin, get_current_user
from app.core.email import send_invite_email
from app.core.audit import log_action
from app.models.models import Household, RefreshToken, User

log = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["users"])


def _auto_create_household(db: Session, lastname: str, admin_user_id: int) -> "Household":
    """Create a household for a user who doesn't have one. Returns the Household object."""
    # Generate next household code
    last = db.query(Household).order_by(Household.household_id.desc()).first()
    next_id = (last.household_id + 1) if last else 1
    code = f"HH-{next_id:04d}"

    hh = Household(household_code=code, name=lastname)
    db.add(hh)
    db.flush()
    # primary_user_id set by caller after user is created
    log_action(db, user_id=admin_user_id, action="auto_create", entity_type="household",
        entity_id=hh.household_id,
        details={"summary": f"Auto-created household '{lastname}' for new member"})
    return hh


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class CreateUserRequest(BaseModel):
    firstname:    str
    lastname:     str
    email:        EmailStr
    phone:        str = ""
    is_admin:     bool = False
    youth:        bool = False
    is_tester:    bool = False
    household_id: Optional[int] = None       # existing household
    create_household: Optional[bool] = False  # explicitly create new
    # If household_id is set → use it; if create_household → auto-create; else → no household

_UNSET = object()

class UpdateUserRequest(BaseModel):
    firstname:    Optional[str] = None
    lastname:     Optional[str] = None
    email:        Optional[EmailStr] = None
    phone:        Optional[str] = None
    is_admin:     Optional[bool] = None
    is_active:    Optional[bool] = None
    youth:        Optional[bool] = None
    is_tester:    Optional[bool] = None
    household_id: Union[int, None] = _UNSET  # distinguish "not sent" from "set to null"
    new_password: Optional[str] = None

    class Config:
        arbitrary_types_allowed = True


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/", status_code=status.HTTP_201_CREATED)
def create_user(
    payload: CreateUserRequest,
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
):
    if db.query(User).filter(User.email == payload.email).first():
        raise HTTPException(status_code=400, detail="Email already registered")

    # Household assignment: explicit ID, create new, or none
    household_id = payload.household_id
    new_hh = None
    if not household_id and payload.create_household:
        new_hh = _auto_create_household(db, payload.lastname, _admin.user_id)
        household_id = new_hh.household_id

    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=settings.invite_token_expire_hours)

    user = User(
        firstname=payload.firstname,
        lastname=payload.lastname,
        email=payload.email,
        phone=payload.phone,
        password_hash="",          # empty until they accept the invite
        is_admin=int(payload.is_admin),
        is_active=0,               # inactive until they set a password
        youth=int(payload.youth),
        is_tester=int(payload.is_tester),
        household_id=household_id,
        invite_token=token,
        invite_expires=expires,
    )
    db.add(user)
    db.flush()

    # Set primary contact on newly created household
    if new_hh:
        new_hh.primary_user_id = user.user_id

    log_action(db, user_id=_admin.user_id, action="create", entity_type="user", entity_id=user.user_id,
        details={"summary": f"Created user {payload.firstname} {payload.lastname} ({payload.email})"})
    db.commit()
    db.refresh(user)

    try:
        send_invite_email(user.email, user.firstname, token)
        detail = "User created and invite email sent"
    except Exception:
        detail = "User created. Invite email could not be sent (SMTP not configured)."

    return {
        "user_id":  user.user_id,
        "email":    user.email,
        "detail":   detail,
    }


@router.get("/")
def list_users(
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
):
    users = db.query(User).options(joinedload(User.household)).order_by(User.lastname, User.firstname).all()
    return [
        {
            "user_id":        u.user_id,
            "firstname":      u.firstname,
            "lastname":       u.lastname,
            "email":          u.email,
            "phone":          u.phone,
            "is_admin":       bool(u.is_admin),
            "is_active":      bool(u.is_active),
            "youth":          bool(u.youth),
            "is_tester":      bool(u.is_tester),
            "household_id":   u.household_id,
            "household_name": u.household.name if u.household else None,
            "last_login":     u.last_login,
            "invite_pending": bool(u.invite_token),
        }
        for u in users
    ]


@router.get("/{user_id}")
def get_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Members can only view their own profile; admins can view anyone
    if not current_user.is_admin and current_user.user_id != user_id:
        raise HTTPException(status_code=403, detail="Access denied")

    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return {
        "user_id":      user.user_id,
        "firstname":    user.firstname,
        "lastname":     user.lastname,
        "email":        user.email,
        "phone":        user.phone,
        "is_admin":     bool(user.is_admin),
        "is_active":    bool(user.is_active),
        "youth":        bool(user.youth),
        "is_tester":    bool(user.is_tester),
        "waiver":       user.waiver,
        "household_id": user.household_id,
        "last_login":   user.last_login,
    }


@router.patch("/{user_id}")
def update_user(
    user_id: int,
    payload: UpdateUserRequest,
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    changes = {}
    for field in ("firstname", "lastname", "email", "phone", "is_admin", "is_active", "youth", "is_tester"):
        new_val = getattr(payload, field)
        if new_val is not None and str(new_val) != str(getattr(user, field)):
            changes[field] = {"old": getattr(user, field), "new": new_val}
    if payload.household_id is not _UNSET and payload.household_id != user.household_id:
        changes["household_id"] = {"old": user.household_id, "new": payload.household_id}

    if payload.firstname    is not None: user.firstname    = payload.firstname
    if payload.lastname     is not None: user.lastname     = payload.lastname
    if payload.email        is not None: user.email        = payload.email
    if payload.phone        is not None: user.phone        = payload.phone
    if payload.is_admin     is not None: user.is_admin     = int(payload.is_admin)
    if payload.is_active is not None:
        user.is_active = int(payload.is_active)
        if not payload.is_active:
            db.query(RefreshToken).filter(RefreshToken.user_id == user_id).delete()
    if payload.youth        is not None: user.youth        = int(payload.youth)
    if payload.is_tester    is not None: user.is_tester    = int(payload.is_tester)
    if payload.household_id is not _UNSET: user.household_id = payload.household_id
    if payload.new_password:
        if len(payload.new_password) < 8:
            raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
        from app.core.security import hash_password
        user.password_hash = hash_password(payload.new_password)
        user.invite_token  = None
        user.invite_expires = None
        user.is_active     = 1

    log_action(db, user_id=_admin.user_id, action="update", entity_type="user", entity_id=user_id,
        details={"summary": f"Updated user {user.firstname} {user.lastname}", "changes": changes})
    db.commit()
    return {"detail": "User updated"}


@router.post("/bulk-households")
def bulk_create_households(
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
):
    """Create households for all users who don't have one.
    Groups users by lastname so family members share a household."""
    orphans = db.query(User).filter(User.household_id.is_(None)).all()
    if not orphans:
        return {"created": 0, "assigned": 0, "detail": "All users already have households."}

    # Group by lastname
    by_name: dict[str, list] = {}
    for u in orphans:
        by_name.setdefault(u.lastname, []).append(u)

    # Get max household_id for code generation
    last = db.query(Household).order_by(Household.household_id.desc()).first()
    next_id = (last.household_id + 1) if last else 1

    created = 0
    assigned = 0
    for lastname, members in by_name.items():
        code = f"HH-{next_id:04d}"
        hh = Household(household_code=code, name=lastname)
        db.add(hh)
        db.flush()
        next_id = hh.household_id + 1

        for u in members:
            u.household_id = hh.household_id
            assigned += 1

        # Set first member as primary contact
        hh.primary_user_id = members[0].user_id

        log_action(db, user_id=_admin.user_id, action="bulk_create", entity_type="household",
            entity_id=hh.household_id,
            details={"summary": f"Bulk-created household '{lastname}' for {len(members)} member(s)",
                     "members": [f"{u.firstname} {u.lastname}" for u in members]})
        created += 1

    db.commit()
    return {
        "created": created,
        "assigned": assigned,
        "detail": f"Created {created} household(s) and assigned {assigned} member(s).",
    }


@router.post("/{user_id}/resend-invite")
def resend_invite(
    user_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.last_login:
        raise HTTPException(status_code=400, detail="User has already logged in")

    token = secrets.token_urlsafe(32)
    user.invite_token   = token
    user.invite_expires = datetime.now(timezone.utc) + timedelta(
        hours=settings.invite_token_expire_hours
    )
    log_action(db, user_id=_admin.user_id, action="resend_invite", entity_type="user", entity_id=user_id,
        details={"summary": f"Resent invite to {user.firstname} {user.lastname} ({user.email})"})
    db.commit()

    try:
        send_invite_email(user.email, user.firstname, token)
    except Exception:
        pass
    return {"detail": "Invite resent (email delivery requires SMTP configuration)"}


