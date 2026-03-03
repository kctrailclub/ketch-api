from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_admin, get_current_user
from app.models.models import Household, Notification, User

router = APIRouter(prefix="/households", tags=["households"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class CreateHouseholdRequest(BaseModel):
    name:    str
    address: str = ""

class UpdateHouseholdRequest(BaseModel):
    name:            Optional[str] = None
    address:         Optional[str] = None
    primary_user_id: Optional[int] = None

class JoinRequest(BaseModel):
    household_id: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _next_household_code(db: Session) -> str:
    last = db.query(Household).order_by(Household.household_id.desc()).first()
    next_id = (last.household_id + 1) if last else 1
    return f"HH-{next_id:04d}"


def _notify_admins(db: Session, message: str, reference_id: int) -> None:
    admins = db.query(User).filter(User.is_admin == 1, User.is_active == 1).all()
    for admin in admins:
        db.add(Notification(
            user_id=admin.user_id,
            notification_type="household_request",
            reference_id=reference_id,
            message=message,
        ))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/", status_code=status.HTTP_201_CREATED)
def create_household(
    payload: CreateHouseholdRequest,
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
):
    household = Household(
        household_code=_next_household_code(db),
        name=payload.name,
        address=payload.address,
    )
    db.add(household)
    db.commit()
    db.refresh(household)
    return {
        "household_id":   household.household_id,
        "household_code": household.household_code,
        "detail":         "Household created",
    }


@router.get("/")
def list_households(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    households = db.query(Household).order_by(Household.name).all()
    return [
        {
            "household_id":      h.household_id,
            "household_code":    h.household_code,
            "name":              h.name,
            "address":           h.address,
            "primary_user_id":   h.primary_user_id,
            "member_count":      len(h.members),
        }
        for h in households
    ]


@router.get("/{household_id}")
def get_household(
    household_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Members can only view their own household
    if not current_user.is_admin and current_user.household_id != household_id:
        raise HTTPException(status_code=403, detail="Access denied")

    h = db.get(Household, household_id)
    if not h:
        raise HTTPException(status_code=404, detail="Household not found")

    return {
        "household_id":    h.household_id,
        "household_code":  h.household_code,
        "name":            h.name,
        "address":         h.address,
        "primary_user_id": h.primary_user_id,
        "members": [
            {
                "user_id":   m.user_id,
                "firstname": m.firstname,
                "lastname":  m.lastname,
                "email":     m.email,
            }
            for m in h.members
        ],
    }


@router.patch("/{household_id}")
def update_household(
    household_id: int,
    payload: UpdateHouseholdRequest,
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
):
    h = db.get(Household, household_id)
    if not h:
        raise HTTPException(status_code=404, detail="Household not found")

    if payload.name            is not None: h.name            = payload.name
    if payload.address         is not None: h.address         = payload.address
    if payload.primary_user_id is not None: h.primary_user_id = payload.primary_user_id

    db.commit()
    return {"detail": "Household updated"}


@router.post("/join-request")
def request_to_join(
    payload: JoinRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.household_id:
        raise HTTPException(status_code=400, detail="You are already a member of a household")
    if current_user.household_request_id:
        raise HTTPException(status_code=400, detail="You already have a pending join request")

    h = db.get(Household, payload.household_id)
    if not h:
        raise HTTPException(status_code=404, detail="Household not found")

    current_user.household_request_id = payload.household_id
    db.flush()

    _notify_admins(
        db,
        f"{current_user.firstname} {current_user.lastname} requested to join {h.name} ({h.household_code})",
        current_user.user_id,
    )
    db.commit()
    return {"detail": "Join request submitted and pending admin approval"}


@router.post("/{household_id}/members/{user_id}/approve")
def approve_join_request(
    household_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.household_request_id != household_id:
        raise HTTPException(status_code=400, detail="No pending request for this household")

    user.household_id         = household_id
    user.household_request_id = None

    # Set as primary contact if household has none
    h = db.get(Household, household_id)
    if not h.primary_user_id:
        h.primary_user_id = user_id

    db.commit()
    return {"detail": "Join request approved"}


@router.post("/{household_id}/members/{user_id}/reject")
def reject_join_request(
    household_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.household_request_id != household_id:
        raise HTTPException(status_code=400, detail="No pending request for this household")

    user.household_request_id = None
    db.commit()
    return {"detail": "Join request rejected"}


@router.delete("/{household_id}/members/{user_id}")
def remove_member(
    household_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
):
    user = db.get(User, user_id)
    if not user or user.household_id != household_id:
        raise HTTPException(status_code=404, detail="User not in this household")

    user.household_id = None

    # Clear primary contact if it was this user
    h = db.get(Household, household_id)
    if h.primary_user_id == user_id:
        # Reassign to another member if one exists
        other = db.query(User).filter(
            User.household_id == household_id,
            User.user_id != user_id
        ).first()
        h.primary_user_id = other.user_id if other else None

    db.commit()
    return {"detail": "Member removed from household"}
