from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_admin, get_current_user
from app.models.models import Hour, Notification, Project, User

router = APIRouter(prefix="/hours", tags=["hours"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class SubmitHoursRequest(BaseModel):
    member_id:    int
    project_id:   int
    service_date: date
    hours:        float
    notes:        Optional[str] = None

class ReviewHoursRequest(BaseModel):
    status:      str          # "approved" or "rejected"
    status_note: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _notify_admins(db: Session, message: str, reference_id: int) -> None:
    admins = db.query(User).filter(User.is_admin == 1, User.is_active == 1).all()
    for admin in admins:
        db.add(Notification(
            user_id=admin.user_id,
            notification_type="hours_pending",
            reference_id=reference_id,
            message=message,
        ))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/", status_code=status.HTTP_201_CREATED)
def submit_hours(
    payload: SubmitHoursRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Members can only submit for themselves; admins can submit for anyone
    if not current_user.is_admin and current_user.user_id != payload.member_id:
        raise HTTPException(status_code=403, detail="You can only submit hours for yourself")

    if payload.hours <= 0:
        raise HTTPException(status_code=400, detail="Hours must be greater than zero")

    if payload.service_date.year != date.today().year:
        raise HTTPException(
            status_code=400,
            detail=f"Hours can only be logged for the current calendar year ({date.today().year}).",
        )

    project = db.get(Project, payload.project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.end_date and project.end_date < payload.service_date:
        raise HTTPException(status_code=400, detail="Project has ended")

    member = db.get(User, payload.member_id)
    if not member:
        raise HTTPException(status_code=404, detail="Member not found")

    auto_approve = bool(current_user.is_admin)

    hour = Hour(
        member_id=payload.member_id,
        project_id=payload.project_id,
        logged_by=current_user.user_id,
        service_date=payload.service_date,
        hours=payload.hours,
        notes=payload.notes,
        status="approved" if auto_approve else "pending",
        status_updated=datetime.now(timezone.utc) if auto_approve else None,
        status_by=current_user.user_id if auto_approve else None,
    )
    db.add(hour)
    db.flush()  # get hour_id before commit

    if not auto_approve:
        _notify_admins(
            db,
            f"{member.firstname} {member.lastname} submitted {payload.hours}h for {project.name}",
            hour.hour_id,
        )

    db.commit()
    detail = "Hours approved" if auto_approve else "Hours submitted and pending approval"
    return {"hour_id": hour.hour_id, "detail": detail}


@router.get("/")
def list_hours(
    member_id: Optional[int] = None,
    year: Optional[int] = None,
    status_filter: Optional[str] = None,
    household_scope: Optional[bool] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = db.query(Hour)

    # Non-admins (or admins viewing "My Hours" with household_scope=true)
    # see hours for all members of their household
    if not current_user.is_admin or household_scope:
        if member_id and member_id == current_user.user_id:
            q = q.filter(Hour.member_id == current_user.user_id)
        elif current_user.household_id:
            household_member_ids = [
                u.user_id for u in db.query(User).filter(User.household_id == current_user.household_id).all()
            ]
            q = q.filter(Hour.member_id.in_(household_member_ids))
        else:
            q = q.filter(Hour.member_id == current_user.user_id)
    elif member_id:
        q = q.filter(Hour.member_id == member_id)

    if year:
        q = q.filter(Hour.service_date.between(date(year, 1, 1), date(year, 12, 31)))
    if status_filter:
        q = q.filter(Hour.status == status_filter)

    hours = q.order_by(Hour.service_date.desc()).all()

    return [
        {
            "hour_id":      h.hour_id,
            "member_id":    h.member_id,
            "member_name":  f"{h.member.firstname} {h.member.lastname}",
            "project_id":   h.project_id,
            "project_name": h.project.name,
            "service_date": h.service_date,
            "hours":        float(h.hours),
            "notes":        h.notes,
            "status":       h.status,
            "status_note":  h.status_note,
            "created":      h.created,
        }
        for h in hours
    ]


@router.get("/pending")
def list_pending(
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
):
    hours = db.query(Hour).filter(Hour.status == "pending").order_by(Hour.created.asc()).all()
    return [
        {
            "hour_id":      h.hour_id,
            "member_name":  f"{h.member.firstname} {h.member.lastname}",
            "project_name": h.project.name,
            "service_date": h.service_date,
            "hours":        float(h.hours),
            "notes":        h.notes,
            "submitted_on": h.created,
        }
        for h in hours
    ]


@router.post("/{hour_id}/review")
def review_hours(
    hour_id: int,
    payload: ReviewHoursRequest,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    if payload.status not in ("approved", "rejected"):
        raise HTTPException(status_code=400, detail="Status must be 'approved' or 'rejected'")

    hour = db.get(Hour, hour_id)
    if not hour:
        raise HTTPException(status_code=404, detail="Hour record not found")
    if hour.status != "pending":
        raise HTTPException(status_code=400, detail="Only pending records can be reviewed")

    hour.status         = payload.status
    hour.status_note    = payload.status_note
    hour.status_updated = datetime.now(timezone.utc)
    hour.status_by      = current_admin.user_id

    db.commit()
    return {"detail": f"Hours {payload.status}"}


@router.post("/approve-all")
def approve_all_hours(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    pending = db.query(Hour).filter(Hour.status == "pending").all()
    if not pending:
        raise HTTPException(status_code=400, detail="No pending hours to approve")

    count = 0
    for hour in pending:
        hour.status = "approved"
        hour.status_updated = datetime.now(timezone.utc)
        hour.status_by = current_admin.user_id
        count += 1

    db.commit()
    return {"detail": f"{count} hour record{'s' if count != 1 else ''} approved"}


@router.delete("/{hour_id}")
def delete_hours(
    hour_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    hour = db.get(Hour, hour_id)
    if not hour:
        raise HTTPException(status_code=404, detail="Hour record not found")

    # Members can only delete their own pending records
    if not current_user.is_admin:
        if hour.member_id != current_user.user_id:
            raise HTTPException(status_code=403, detail="Access denied")
        if hour.status != "pending":
            raise HTTPException(status_code=400, detail="Only pending records can be deleted")

    db.delete(hour)
    db.commit()
    return {"detail": "Hour record deleted"}
