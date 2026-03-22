import html

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.audit import log_action
from app.core.database import get_db
from app.core.dependencies import get_current_admin, get_current_user
from app.core.email import send_raw_email
from app.models.models import Hour, Project, User, Household, Setting, RewardEmail
from sqlalchemy import func, desc
from datetime import date

router = APIRouter(prefix="/settings", tags=["settings"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_setting(db: Session, key: str) -> str:
    row = db.query(Setting).filter(Setting.key == key).first()
    return row.value if row else None


def set_setting(db: Session, key: str, value: str):
    row = db.query(Setting).filter(Setting.key == key).first()
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))
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

@router.get("/rewards/threshold")
def get_reward_threshold(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    threshold = int(get_setting(db, "reward_threshold") or 10)
    return {"threshold": threshold}


@router.get("/rewards")
def get_reward_settings(
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
):
    threshold = int(get_setting(db, "reward_threshold") or 10)

    # Calculate household credited hours for current year
    current_year = date.today().year
    hour_records = (
        db.query(Hour)
        .filter(
            Hour.status == "approved",
            Hour.credit_year == current_year,
        )
        .all()
    )

    # Aggregate by household, applying youth credit per-project
    hh_hours = {}
    hh_has_youth = {}
    for h in hour_records:
        user = h.member
        if not user or not user.household_id:
            continue
        hid = user.household_id
        raw = float(h.hours)
        if user.youth:
            credited = raw * (h.project.youth_credit_pct / 100)
            hh_has_youth[hid] = True
        else:
            credited = raw
        hh_hours[hid] = hh_hours.get(hid, 0) + credited

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
            "has_youth_hours": hh_has_youth.get(hh_id, False),
        }

        if hours >= threshold:
            qualified.append(entry)
        elif hours >= threshold / 2:
            close.append(entry)

    qualified.sort(key=lambda x: x["hours"], reverse=True)
    close.sort(key=lambda x: x["hours"], reverse=True)

    # Fetch most-recent reward email per household+type for current year
    recent_sends = (
        db.query(RewardEmail)
        .filter(RewardEmail.year == current_year)
        .order_by(RewardEmail.household_id, RewardEmail.email_type, desc(RewardEmail.sent_at))
        .all()
    )
    send_map = {}
    for s in recent_sends:
        key = (s.household_id, s.email_type)
        if key not in send_map:
            sender_name = None
            if s.sent_by:
                sender = db.get(User, s.sent_by)
                if sender:
                    sender_name = f"{sender.firstname} {sender.lastname}"
            send_map[key] = {
                "sent_by_name": sender_name,
                "sent_at": s.sent_at.isoformat() if s.sent_at else None,
            }

    for entry in qualified:
        entry["last_sent"] = send_map.get((entry["household_id"], "reward"))
    for entry in close:
        entry["last_sent"] = send_map.get((entry["household_id"], "nudge"))

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
    log_action(db, user_id=_admin.user_id, action="update", entity_type="settings",
        details={"summary": f"Updated reward settings (threshold: {payload.reward_threshold})"})
    db.commit()
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

    # Re-fetch hours to get current credited values
    current_year = date.today().year
    hour_records = (
        db.query(Hour)
        .filter(Hour.status == "approved", Hour.credit_year == current_year)
        .all()
    )
    hh_hours = {}
    for h in hour_records:
        user = h.member
        if not user or not user.household_id:
            continue
        raw = float(h.hours)
        credited = raw * (h.project.youth_credit_pct / 100) if user.youth else raw
        hh_hours[user.household_id] = hh_hours.get(user.household_id, 0) + credited

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
            db.add(RewardEmail(
                household_id=hh_id,
                email_type=payload.email_type,
                year=current_year,
                sent_by=_admin.user_id,
            ))
        except Exception as e:
            errors.append(f"{primary.email}: {str(e)}")

    log_action(db, user_id=_admin.user_id, action="send_emails", entity_type="settings",
        details={"summary": f"Sent {sent} {payload.email_type} email{'s' if sent != 1 else ''}", "type": payload.email_type, "count": sent})
    db.commit()
    return {
        "sent":   sent,
        "errors": errors,
        "detail": f"{sent} email{'s' if sent != 1 else ''} sent",
    }
