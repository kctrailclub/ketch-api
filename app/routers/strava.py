import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

import requests as http_requests
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import get_current_admin, get_current_user
from app.core.audit import log_action
from app.models.models import (
    StravaConnection, StravaSegment, StravaSegmentEffort,
    StravaTrail, StravaTrailSegment, User,
)

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


def _current_year_start():
    return datetime(datetime.now(timezone.utc).year, 1, 1, tzinfo=timezone.utc)


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

class CreateTrailRequest(BaseModel):
    name: str
    distance_miles: Optional[float] = None
    elevation_feet: Optional[int] = None
    year: int = 2026
    sort_order: int = 0

class UpdateTrailRequest(BaseModel):
    name: Optional[str] = None
    distance_miles: Optional[float] = None
    elevation_feet: Optional[int] = None
    sort_order: Optional[int] = None
    is_active: Optional[int] = None

class MapSegmentToTrailRequest(BaseModel):
    segment_id: int
    segment_order: int = 0

class BulkCreateTrailsRequest(BaseModel):
    year: int = 2026
    trails: list


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

    existing = db.query(StravaConnection).filter(
        StravaConnection.strava_athlete_id == athlete["id"],
        StravaConnection.user_id != current_user.user_id,
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="This Strava account is already linked to another member")

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
    conn = db.query(StravaConnection).filter(StravaConnection.user_id == current_user.user_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="No Strava connection found")

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
    _ensure_strava_configured()

    existing = db.query(StravaSegment).filter(StravaSegment.strava_segment_id == payload.strava_segment_id).first()
    if existing:
        raise HTTPException(status_code=400, detail="This segment is already featured")

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
# Trail Management (Admin)
# ---------------------------------------------------------------------------

@router.get("/trails")
def list_trails(
    year: Optional[int] = None,
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """List trails with their mapped segments."""
    if year is None:
        year = datetime.now(timezone.utc).year

    query = db.query(StravaTrail).options(
        joinedload(StravaTrail.trail_segments).joinedload(StravaTrailSegment.segment)
    ).filter(StravaTrail.year == year)

    if not include_inactive or not _user.is_admin:
        query = query.filter(StravaTrail.is_active == 1)

    trails = query.order_by(StravaTrail.sort_order, StravaTrail.name).all()

    return [
        {
            "trail_id": t.trail_id,
            "name": t.name,
            "distance_miles": float(t.distance_miles) if t.distance_miles else None,
            "elevation_feet": t.elevation_feet,
            "year": t.year,
            "sort_order": t.sort_order,
            "is_active": t.is_active,
            "segment_count": len(t.trail_segments),
            "segments": sorted([
                {
                    "segment_id": ts.segment.segment_id,
                    "strava_segment_id": ts.segment.strava_segment_id,
                    "name": ts.segment.name,
                    "activity_type": ts.segment.activity_type,
                    "segment_order": ts.segment_order,
                }
                for ts in t.trail_segments
            ], key=lambda s: s["segment_order"]),
        }
        for t in trails
    ]


@router.post("/trails", status_code=status.HTTP_201_CREATED)
def create_trail(
    payload: CreateTrailRequest,
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
):
    trail = StravaTrail(
        name=payload.name,
        distance_miles=payload.distance_miles,
        elevation_feet=payload.elevation_feet,
        year=payload.year,
        sort_order=payload.sort_order,
        created_by=_admin.user_id,
    )
    db.add(trail)
    log_action(db, user_id=_admin.user_id, action="create", entity_type="strava_trail",
               entity_id=0, details={"summary": f"Created trail: {trail.name}"})
    db.commit()
    db.refresh(trail)
    return {"trail_id": trail.trail_id, "name": trail.name, "detail": f"Created trail: {trail.name}"}


@router.post("/trails/bulk", status_code=status.HTTP_201_CREATED)
def bulk_create_trails(
    payload: BulkCreateTrailsRequest,
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
):
    """Bulk-create trails from a list. Each item needs: name, distance_miles, elevation_feet."""
    created = 0
    for idx, item in enumerate(payload.trails):
        name = item.get("name", "").strip()
        if not name:
            continue
        # Skip if trail with same name+year already exists
        exists = db.query(StravaTrail).filter(
            StravaTrail.name == name, StravaTrail.year == payload.year
        ).first()
        if exists:
            continue
        trail = StravaTrail(
            name=name,
            distance_miles=item.get("distance_miles"),
            elevation_feet=item.get("elevation_feet"),
            year=payload.year,
            sort_order=idx,
            created_by=_admin.user_id,
        )
        db.add(trail)
        created += 1

    log_action(db, user_id=_admin.user_id, action="bulk_create", entity_type="strava_trail",
               entity_id=0, details={"summary": f"Bulk-created {created} trail(s) for {payload.year}"})
    db.commit()
    return {"created": created, "detail": f"Created {created} trail(s)"}


@router.patch("/trails/{trail_id}")
def update_trail(
    trail_id: int,
    payload: UpdateTrailRequest,
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
):
    trail = db.get(StravaTrail, trail_id)
    if not trail:
        raise HTTPException(status_code=404, detail="Trail not found")

    if payload.name is not None:
        trail.name = payload.name
    if payload.distance_miles is not None:
        trail.distance_miles = payload.distance_miles
    if payload.elevation_feet is not None:
        trail.elevation_feet = payload.elevation_feet
    if payload.sort_order is not None:
        trail.sort_order = payload.sort_order
    if payload.is_active is not None:
        trail.is_active = payload.is_active

    log_action(db, user_id=_admin.user_id, action="update", entity_type="strava_trail",
               entity_id=trail_id, details={"summary": f"Updated trail: {trail.name}"})
    db.commit()
    return {"detail": "Trail updated"}


@router.delete("/trails/{trail_id}")
def delete_trail(
    trail_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
):
    trail = db.get(StravaTrail, trail_id)
    if not trail:
        raise HTTPException(status_code=404, detail="Trail not found")

    name = trail.name
    db.delete(trail)
    log_action(db, user_id=_admin.user_id, action="delete", entity_type="strava_trail",
               entity_id=trail_id, details={"summary": f"Deleted trail: {name}"})
    db.commit()
    return {"detail": "Trail deleted"}


# ---------------------------------------------------------------------------
# Trail ↔ Segment Mapping (Admin)
# ---------------------------------------------------------------------------

@router.post("/trails/{trail_id}/segments")
def add_segment_to_trail(
    trail_id: int,
    payload: MapSegmentToTrailRequest,
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
):
    trail = db.get(StravaTrail, trail_id)
    if not trail:
        raise HTTPException(status_code=404, detail="Trail not found")

    segment = db.get(StravaSegment, payload.segment_id)
    if not segment:
        raise HTTPException(status_code=404, detail="Segment not found")

    exists = db.query(StravaTrailSegment).filter(
        StravaTrailSegment.trail_id == trail_id,
        StravaTrailSegment.segment_id == payload.segment_id,
    ).first()
    if exists:
        raise HTTPException(status_code=400, detail="Segment already mapped to this trail")

    mapping = StravaTrailSegment(
        trail_id=trail_id,
        segment_id=payload.segment_id,
        segment_order=payload.segment_order,
    )
    db.add(mapping)
    log_action(db, user_id=_admin.user_id, action="map_segment", entity_type="strava_trail",
               entity_id=trail_id,
               details={"summary": f"Mapped segment '{segment.name}' to trail '{trail.name}'"})
    db.commit()
    return {"detail": f"Added '{segment.name}' to '{trail.name}'"}


@router.delete("/trails/{trail_id}/segments/{segment_id}")
def remove_segment_from_trail(
    trail_id: int,
    segment_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
):
    mapping = db.query(StravaTrailSegment).filter(
        StravaTrailSegment.trail_id == trail_id,
        StravaTrailSegment.segment_id == segment_id,
    ).first()
    if not mapping:
        raise HTTPException(status_code=404, detail="Mapping not found")

    db.delete(mapping)
    db.commit()
    return {"detail": "Segment removed from trail"}


# ---------------------------------------------------------------------------
# Sync (Member)
# ---------------------------------------------------------------------------

@router.post("/sync")
def sync_efforts(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Sync efforts from Strava activities (free tier API)."""
    _ensure_strava_configured()

    conn = db.query(StravaConnection).filter(StravaConnection.user_id == current_user.user_id).first()
    if not conn:
        raise HTTPException(status_code=400, detail="Connect your Strava account first")

    token = _refresh_token_if_needed(db, conn)

    segments = db.query(StravaSegment).filter(StravaSegment.is_active == 1).all()
    if not segments:
        return {"new_efforts": 0, "detail": "No featured segments to sync"}

    seg_lookup = {s.strava_segment_id: s for s in segments}

    new_count = 0
    activities_checked = 0
    page = 1

    year_start = _current_year_start()
    after_ts = int(year_start.timestamp())

    while page <= 10:
        activities = _strava_get(token, "/athlete/activities", params={
            "per_page": 50,
            "page": page,
            "after": after_ts,
        })

        if not activities:
            break

        for activity_summary in activities:
            activity_id = activity_summary["id"]
            activity_type = activity_summary.get("type")  # Run, Ride, Hike, etc.
            activities_checked += 1

            try:
                activity_detail = _strava_get(token, f"/activities/{activity_id}")
            except Exception as exc:
                log.error("Error fetching activity %s: %s", activity_id, exc)
                continue

            if not activity_detail:
                continue

            for effort in activity_detail.get("segment_efforts", []):
                seg_strava_id = effort.get("segment", {}).get("id")
                if seg_strava_id not in seg_lookup:
                    continue

                strava_effort_id = effort["id"]
                exists = db.query(StravaSegmentEffort).filter(
                    StravaSegmentEffort.strava_effort_id == strava_effort_id
                ).first()
                if exists:
                    continue

                db.add(StravaSegmentEffort(
                    connection_id=conn.connection_id,
                    segment_id=seg_lookup[seg_strava_id].segment_id,
                    strava_effort_id=strava_effort_id,
                    activity_id=activity_id,
                    activity_type=activity_type,
                    elapsed_time=effort["elapsed_time"],
                    moving_time=effort["moving_time"],
                    start_date=datetime.fromisoformat(effort["start_date"].replace("Z", "+00:00")),
                ))
                new_count += 1

        if len(activities) < 50:
            break
        page += 1

    if new_count > 0:
        log_action(db, user_id=current_user.user_id, action="strava_sync", entity_type="strava",
                   entity_id=current_user.user_id,
                   details={"summary": f"Synced {new_count} new effort(s) from {activities_checked} activities"})
    db.commit()

    return {"new_efforts": new_count, "detail": f"Synced {new_count} new effort(s) from {activities_checked} activities"}


# ---------------------------------------------------------------------------
# Trails Challenge (Member)
# ---------------------------------------------------------------------------

@router.get("/trails-challenge")
def get_trails_challenge(
    year: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Main member endpoint: all trails with completion status."""
    if year is None:
        year = datetime.now(timezone.utc).year

    year_start = datetime(year, 1, 1, tzinfo=timezone.utc)
    year_end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)

    # Get all active trails with their segments
    trails = (
        db.query(StravaTrail)
        .options(joinedload(StravaTrail.trail_segments).joinedload(StravaTrailSegment.segment))
        .filter(StravaTrail.year == year, StravaTrail.is_active == 1)
        .order_by(StravaTrail.sort_order, StravaTrail.name)
        .all()
    )

    # Get user's connection and efforts
    conn = db.query(StravaConnection).filter(StravaConnection.user_id == current_user.user_id).first()

    # Build a dict: segment_id -> {best_time, effort_count, activity_type}
    user_efforts = {}
    if conn:
        # Get best effort per segment for this user this year
        effort_rows = (
            db.query(
                StravaSegmentEffort.segment_id,
                func.min(StravaSegmentEffort.elapsed_time).label("best_time"),
                func.count(StravaSegmentEffort.effort_id).label("effort_count"),
            )
            .filter(
                StravaSegmentEffort.connection_id == conn.connection_id,
                StravaSegmentEffort.start_date >= year_start,
                StravaSegmentEffort.start_date < year_end,
            )
            .group_by(StravaSegmentEffort.segment_id)
            .all()
        )
        for row in effort_rows:
            user_efforts[row.segment_id] = {
                "best_time": row.best_time,
                "effort_count": row.effort_count,
            }

        # Get most recent activity_type per segment
        for seg_id in user_efforts:
            latest = (
                db.query(StravaSegmentEffort.activity_type)
                .filter(
                    StravaSegmentEffort.connection_id == conn.connection_id,
                    StravaSegmentEffort.segment_id == seg_id,
                    StravaSegmentEffort.start_date >= year_start,
                    StravaSegmentEffort.start_date < year_end,
                )
                .order_by(StravaSegmentEffort.start_date.desc())
                .first()
            )
            user_efforts[seg_id]["activity_type"] = latest[0] if latest else None

    # Build response
    completed_count = 0
    trail_list = []

    for t in trails:
        segs = sorted(t.trail_segments, key=lambda ts: ts.segment_order)
        seg_list = []
        segments_completed = 0

        for ts in segs:
            seg = ts.segment
            effort_data = user_efforts.get(seg.segment_id, {})
            has_effort = seg.segment_id in user_efforts

            if has_effort:
                segments_completed += 1

            seg_list.append({
                "segment_id": seg.segment_id,
                "strava_segment_id": seg.strava_segment_id,
                "name": seg.name,
                "segment_order": ts.segment_order,
                "has_effort": has_effort,
                "best_time": effort_data.get("best_time"),
                "best_time_formatted": _format_time(effort_data["best_time"]) if effort_data.get("best_time") else None,
                "activity_type": effort_data.get("activity_type"),
                "effort_count": effort_data.get("effort_count", 0),
            })

        segments_total = len(segs)
        is_completed = segments_total > 0 and segments_completed == segments_total

        if is_completed:
            completed_count += 1

        trail_list.append({
            "trail_id": t.trail_id,
            "name": t.name,
            "distance_miles": float(t.distance_miles) if t.distance_miles else None,
            "elevation_feet": t.elevation_feet,
            "is_completed": is_completed,
            "segments_completed": segments_completed,
            "segments_total": segments_total,
            "segments": seg_list,
        })

    return {
        "year": year,
        "total_trails": len(trails),
        "completed_trails": completed_count,
        "trails": trail_list,
    }


@router.get("/trails-challenge/leaderboard")
def get_trails_challenge_leaderboard(
    year: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Leaderboard: ranked by trails completed, tiebreaker by total best time."""
    if year is None:
        year = datetime.now(timezone.utc).year

    year_start = datetime(year, 1, 1, tzinfo=timezone.utc)
    year_end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)

    # Get all active trails and their required segment IDs
    trails = (
        db.query(StravaTrail)
        .options(joinedload(StravaTrail.trail_segments))
        .filter(StravaTrail.year == year, StravaTrail.is_active == 1)
        .all()
    )

    # Build: trail_id -> set of required segment_ids
    trail_requirements = {}
    for t in trails:
        seg_ids = {ts.segment_id for ts in t.trail_segments}
        if seg_ids:  # Skip trails with no segments
            trail_requirements[t.trail_id] = seg_ids

    total_trails = len(trail_requirements)
    if total_trails == 0:
        return []

    # Get all connections with at least one effort this year
    connections = (
        db.query(StravaConnection)
        .join(StravaSegmentEffort)
        .filter(
            StravaSegmentEffort.start_date >= year_start,
            StravaSegmentEffort.start_date < year_end,
        )
        .distinct()
        .all()
    )

    leaderboard = []

    for conn in connections:
        # Get this user's completed segment IDs and best times
        effort_rows = (
            db.query(
                StravaSegmentEffort.segment_id,
                func.min(StravaSegmentEffort.elapsed_time).label("best_time"),
            )
            .filter(
                StravaSegmentEffort.connection_id == conn.connection_id,
                StravaSegmentEffort.start_date >= year_start,
                StravaSegmentEffort.start_date < year_end,
            )
            .group_by(StravaSegmentEffort.segment_id)
            .all()
        )

        completed_segs = {r.segment_id for r in effort_rows}
        best_times = {r.segment_id: r.best_time for r in effort_rows}

        # Count completed trails
        trails_completed = 0
        total_best_time = 0

        for trail_id, required_segs in trail_requirements.items():
            if required_segs.issubset(completed_segs):
                trails_completed += 1
                total_best_time += sum(best_times.get(sid, 0) for sid in required_segs)

        if trails_completed == 0:
            continue

        user = db.get(User, conn.user_id)
        if not user:
            continue

        leaderboard.append({
            "user_id": user.user_id,
            "name": f"{user.firstname} {user.lastname}",
            "trails_completed": trails_completed,
            "total_trails": total_trails,
            "total_best_time": total_best_time,
            "total_best_time_formatted": _format_time(total_best_time),
            "is_current_user": user.user_id == current_user.user_id,
        })

    # Sort: most trails completed, then lowest total time as tiebreaker
    leaderboard.sort(key=lambda x: (-x["trails_completed"], x["total_best_time"]))

    for rank, entry in enumerate(leaderboard, 1):
        entry["rank"] = rank

    return leaderboard


# ---------------------------------------------------------------------------
# Segment-level endpoints (Member)
# ---------------------------------------------------------------------------

@router.get("/segments/{segment_id}/leaderboard")
def get_segment_leaderboard(
    segment_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    from sqlalchemy import and_

    segment = db.get(StravaSegment, segment_id)
    if not segment:
        raise HTTPException(status_code=404, detail="Segment not found")

    year_start = _current_year_start()

    subq = (
        db.query(
            StravaSegmentEffort.connection_id,
            func.min(StravaSegmentEffort.elapsed_time).label("best_time"),
        )
        .filter(
            StravaSegmentEffort.segment_id == segment_id,
            StravaSegmentEffort.start_date >= year_start,
        )
        .group_by(StravaSegmentEffort.connection_id)
        .subquery()
    )

    results = (
        db.query(StravaSegmentEffort, StravaConnection, User)
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
            "activity_type": effort.activity_type,
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
    conn = db.query(StravaConnection).filter(StravaConnection.user_id == current_user.user_id).first()
    if not conn:
        return []

    year_start = _current_year_start()

    efforts = (
        db.query(StravaSegmentEffort)
        .filter(
            StravaSegmentEffort.connection_id == conn.connection_id,
            StravaSegmentEffort.segment_id == segment_id,
            StravaSegmentEffort.start_date >= year_start,
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
            "activity_type": e.activity_type,
            "start_date": e.start_date.isoformat() if e.start_date else None,
            "is_pr": e.elapsed_time == best_time,
        }
        for e in efforts
    ]
