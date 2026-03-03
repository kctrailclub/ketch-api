import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.email import send_password_reset_email
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models.models import User

router = APIRouter(prefix="/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"

class RefreshRequest(BaseModel):
    refresh_token: str

class SetPasswordRequest(BaseModel):
    token: str
    password: str

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr





# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email).first()

    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is deactivated",
        )
    if not user.password_hash:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Password not set — check your invite email",
        )

    user.last_login = datetime.now(timezone.utc)
    db.commit()

    return TokenResponse(
        access_token=create_access_token(user.user_id, bool(user.is_admin)),
        refresh_token=create_refresh_token(user.user_id),
    )


@router.post("/refresh", response_model=TokenResponse)
def refresh(payload: RefreshRequest, db: Session = Depends(get_db)):
    decoded = decode_token(payload.refresh_token)

    if not decoded or decoded.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )

    user = db.get(User, int(decoded["sub"]))
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )

    return TokenResponse(
        access_token=create_access_token(user.user_id, bool(user.is_admin)),
        refresh_token=create_refresh_token(user.user_id),
    )


@router.post("/set-password", status_code=status.HTTP_200_OK)
def set_password(payload: SetPasswordRequest, db: Session = Depends(get_db)):
    """
    Used for both first-time invite acceptance and password resets.
    The token is the invite_token stored on the user record.
    """
    user = db.query(User).filter(User.invite_token == payload.token).first()

    if not user:
        raise HTTPException(status_code=400, detail="Invalid or expired token")

    if user.invite_expires and user.invite_expires < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Token has expired — request a new one")

    if len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    user.password_hash  = hash_password(payload.password)
    user.invite_token   = None
    user.invite_expires = None
    user.is_active      = 1
    db.commit()

    return {"detail": "Password set successfully"}


@router.post("/change-password", status_code=status.HTTP_200_OK)
def change_password(
    payload: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not verify_password(payload.current_password, current_user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Current password is incorrect")
    if len(payload.new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    current_user.password_hash = hash_password(payload.new_password)
    db.commit()
    return {"detail": "Password updated successfully"}


@router.post("/forgot-password", status_code=status.HTTP_200_OK)
def forgot_password(payload: ForgotPasswordRequest, db: Session = Depends(get_db)):
    """
    Generates a reset token and emails it. Always returns 200 so we don't
    leak whether an email address is registered.
    """
    user = db.query(User).filter(User.email == payload.email).first()

    if user and user.is_active:
        token = secrets.token_urlsafe(32)
        user.invite_token   = token
        user.invite_expires = datetime.now(timezone.utc) + timedelta(
            hours=settings.invite_token_expire_hours
        )
        db.commit()
        send_password_reset_email(user.email, user.firstname, token)

    return {"detail": "If that email is registered you will receive a reset link shortly"}


@router.get("/me")
def me(current_user: User = Depends(get_current_user)):
    return {
        "user_id":      current_user.user_id,
        "firstname":    current_user.firstname,
        "lastname":     current_user.lastname,
        "email":        current_user.email,
        "is_admin":     bool(current_user.is_admin),
        "household_id": current_user.household_id,
    }
