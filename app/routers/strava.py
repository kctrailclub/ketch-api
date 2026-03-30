import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

import requests as http_requests
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import get_current_admin, get_current_user
from app.core.audit import log_action
from app.models.models import StravaConnection, StravaSegment, StravaSegmentEffort, User

log = logging.getLogger(__name__)

router = APIRouter(prefix="/strava", tags=["strava"])

STRAVA_AUTH_URL = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_DEAUTH_URL = "https://www.strava.com/oauth/deauthorize"
STRAVA_API_BASE = "https://www.strava.com/api/v3"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_strava_configured():
    if not settings.strava_client_id or not settings.strava_client_secret:
        raise HTTPException(status_code=501, detail="Strava integration is not configured")


def _refresh_token_if_needed(db: Session, conn: StravaConnection) -> str:
    """Return a valid Strava access token, refreshing if expired."""
    now = datetime.now(timezone.utc)
    expires = conn.token_expires_at.replace(tzinfo=timezone.utc) if conn.token_expires_at.tzinfo is None else conn.token_expires_at

    if now < expires - timedelta(minutes=5):
        return conn.access_token

    resp = http_requests.post(STRAVA_TOKEN_URL, data={
        "client_id": settings.strava_client_id,
        "client_secret": settings.strava_client_secret,
        "grant_type": "refresh_token",
        "refresh_token": conn.refresh_token,
    }, timeout=15)

    if resp.status_code != 200:
        log.error("Strava token refresh failed: %s", resp.text)
        raise HTTPException(status_code=502, detail="Failed to refresh Strava token. Try reconnecting.")

    data = resp.json()
    conn.access_token = data["access_token"]
    conn.refresh_token = data["refresh_token"]
    conn.token_expires_at = datetime.fromtimestamp(data["expires_at"], tz=timezone.utc)
    db.commit()
    return conn.access_token


def _strava_get(token: str, path: str, params: dict = None):
    """Make an authenticated GET to the Strava API."""
    resp = http_requests.get(
        f"{STRAVA_API_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params or {},
        timeout=15,
    )
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        log.error("Strava API error %s %s: %s", resp.status_code, path, resp.text[:500])
        raise HTTPException(status_code=502, detail=f"Strava API error ({resp.status_code})")
    return resp.json()


def _format_time(seconds: int) -> str:
    """Format seconds as M:SS or H:MM:SS."""
    if seconds < 3600:
        return f"{seconds // 60}:{seconds % 60:02d}"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h}:{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class StravaCallbackRequest(BaseModel):
    code: str

class AddSegmentRequest(BaseModel):
    strava_segment_id: int
    sort_order: int = 0

class UpdateSegmentRequest(BaseModel):
    name: Optional[str] = None
    sort_order: Optional[int] = None
    is_active: Optional[int] = None


# ---------------------------------------------------------------------------
# OAuth Endpoints
# ---------------------------------------------------------------------------

@router.get("/auth-url")
def get_auth_url(current_user: User = Depends(get_current_user)):
    """Return the Strava OAuth authorization URL."""
    _ensure_strava_configured()

    params = {
        "client_id": settings.strava_client_id,
        "response_type": "code",
        "redirect_uri": f"{settings.frontend_url}/resources?strava_callback=1",
        "scope": "read,activity:read",
        "approval_prompt": "auto",
    }
    return {"url": f"{STRAVA_AUTH_URL}?{urlencode(params)}"}


@router.post("/callback")
def strava_callback(
    payload: StravaCallbackRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Exchange the Strava authorization code for tokens and store the connection."""
    _ensure_strava_configured()

    # Exchange code for tokens
    resp = http_requests.post(STRAVA_TOKEN_URL, data={
        "client_id": settings.strava_client_id,
        "client_secret": settings.strava_client_secret,
        "code": payload.code,
        "grant_type": "authorization_code",
    }, timeout=15)

    if resp.status_code != 200:
        log.error("Strava token exchange failed: %s", resp.text)
        raise HTTPException(status_code=400, detail="Failed to connect to Strava. Please try again.")

    data = resp.json()
    athlete = data.get("athlete", {})

    # Check if this Strava account is already linked to another user
    existing = db.query(StravaConnection).filter(
        StravaConnection.strava_athlete_id == athlete["id"],
        StravaConnection.user_id != current_user.user_id,
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="This Strava account is already linked to another member")

    # Upsert connection
    conn = db.query(StravaConnection).filter(StravaConnection.user_id == current_user.user_id).first()
    if conn:
        conn.strava_athlete_id = athlete["id"]
        conn.access_token = data["access_token"]
        conn.refresh_token = data["refresh_token"]
        conn.token_expires_at = datetime.fromtimestamp(data["expires_at"], tz=timezone.utc)
        conn.athlete_firstname = athlete.get("firstname")
        conn.athlete_lastname = athlete.get("lastname")
    else:
        conn = StravaConnection(
            user_id=current_user.user_id,
            strava_athlete_id=athlete["id"],
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            token_expires_at=datetime.fromtimestamp(data["expires_at"], tz=timezone.utc),
            athlete_firstname=athlete.get("firstname"),
            athlete_lastname=athlete.get("lastname"),
        )
        db.add(conn)

    log_action(db, user_id=current_user.user_id, action="strava_connect", entity_type="strava",
               entity_id=current_user.user_id,
               details={"summary": f"{current_user.firstname} {current_user.lastname} connected Strava"})
    db.commit()

    return {
        "detail": "Strava connected",
        "athlete_name": f"{athlete.get('firstname', '')} {athlete.get('lastname', '')}".strip(),
    }


@router.get("/connection")
def get_connection(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Check if the current user has a Strava connection."""
    conn = db.query(StravaConnection).filter(StravaConnection.user_id == current_user.user_id).first()
    if not conn:
        return {"connected": False}
    return {
        "connected": True,
        "athlete_name": f"{conn.athlete_firstname or ''} {conn.athlete_lastname or ''}".strip(),
        "strava_athlete_id": conn.strava_athlete_id,
    }


@router.delete("/connection")
def disconnect_strava(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Disconnect the user's Strava account."""
    conn = db.query(StravaConnection).filter(StravaConnection.user_id == current_user.user_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="No Strava connection found")

    # Attempt to deauthorize on Strava's side (best effort)
    try:
        http_requests.post(STRAVA_DEAUTH_URL, data={"access_token": conn.access_token}, timeout=10)
    except Exception:
        pass

    db.delete(conn)
    log_action(db, user_id=current_user.user_id, action="strava_disconnect", entity_type="strava",
               entity_id=current_user.user_id,
               details={"summary": f"{current_user.firstname} {current_user.lastname} disconnected Strava"})
    db.commit()

    return {"detail": "Strava disconnected"}


# ---------------------------------------------------------------------------
# Segment Management (Admin)
# ---------------------------------------------------------------------------

@router.get("/segments")
def list_segments(
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """List featured segments. Members see active only; admin can include inactive."""
    query = db.query(StravaSegment)
    if not include_inactive or not _user.is_admin:
        query = query.filter(StravaSegment.is_active == 1)
    segments = query.order_by(StravaSegment.sort_order, StravaSegment.name).all()

    return [
        {
            "segment_id": s.segment_id,
            "strava_segment_id": s.strava_segment_id,
            "name": s.name,
            "activity_type": s.activity_type,
            "distance": float(s.distance) if s.distance else None,
            "average_grade": float(s.average_grade) if s.average_grade else None,
            "elevation_high": float(s.elevation_high) if s.elevation_high else None,
            "elevation_low": float(s.elevation_low) if s.elevation_low else None,
            "sort_order": s.sort_order,
            "is_active": s.is_active,
        }
        for s in segments
    ]


@router.post("/segments", status_code=status.HTTP_201_CREATED)
def add_segment(
    payload: AddSegmentRequest,
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
):
    """Add a featured segment by Strava segment ID. Fetches metadata from Strava."""
    _ensure_strava_configured()

    # Check for duplicate
    existing = db.query(StravaSegment).filter(StravaSegment.strava_segment_id == payload.strava_segment_id).first()
    if existing:
        raise HTTPException(status_code=400, detail="This segment is already featured")

    # Need admin's Strava connection to fetch segment data
    conn = db.query(StravaConnection).filter(StravaConnection.user_id == _admin.user_id).first()
    if not conn:
        raise HTTPException(status_code=400, detail="Connect your Strava account first to add segments")

    token = _refresh_token_if_needed(db, conn)
    seg_data = _strava_get(token, f"/segments/{payload.strava_segment_id}")

    if not seg_data:
        raise HTTPException(status_code=404, detail="Segment not found on Strava. Check the ID and try again.")

    segment = StravaSegment(
        strava_segment_id=payload.strava_segment_id,
        name=seg_data.get("name", f"Segment {payload.strava_segment_id}"),
        activity_type=seg_data.get("activity_type", "Ride"),
        distance=seg_data.get("distance"),
        average_grade=seg_data.get("average_grade"),
        elevation_high=seg_data.get("elevation_high"),
        elevation_low=seg_data.get("elevation_low"),
        sort_order=payload.sort_order,
        created_by=_admin.user_id,
    )
    db.add(segment)
    log_action(db, user_id=_admin.user_id, action="create", entity_type="strava_segment",
               entity_id=payload.strava_segment_id,
               details={"summary": f"Added featured segment: {segment.name}"})
    db.commit()
    db.refresh(segment)

    return {
        "segment_id": segment.segment_id,
        "name": segment.name,
        "activity_type": segment.activity_type,
        "distance": float(segment.distance) if segment.distance else None,
        "average_grade": float(segment.average_grade) if segment.average_grade else None,
        "detail": f"Added segment: {segment.name}",
    }


@router.patch("/segments/{segment_id}")
def update_segment(
    segment_id: int,
    payload: UpdateSegmentRequest,
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
):
    """Update a featured segment (name override, sort order, active status)."""
    segment = db.get(StravaSegment, segment_id)
    if not segment:
        raise HTTPException(status_code=404, detail="Segment not found")

    if payload.name is not None:
        segment.name = payload.name
    if payload.sort_order is not None:
        segment.sort_order = payload.sort_order
    if payload.is_active is not None:
        segment.is_active = payload.is_active

    log_action(db, user_id=_admin.user_id, action="update", entity_type="strava_segment",
               entity_id=segment_id,
               details={"summary": f"Updated segment: {segment.name}"})
    db.commit()
    return {"detail": "Segment updated"}


@router.delete("/segments/{segment_id}")
def delete_segment(
    segment_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
):
    """Delete a featured segment and all associated efforts."""
    segment = db.get(StravaSegment, segment_id)
    if not segment:
        raise HTTPException(status_code=404, detail="Segment not found")

    name = segment.name
    db.delete(segment)
    log_action(db, user_id=_admin.user_id, action="delete", entity_type="strava_segment",
               entity_id=segment_id,
               details={"summary": f"Deleted segment: {name}"})
    db.commit()
    return {"detail": "Segment deleted"}


@router.post("/segments/{segment_id}/refresh")
def refresh_segment(
    segment_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
):
    """Re-fetch segment metadata from Strava."""
    _ensure_strava_configured()

    segment = db.get(StravaSegment, segment_id)
    if not segment:
        raise HTTPException(status_code=404, detail="Segment not found")

    conn = db.query(StravaConnection).filter(StravaConnection.user_id == _admin.user_id).first()
    if not conn:
        raise HTTPException(status_code=400, detail="Connect your Strava account first")

    token = _refresh_token_if_needed(db, conn)
    seg_data = _strava_get(token, f"/segments/{segment.strava_segment_id}")

    if not seg_data:
        raise HTTPException(status_code=404, detail="Segment no longer found on Strava")

    segment.name = seg_data.get("name", segment.name)
    segment.activity_type = seg_data.get("activity_type", segment.activity_type)
    segment.distance = seg_data.get("distance")
    segment.average_grade = seg_data.get("average_grade")
    segment.elevation_high = seg_data.get("elevation_high")
    segment.elevation_low = seg_data.get("elevation_low")

    db.commit()
    return {"detail": f"Refreshed segment: {segment.name}"}


# ---------------------------------------------------------------------------
# Sync & Leaderboard (Member)
# ---------------------------------------------------------------------------

@router.post("/sync")
def sync_efforts(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Sync the current user's efforts on all featured segments from Strava."""
    _ensure_strava_configured()

    conn = db.query(StravaConnection).filter(StravaConnection.user_id == current_user.user_id).first()
    if not conn:
        raise HTTPException(status_code=400, detail="Connect your Strava account first")

    token = _refresh_token_if_needed(db, conn)

    # Get all active featured segments
    segments = db.query(StravaSegment).filter(StravaSegment.is_active == 1).all()
    if not segments:
        return {"new_efforts": 0, "detail": "No featured segments to sync"}

    new_count = 0
    errors = 0

    for seg in segments:
        try:
            # Fetch this user's efforts on this segment
            efforts_data = _strava_get(token, "/segment_efforts", params={
                "segment_id": seg.strava_segment_id,
                "per_page": 100,
            })

            if not efforts_data:
                continue

            for effort in efforts_data:
                strava_effort_id = effort["id"]
                # Skip if already cached
                exists = db.query(StravaSegmentEffort).filter(
                    StravaSegmentEffort.strava_effort_id == strava_effort_id
                ).first()
                if exists:
                    continue

                db.add(StravaSegmentEffort(
                    connection_id=conn.connection_id,
                    segment_id=seg.segment_id,
                    strava_effort_id=strava_effort_id,
                    activity_id=effort["activity"]["id"],
                    elapsed_time=effort["elapsed_time"],
                    moving_time=effort["moving_time"],
                    start_date=datetime.fromisoformat(effort["start_date"].replace("Z", "+00:00")),
                ))
                new_count += 1

        except HTTPException:
            errors += 1
        except Exception as exc:
            log.error("Error syncing segment %s: %s", seg.strava_segment_id, exc)
            errors += 1

    if new_count > 0:
        log_action(db, user_id=current_user.user_id, action="strava_sync", entity_type="strava",
                   entity_id=current_user.user_id,
                   details={"summary": f"Synced {new_count} new effort(s) from Strava"})
    db.commit()

    msg = f"Synced {new_count} new effort(s)"
    if errors:
        msg += f" ({errors} segment(s) had errors)"
    return {"new_efforts": new_count, "detail": msg}


@router.get("/segments/{segment_id}/leaderboard")
def get_leaderboard(
    segment_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Get the leaderboard (best time per member) for a segment."""
    segment = db.get(StravaSegment, segment_id)
    if not segment:
        raise HTTPException(status_code=404, detail="Segment not found")

    # Best elapsed_time per connection_id
    from sqlalchemy import and_

    subq = (
        db.query(
            StravaSegmentEffort.connection_id,
            func.min(StravaSegmentEffort.elapsed_time).label("best_time"),
        )
        .filter(StravaSegmentEffort.segment_id == segment_id)
        .group_by(StravaSegmentEffort.connection_id)
        .subquery()
    )

    # Join to get the actual effort row (for start_date) and user info
    results = (
        db.query(
            StravaSegmentEffort,
            StravaConnection,
            User,
        )
        .join(subq, and_(
            StravaSegmentEffort.connection_id == subq.c.connection_id,
            StravaSegmentEffort.elapsed_time == subq.c.best_time,
        ))
        .filter(StravaSegmentEffort.segment_id == segment_id)
        .join(StravaConnection, StravaSegmentEffort.connection_id == StravaConnection.connection_id)
        .join(User, StravaConnection.user_id == User.user_id)
        .order_by(StravaSegmentEffort.elapsed_time)
        .all()
    )

    leaderboard = []
    for rank, (effort, conn, user) in enumerate(results, 1):
        leaderboard.append({
            "rank": rank,
            "user_id": user.user_id,
            "name": f"{user.firstname} {user.lastname}",
            "elapsed_time": effort.elapsed_time,
            "elapsed_time_formatted": _format_time(effort.elapsed_time),
            "start_date": effort.start_date.isoformat() if effort.start_date else None,
            "is_current_user": user.user_id == _user.user_id,
        })

    return leaderboard


@router.get("/segments/{segment_id}/my-efforts")
def get_my_efforts(
    segment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get the current user's efforts on a specific segment."""
    conn = db.query(StravaConnection).filter(StravaConnection.user_id == current_user.user_id).first()
    if not conn:
        return []

    efforts = (
        db.query(StravaSegmentEffort)
        .filter(
            StravaSegmentEffort.connection_id == conn.connection_id,
            StravaSegmentEffort.segment_id == segment_id,
        )
        .order_by(StravaSegmentEffort.elapsed_time)
        .all()
    )

    best_time = efforts[0].elapsed_time if efforts else None

    return [
        {
            "effort_id": e.effort_id,
            "elapsed_time": e.elapsed_time,
            "elapsed_time_formatted": _format_time(e.elapsed_time),
            "moving_time": e.moving_time,
            "moving_time_formatted": _format_time(e.moving_time),
            "start_date": e.start_date.isoformat() if e.start_date else None,
            "is_pr": e.elapsed_time == best_time,
        }
        for e in efforts
    ]


@router.get("/my-stats")
def get_my_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get aggregate Strava stats for the current user."""
    conn = db.query(StravaConnection).filter(StravaConnection.user_id == current_user.user_id).first()
    if not conn:
        return {"connected": False}

    total_efforts = db.query(StravaSegmentEffort).filter(
        StravaSegmentEffort.connection_id == conn.connection_id
    ).count()

    segments_with_efforts = (
        db.query(StravaSegmentEffort.segment_id)
        .filter(StravaSegmentEffort.connection_id == conn.connection_id)
        .distinct()
        .count()
    )

    total_segments = db.query(StravaSegment).filter(StravaSegment.is_active == 1).count()

    return {
        "connected": True,
        "total_efforts": total_efforts,
        "segments_completed": segments_with_efforts,
        "total_segments": total_segments,
    }
