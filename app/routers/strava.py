import json
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from math import cos, radians
from typing import Optional
from urllib.parse import urlencode

import polyline as polyline_codec
import requests as http_requests
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel
from shapely.geometry import LineString
from shapely.ops import unary_union
from sqlalchemy.orm import Session

from app.core.audit import log_action
from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import get_current_admin, get_current_user
from app.models.models import (
    Setting, StravaConnection, StravaTrail, TrailCompletion, User,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/strava", tags=["strava"])

STRAVA_AUTH_URL  = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_DEAUTH_URL = "https://www.strava.com/oauth/deauthorize"
STRAVA_API_BASE  = "https://www.strava.com/api/v3"

# ---------------------------------------------------------------------------
# Equirectangular projection constants (Ken-Caryl area, anchored at 39.5°N)
# ---------------------------------------------------------------------------
_LAT_0 = 39.5
_M_PER_DEG_LAT = 110_540.0
_M_PER_DEG_LON = 111_320.0 * cos(radians(_LAT_0))   # ≈ 85 918 m/deg at 39.5°N


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _proj(lat: float, lon: float):
    """Convert (lat, lon) to approximate (x, y) in metres."""
    return (lon - 0.0) * _M_PER_DEG_LON, (lat - _LAT_0) * _M_PER_DEG_LAT


def _build_line(coords) -> Optional[LineString]:
    """Build a Shapely LineString from a list of (lat, lon) pairs.
    Returns None if fewer than 2 unique points after deduplication."""
    pts = []
    prev = None
    for lat, lon in coords:
        xy = _proj(lat, lon)
        if xy != prev:
            pts.append(xy)
            prev = xy
    if len(pts) < 2:
        return None
    return LineString(pts)


def _coverage(trail_json: str, polylines: list, buffer_m: float, threshold_pct: float) -> bool:
    """Return True if the union of decoded activity polylines covers >= threshold_pct
    of the trail geometry (sampled every 5 m after buffering by buffer_m metres)."""
    try:
        trail_coords = json.loads(trail_json)
    except Exception:
        return False

    trail_line = _build_line(trail_coords)
    if not trail_line:
        return False

    activity_lines = []
    for p in polylines:
        try:
            coords = polyline_codec.decode(p)
            line = _build_line(coords)
            if line:
                activity_lines.append(line)
        except Exception:
            continue

    if not activity_lines:
        return False

    buffered = unary_union(activity_lines).buffer(buffer_m)
    n = max(20, int(trail_line.length / 5.0))
    covered = sum(
        1 for i in range(n + 1)
        if buffered.contains(trail_line.interpolate(i / n, normalized=True))
    )
    return (covered / (n + 1)) >= (threshold_pct / 100.0)


def _parse_gpx_bytes(raw: bytes) -> list:
    """Parse a GPX file and return a list of trail dicts.
    Each dict: {name, distance_miles, points: [[lat, lon], ...]}
    Tracks named 'None' (GIS artifacts) are excluded.
    """
    NS = {"gpx": "http://www.topografix.com/GPX/1/1"}
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid GPX file: {exc}")

    trails = []
    for trk in root.findall("gpx:trk", NS):
        name_el = trk.find("gpx:name", NS)
        raw_name = (name_el.text or "").strip() if name_el is not None else ""
        if not raw_name or raw_name.lower() == "none":
            continue

        desc_el = trk.find("gpx:desc", NS)
        distance_miles = None
        if desc_el is not None and desc_el.text:
            try:
                distance_miles = float(desc_el.text.strip())
            except ValueError:
                pass

        points = []
        for seg in trk.findall("gpx:trkseg", NS):
            for pt in seg.findall("gpx:trkpt", NS):
                try:
                    lat = float(pt.attrib["lat"])
                    lon = float(pt.attrib["lon"])
                    points.append([lat, lon])
                except (KeyError, ValueError):
                    continue

        if points:
            trails.append({
                "name": raw_name,
                "distance_miles": distance_miles,
                "points": points,
            })

    return trails


def _get_setting(db: Session, key: str, default):
    """Fetch a value from the settings table."""
    row = db.query(Setting).filter(Setting.key == key).first()
    if row and row.value is not None:
        return row.value
    return default


# ---------------------------------------------------------------------------
# Strava API helpers
# ---------------------------------------------------------------------------

def _ensure_strava_configured():
    if not settings.strava_client_id or not settings.strava_client_secret:
        raise HTTPException(status_code=501, detail="Strava integration is not configured")


def _refresh_token_if_needed(db: Session, conn: StravaConnection) -> str:
    """Return a valid Strava access token, refreshing if expired."""
    now = datetime.now(timezone.utc)
    expires = (
        conn.token_expires_at.replace(tzinfo=timezone.utc)
        if conn.token_expires_at.tzinfo is None
        else conn.token_expires_at
    )
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
    conn.access_token      = data["access_token"]
    conn.refresh_token     = data["refresh_token"]
    conn.token_expires_at  = datetime.fromtimestamp(data["expires_at"], tz=timezone.utc)
    db.commit()
    return conn.access_token


def _strava_get(token: str, path: str, params: dict = None):
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


# ---------------------------------------------------------------------------
# Sync logic
# ---------------------------------------------------------------------------

def _sync_member(db: Session, conn: StravaConnection, trails: list, max_pages: int = 5) -> int:
    """Sync one member's trail completions.
    Fetches Strava activities since challenge_start_date, checks coverage for each
    active trail with geometry. Returns count of newly-completed trails.
    max_pages=5 for member-triggered syncs (~500 activities, stays under 60s);
    max_pages=20 for the nightly scheduler (full history, no timeout pressure).
    """
    challenge_start_str = _get_setting(db, "challenge_start_date", "2026-01-01")
    try:
        challenge_start = datetime.strptime(challenge_start_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        challenge_start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    buffer_m = float(_get_setting(db, "challenge_buffer_distance_m", "15"))
    threshold_pct = float(_get_setting(db, "challenge_coverage_threshold", "95"))

    token = _refresh_token_if_needed(db, conn)

    # Collect activity summary_polylines since challenge start
    polylines = []
    page = 1
    after_ts = int(challenge_start.timestamp())
    while page <= max_pages:
        activities = _strava_get(token, "/athlete/activities", params={
            "per_page": 100,
            "page": page,
            "after": after_ts,
        })
        if not activities:
            break
        for act in activities:
            poly = act.get("map", {}).get("summary_polyline") or ""
            if poly:
                polylines.append(poly)
        if len(activities) < 100:
            break
        page += 1

    now = datetime.now(timezone.utc)
    newly_completed = 0

    for trail in trails:
        if not trail.geometry:
            continue  # No geometry — skip; admin must upload it

        existing = db.query(TrailCompletion).filter(
            TrailCompletion.user_id == conn.user_id,
            TrailCompletion.trail_id == trail.trail_id,
        ).first()

        is_done = _coverage(trail.geometry, polylines, buffer_m, threshold_pct)

        if existing:
            if existing.completed != int(is_done):
                existing.completed = int(is_done)
            existing.last_synced = now
        else:
            db.add(TrailCompletion(
                user_id=conn.user_id,
                trail_id=trail.trail_id,
                completed=int(is_done),
                last_synced=now,
            ))
            if is_done:
                newly_completed += 1

    db.commit()
    return newly_completed


def _sync_all_members(db: Session):
    """Called by APScheduler daily at 03:00 UTC. Sync all connected members."""
    trails = db.query(StravaTrail).filter(StravaTrail.is_active == 1).all()
    connections = db.query(StravaConnection).all()

    for conn in connections:
        try:
            _sync_member(db, conn, trails, max_pages=20)
        except Exception as exc:
            log.error("Sync failed for user_id=%s: %s", conn.user_id, exc)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class StravaCallbackRequest(BaseModel):
    code: str

class CreateTrailRequest(BaseModel):
    name: str
    distance_miles: Optional[float] = None
    elevation_feet: Optional[int] = None
    sort_order: int = 0

class UpdateTrailRequest(BaseModel):
    name: Optional[str] = None
    distance_miles: Optional[float] = None
    elevation_feet: Optional[int] = None
    sort_order: Optional[int] = None
    is_active: Optional[int] = None


# ---------------------------------------------------------------------------
# OAuth Endpoints
# ---------------------------------------------------------------------------

@router.get("/auth-url")
def get_auth_url(current_user: User = Depends(get_current_user)):
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

    data    = resp.json()
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
        conn.access_token      = data["access_token"]
        conn.refresh_token     = data["refresh_token"]
        conn.token_expires_at  = datetime.fromtimestamp(data["expires_at"], tz=timezone.utc)
        conn.athlete_firstname = athlete.get("firstname")
        conn.athlete_lastname  = athlete.get("lastname")
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

    # Delete trail completions before removing the connection (no direct FK cascade)
    db.query(TrailCompletion).filter(TrailCompletion.user_id == current_user.user_id).delete()
    db.delete(conn)
    log_action(db, user_id=current_user.user_id, action="strava_disconnect", entity_type="strava",
               entity_id=current_user.user_id,
               details={"summary": f"{current_user.firstname} {current_user.lastname} disconnected Strava"})
    db.commit()
    return {"detail": "Strava disconnected"}


# ---------------------------------------------------------------------------
# Trail Management (Admin)
# ---------------------------------------------------------------------------

@router.get("/trails")
def list_trails(
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    query = db.query(StravaTrail)
    if not include_inactive or not _user.is_admin:
        query = query.filter(StravaTrail.is_active == 1)

    trails = query.order_by(StravaTrail.sort_order, StravaTrail.name).all()

    return [
        {
            "trail_id":       t.trail_id,
            "name":           t.name,
            "distance_miles": float(t.distance_miles) if t.distance_miles else None,
            "elevation_feet": t.elevation_feet,
            "has_geometry":   t.geometry is not None,
            "sort_order":     t.sort_order,
            "is_active":      t.is_active,
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
        sort_order=payload.sort_order,
        created_by=_admin.user_id,
    )
    db.add(trail)
    log_action(db, user_id=_admin.user_id, action="create", entity_type="strava_trail",
               entity_id=0, details={"summary": f"Created trail: {trail.name}"})
    db.commit()
    db.refresh(trail)
    return {"trail_id": trail.trail_id, "name": trail.name, "detail": f"Created trail: {trail.name}"}


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

    if payload.name           is not None: trail.name           = payload.name
    if payload.distance_miles is not None: trail.distance_miles = payload.distance_miles
    if payload.elevation_feet is not None: trail.elevation_feet = payload.elevation_feet
    if payload.sort_order     is not None: trail.sort_order     = payload.sort_order
    if payload.is_active      is not None: trail.is_active      = payload.is_active

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
# GPX Import (Admin)
# ---------------------------------------------------------------------------

@router.post("/trails/gpx-preview")
async def gpx_preview(
    file: UploadFile = File(...),
    _admin: User = Depends(get_current_admin),
):
    """Parse a GPX file and return the list of named tracks. No DB writes."""
    raw = await file.read()
    trails = _parse_gpx_bytes(raw)

    return {
        "count": len(trails),
        "trails": [
            {
                "name": t["name"],
                "distance_miles": t["distance_miles"],
                "point_count": len(t["points"]),
            }
            for t in trails
        ],
    }


@router.post("/trails/gpx-import", status_code=status.HTTP_201_CREATED)
async def gpx_import(
    file: UploadFile = File(...),
    selections: str = Form(...),   # JSON array of trail names to import
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
):
    """Phase-2 bulk import: parse GPX, filter to selected names, upsert DB rows.
    selections — JSON-encoded list of {name, distance_miles, elevation_feet, sort_order}
    dicts identifying which GPX tracks to import and their admin-confirmed metadata.
    Existing trails with matching names have their geometry updated; new rows are created.
    """
    raw = await file.read()
    gpx_trails = _parse_gpx_bytes(raw)

    try:
        sel_list = json.loads(selections)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="selections must be a JSON array")

    # Index GPX by name for O(1) lookup (last one wins if duplicate names)
    gpx_index = {t["name"]: t for t in gpx_trails}

    created = updated = skipped = 0

    for idx, sel in enumerate(sel_list):
        name = (sel.get("name") or "").strip()
        if not name:
            skipped += 1
            continue

        gpx_entry = gpx_index.get(name)
        geometry_json = json.dumps(gpx_entry["points"]) if gpx_entry else None

        existing = db.query(StravaTrail).filter(StravaTrail.name == name).first()
        if existing:
            if geometry_json:
                existing.geometry = geometry_json
            if sel.get("distance_miles") is not None:
                existing.distance_miles = sel["distance_miles"]
            if sel.get("elevation_feet") is not None:
                existing.elevation_feet = sel["elevation_feet"]
            if sel.get("sort_order") is not None:
                existing.sort_order = sel["sort_order"]
            updated += 1
        else:
            trail = StravaTrail(
                name=name,
                distance_miles=sel.get("distance_miles") or (gpx_entry["distance_miles"] if gpx_entry else None),
                elevation_feet=sel.get("elevation_feet"),
                geometry=geometry_json,
                sort_order=sel.get("sort_order", idx),
                created_by=_admin.user_id,
            )
            db.add(trail)
            created += 1

    log_action(db, user_id=_admin.user_id, action="gpx_import", entity_type="strava_trail",
               entity_id=0,
               details={"summary": f"GPX import: {created} created, {updated} updated, {skipped} skipped"})
    db.commit()

    return {
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "detail": f"GPX import complete: {created} created, {updated} updated",
    }


@router.post("/trails/{trail_id}/geometry")
async def upload_trail_geometry(
    trail_id: int,
    file: UploadFile = File(...),
    track_name: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
):
    """Upload a GPX file for a single trail. If the file contains multiple tracks,
    track_name selects which one; otherwise all tracks are merged."""
    trail = db.get(StravaTrail, trail_id)
    if not trail:
        raise HTTPException(status_code=404, detail="Trail not found")

    raw = await file.read()
    gpx_trails = _parse_gpx_bytes(raw)

    if not gpx_trails:
        raise HTTPException(status_code=400, detail="No valid tracks found in GPX file")

    if track_name:
        matching = [t for t in gpx_trails if t["name"] == track_name]
        if not matching:
            raise HTTPException(status_code=400, detail=f"Track '{track_name}' not found in GPX file")
        points = matching[0]["points"]
    else:
        # Merge all tracks
        points = []
        for t in gpx_trails:
            points.extend(t["points"])

    trail.geometry = json.dumps(points)
    log_action(db, user_id=_admin.user_id, action="update", entity_type="strava_trail",
               entity_id=trail_id,
               details={"summary": f"Uploaded geometry for trail: {trail.name} ({len(points)} points)"})
    db.commit()

    return {"detail": f"Geometry uploaded for '{trail.name}': {len(points)} points"}


# ---------------------------------------------------------------------------
# Sync Endpoints
# ---------------------------------------------------------------------------

@router.post("/sync")
def sync_trails(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Member-triggered sync: fetch Strava activities and update trail completions."""
    _ensure_strava_configured()

    conn = db.query(StravaConnection).filter(StravaConnection.user_id == current_user.user_id).first()
    if not conn:
        raise HTTPException(status_code=400, detail="Connect your Strava account first")

    trails = db.query(StravaTrail).filter(StravaTrail.is_active == 1).all()

    try:
        _sync_member(db, conn, trails)
    except HTTPException:
        raise
    except Exception as exc:
        log.error("Sync error for user_id=%s: %s", current_user.user_id, exc)
        raise HTTPException(status_code=502, detail="Sync failed. Please try again.")

    completed = db.query(TrailCompletion).filter(
        TrailCompletion.user_id == current_user.user_id,
        TrailCompletion.completed == 1,
    ).count()

    log_action(db, user_id=current_user.user_id, action="strava_sync", entity_type="strava",
               entity_id=current_user.user_id,
               details={"summary": f"Trail sync complete: {completed} trail(s) completed"})

    return {"detail": "Sync complete", "completed_trails": completed}


@router.post("/admin/sync-all")
def admin_sync_all(
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
):
    """Admin-triggered sync for all connected members."""
    _ensure_strava_configured()
    _sync_all_members(db)
    return {"detail": "Full sync complete"}


# ---------------------------------------------------------------------------
# Trails Challenge (Member)
# ---------------------------------------------------------------------------

@router.get("/trails-challenge")
def get_trails_challenge(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return challenge progress for the current user."""
    conn = db.query(StravaConnection).filter(StravaConnection.user_id == current_user.user_id).first()
    connected = conn is not None

    trails = (
        db.query(StravaTrail)
        .filter(StravaTrail.is_active == 1)
        .order_by(StravaTrail.sort_order, StravaTrail.name)
        .all()
    )

    # Build completion lookup for this user
    completion_rows = {}
    last_synced_ts = None
    if connected:
        for row in db.query(TrailCompletion).filter(TrailCompletion.user_id == current_user.user_id).all():
            completion_rows[row.trail_id] = row
            if row.last_synced and (last_synced_ts is None or row.last_synced > last_synced_ts):
                last_synced_ts = row.last_synced

    completed_count = 0
    trail_list = []

    for t in trails:
        row = completion_rows.get(t.trail_id)
        is_completed = bool(row and row.completed)
        last_synced  = row.last_synced.isoformat() if (row and row.last_synced) else None

        if is_completed:
            completed_count += 1

        trail_list.append({
            "trail_id":       t.trail_id,
            "name":           t.name,
            "distance_miles": float(t.distance_miles) if t.distance_miles else None,
            "elevation_feet": t.elevation_feet,
            "has_geometry":   t.geometry is not None,
            "is_completed":   is_completed,
            "last_synced":    last_synced,
        })

    return {
        "connected":       connected,
        "total_trails":    len(trails),
        "completed_trails": completed_count,
        "last_synced":     last_synced_ts.isoformat() if last_synced_ts else None,
        "trails":          trail_list,
    }


@router.get("/trails-challenge/leaderboard")
def get_trails_challenge_leaderboard(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Leaderboard ranked by trails completed (tied ranks, no total time)."""
    total_trails = db.query(StravaTrail).filter(StravaTrail.is_active == 1).count()
    if total_trails == 0:
        return []

    # Aggregate completions per user
    rows = (
        db.query(
            TrailCompletion.user_id,
            TrailCompletion.last_synced,
        )
        .filter(TrailCompletion.completed == 1)
        .all()
    )

    from collections import defaultdict
    user_data: dict[int, dict] = defaultdict(lambda: {"count": 0, "last_synced": None})
    for row in rows:
        user_data[row.user_id]["count"] += 1
        if row.last_synced:
            prev = user_data[row.user_id]["last_synced"]
            if prev is None or row.last_synced > prev:
                user_data[row.user_id]["last_synced"] = row.last_synced

    if not user_data:
        return []

    # Load users in bulk
    user_ids = list(user_data.keys())
    users = {u.user_id: u for u in db.query(User).filter(User.user_id.in_(user_ids)).all()}

    entries = []
    for uid, data in user_data.items():
        user = users.get(uid)
        if not user:
            continue
        last = data["last_synced"]
        entries.append({
            "user_id":            uid,
            "name":               f"{user.firstname} {user.lastname}",
            "trails_completed":   data["count"],
            "total_trails":       total_trails,
            "last_synced_display": last.strftime("%-m/%-d/%y") if last else None,
            "is_current_user":    uid == current_user.user_id,
            "_last_synced_raw":   last,
        })

    # Sort by trails completed desc; secondary by last_synced asc (earlier = was there longer)
    entries.sort(key=lambda x: (-x["trails_completed"], x["_last_synced_raw"] or datetime.max))

    # Assign tied ranks
    rank = 1
    for i, entry in enumerate(entries):
        if i > 0 and entry["trails_completed"] < entries[i - 1]["trails_completed"]:
            rank = i + 1
        entry["rank"] = rank
        del entry["_last_synced_raw"]

    return entries
