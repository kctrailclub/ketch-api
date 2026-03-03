import html

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_admin
from app.core.email import send_raw_email
from app.models.models import Hour, User, Household
from sqlalchemy import func
from datetime import date

router = APIRouter(prefix="/settings", tags=["settings"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_setting(db: Session, key: str) -> str:
    row = db.execute(text("SELECT `value` FROM settings WHERE `key` = :k"), {"k": key}).fetchone()
    return row[0] if row else None


def set_setting(db: Session, key: str, value: str):
    db.execute(
        text("INSERT INTO settings (`key`, `value`) VALUES (:k, :v) ON DUPLICATE KEY UPDATE `value` = :v"),
        {"k": key, "v": value}
    )
    db.commit()


def apply_template(template: str, replacements: dict) -> str:
    for k, v in replacements.items():
        template = template.replace(f"{{{{{k}}}}}", html.escape(str(v)))
    return template


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class SettingsUpdate(BaseModel):
    reward_threshold:    int
    reward_email_subject: str
    reward_email_body:   str
    nudge_email_subject: str
    nudge_email_body:    str


class SendRewardsRequest(BaseModel):
    email_type:    str   # "reward" or "nudge"
    household_ids: list[int]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/rewards")
def get_reward_settings(
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
):
    threshold = int(get_setting(db, "reward_threshold") or 10)

    # Calculate household hours for current year
    current_year = date.today().year
    results = (
        db.query(
            Hour.member_id,
            func.sum(Hour.hours).label("total_hours")
        )
        .filter(
            Hour.status == "approved",
            func.year(Hour.service_date) == current_year,
        )
        .group_by(Hour.member_id)
        .all()
    )

    # Aggregate by household
    hh_hours = {}
    for member_id, total in results:
        user = db.get(User, member_id)
        if not user or not user.household_id:
            continue
        hh_hours[user.household_id] = hh_hours.get(user.household_id, 0) + float(total)

    qualified  = []  # >= threshold
    close      = []  # >= threshold/2 but < threshold

    for hh_id, hours in hh_hours.items():
        hh = db.get(Household, hh_id)
        if not hh:
            continue

        # Find primary contact or first active member
        primary = db.get(User, hh.primary_user_id) if hh.primary_user_id else None
        if not primary:
            primary = db.query(User).filter(
                User.household_id == hh_id,
                User.is_active == 1,
            ).first()
        if not primary:
            continue

        entry = {
            "household_id":   hh_id,
            "household_name": hh.name,
            "primary_name":   f"{primary.firstname} {primary.lastname}",
            "primary_email":  primary.email,
            "firstname":      primary.firstname,
            "hours":          round(hours, 2),
            "remaining":      round(max(threshold - hours, 0), 2),
        }

        if hours >= threshold:
            qualified.append(entry)
        elif hours >= threshold / 2:
            close.append(entry)

    qualified.sort(key=lambda x: x["hours"], reverse=True)
    close.sort(key=lambda x: x["hours"], reverse=True)

    return {
        "threshold":            threshold,
        "reward_email_subject": get_setting(db, "reward_email_subject"),
        "reward_email_body":    get_setting(db, "reward_email_body"),
        "nudge_email_subject":  get_setting(db, "nudge_email_subject"),
        "nudge_email_body":     get_setting(db, "nudge_email_body"),
        "qualified":            qualified,
        "close":                close,
        "year":                 current_year,
    }


@router.post("/rewards")
def update_reward_settings(
    payload: SettingsUpdate,
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
):
    set_setting(db, "reward_threshold",    str(payload.reward_threshold))
    set_setting(db, "reward_email_subject", payload.reward_email_subject)
    set_setting(db, "reward_email_body",    payload.reward_email_body)
    set_setting(db, "nudge_email_subject",  payload.nudge_email_subject)
    set_setting(db, "nudge_email_body",     payload.nudge_email_body)
    return {"detail": "Settings saved"}


@router.post("/rewards/send")
def send_reward_emails(
    payload: SendRewardsRequest,
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
):
    if payload.email_type not in ("reward", "nudge"):
        raise HTTPException(status_code=400, detail="email_type must be 'reward' or 'nudge'")

    threshold = int(get_setting(db, "reward_threshold") or 10)

    if payload.email_type == "reward":
        subject_tpl = get_setting(db, "reward_email_subject")
        body_tpl    = get_setting(db, "reward_email_body")
    else:
        subject_tpl = get_setting(db, "nudge_email_subject")
        body_tpl    = get_setting(db, "nudge_email_body")

    # Re-fetch hours to get current values
    current_year = date.today().year
    results = (
        db.query(Hour.member_id, func.sum(Hour.hours).label("total_hours"))
        .filter(Hour.status == "approved", func.year(Hour.service_date) == current_year)
        .group_by(Hour.member_id)
        .all()
    )
    hh_hours = {}
    hh_primary = {}
    for member_id, total in results:
        user = db.get(User, member_id)
        if not user or not user.household_id:
            continue
        hh_hours[user.household_id] = hh_hours.get(user.household_id, 0) + float(total)

    sent = 0
    errors = []

    for hh_id in payload.household_ids:
        hh = db.get(Household, hh_id)
        if not hh:
            continue

        primary = db.get(User, hh.primary_user_id) if hh.primary_user_id else None
        if not primary:
            primary = db.query(User).filter(
                User.household_id == hh_id, User.is_active == 1
            ).first()
        if not primary or not primary.email or "placeholder.invalid" in primary.email:
            errors.append(f"No valid email for household {hh.name}")
            continue

        hours     = round(hh_hours.get(hh_id, 0), 2)
        remaining = round(max(threshold - hours, 0), 2)

        replacements = {
            "firstname": primary.firstname,
            "lastname":  primary.lastname,
            "hours":     hours,
            "remaining": remaining,
            "threshold": threshold,
            "household": hh.name,
        }

        subject = apply_template(subject_tpl, replacements)
        body    = apply_template(body_tpl,    replacements)

        try:
            send_raw_email(primary.email, subject, body)
            sent += 1
        except Exception as e:
            errors.append(f"{primary.email}: {str(e)}")

    return {
        "sent":   sent,
        "errors": errors,
        "detail": f"{sent} email{'s' if sent != 1 else ''} sent",
    }
