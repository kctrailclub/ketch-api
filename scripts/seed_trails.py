#!/usr/bin/env python3
"""
seed_trails.py — EP-0031 / RSK-0004
Populate the strava_trails table from the KCR Master Trails GPX file.

Usage (run from kctc-api repo root after running the migration SQL):
    python scripts/seed_trails.py /path/to/KCR\ Master\ Trails\ GPX.gpx

The script:
  1. Parses the GPX file to extract all named tracks (skips 'None'-named GIS artifacts).
  2. Applies the champion-approved 34-trail mapping:
     - Massey Draw: uses the first matching track (merged into one entry).
     - Shaffer: split into "North Shaffer" (lat >= 39.578) and "South Shaffer" (lat < 39.578).
     - Connector—Lark Bunting to Hogback: inserted with no geometry (no match in GPX).
  3. Upserts each trail into strava_trails (matching on name).
     - Creates new row if trail name not found.
     - Updates geometry + distance_miles on existing rows.

Environment variables (same as the API):
    DATABASE_URL — mysql+pymysql://user:pass@host/db
Or set DB_* vars individually: DB_USER, DB_PASSWORD, DB_HOST, DB_PORT, DB_NAME.
"""

import json
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone


def _parse_gpx(path: str) -> list:
    """Return list of {name, distance_miles, points: [[lat, lon], ...]}."""
    NS = {"gpx": "http://www.topografix.com/GPX/1/1"}
    tree = ET.parse(path)
    root = tree.getroot()
    tracks = []
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
                    points.append([float(pt.attrib["lat"]), float(pt.attrib["lon"])])
                except (KeyError, ValueError):
                    pass
        if points:
            tracks.append({"name": raw_name, "distance_miles": distance_miles, "points": points})
    return tracks


# ── 34-trail authoritative list with GPX name mappings ──────────────────────
# Format: (display_name, gpx_name or None, distance_mi_override or None, elevation_ft or None, sort_order)
# gpx_name=None means no geometry available.
# distance_mi_override overrides the GPX <desc> value when set.
# 'SHAFFER_NORTH' / 'SHAFFER_SOUTH' are sentinel values handled specially.

TRAIL_DEFS = [
    ("Bear Creek Trail",                   "Bear Creek Trail",                  None, None, 1),
    ("Cathy Johnson Trail",                "Cathy Johnson Trail",               None, None, 2),
    ("Coyote Song Trail",                  "Coyote Song Trail",                 None, None, 3),
    ("Cub Creek Trail",                    "Cub Creek Trail",                   None, None, 4),
    ("Dead Deer Trail",                    "Dead Deer Trail",                   None, None, 5),
    ("Deer Creek Trail",                   "Deer Creek Trail",                  None, None, 6),
    ("Eagle's Wing Trail",                 "Eagle's Wing Trail",                None, None, 7),
    ("Eastern Ridge Trail",                "Eastern Ridge Trail",               None, None, 8),
    ("Falcon Trail",                       "Falcon Trail",                      None, None, 9),
    ("Fox Meadow Trail",                   "Fox Meadow Trail",                  None, None, 10),
    ("Hogback Trail",                      "Hogback Trail",                     None, None, 11),
    ("Indian Creek Trail",                 "Indian Creek Trail",                None, None, 12),
    ("Kossler Lake Trail",                 "Kossler Lake Trail",                None, None, 13),
    ("Long Scraggy Trail",                 "Long Scraggy Trail",                None, None, 14),
    ("Massey Draw Trail",                  "Massey Draw Trail",                 None, None, 15),
    ("Meadowlark Trail",                   "Meadowlark Trail",                  None, None, 16),
    ("Mixed Bag Trail",                    "Mixed Bag Trail",                   None, None, 17),
    ("No Name Creek Trail",                "No Name Creek Trail",               None, None, 18),
    ("Outer Mongolia Trail",               "Outer Mongolia Trail",              None, None, 19),
    ("Owl Hollow Trail",                   "Owl Hollow Trail",                  None, None, 20),
    ("Pearce Ranch Trail",                 "Pearce Ranch Trail",                None, None, 21),
    ("Raccoon Trail",                      "Raccoon Trail",                     None, None, 22),
    ("Rattlesnake Gulch Trail",            "Rattlesnake Gulch Trail",           None, None, 23),
    ("Ravine Trail",                       "Ravine Trail",                      None, None, 24),
    ("Red Mesa Loop",                      "Red Mesa Loop",                     None, None, 25),
    ("Ridgeline Trail",                    "Ridgeline Trail",                   None, None, 26),
    ("South Rim Trail",                    "South Rim Trail",                   None, None, 27),
    ("North Shaffer Trail",                "SHAFFER_NORTH",                     None, None, 28),
    ("South Shaffer Trail",                "SHAFFER_SOUTH",                     None, None, 29),
    ("Sluicebox Trail",                    "Sluicebox Trail",                   None, None, 30),
    ("Sunrise Trail",                      "Sunrise Trail",                     None, None, 31),
    ("Timberline Trail",                   "Timberline Trail",                  None, None, 32),
    ("Valley View Trail",                  "Valley View Trail",                 None, None, 33),
    # No geometry — connector path without GPX representation
    ("Connector—Lark Bunting to Hogback",  None,                                None, None, 34),
]

# Latitude boundary for Shaffer split (champion DEC-0016 approved 2026-05-22)
SHAFFER_LAT_SPLIT = 39.578


def _split_shaffer(tracks: list):
    """Return (north_points, south_points) from Shaffer tracks."""
    # Find all tracks whose name contains "Shaffer"
    shaffer_tracks = [t for t in tracks if "Shaffer" in t["name"]]
    north_pts, south_pts = [], []
    for t in shaffer_tracks:
        for pt in t["points"]:
            if pt[0] >= SHAFFER_LAT_SPLIT:
                north_pts.append(pt)
            else:
                south_pts.append(pt)
    return north_pts, south_pts


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/seed_trails.py <path/to/KCR Master Trails GPX.gpx>")
        sys.exit(1)

    gpx_path = sys.argv[1]
    if not os.path.exists(gpx_path):
        print(f"ERROR: file not found: {gpx_path}")
        sys.exit(1)

    print(f"Parsing GPX: {gpx_path}")
    tracks = _parse_gpx(gpx_path)
    print(f"  Found {len(tracks)} named tracks")

    # Build lookup by name (last segment wins if dupes)
    gpx_lookup = {}
    for t in tracks:
        gpx_lookup[t["name"]] = t

    # Pre-compute Shaffer splits
    north_shaffer_pts, south_shaffer_pts = _split_shaffer(tracks)
    print(f"  Shaffer split: {len(north_shaffer_pts)} north pts / {len(south_shaffer_pts)} south pts")

    # ── Connect to DB ──────────────────────────────────────────
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        user     = os.environ.get("DB_USER", "root")
        password = os.environ.get("DB_PASSWORD", "")
        host     = os.environ.get("DB_HOST", "localhost")
        port     = os.environ.get("DB_PORT", "3306")
        name     = os.environ.get("DB_NAME", "kctc")
        database_url = f"mysql+pymysql://{user}:{password}@{host}:{port}/{name}"

    from sqlalchemy import create_engine, text
    engine = create_engine(database_url)

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    created = updated = skipped = 0

    with engine.begin() as conn:
        for display_name, gpx_key, dist_override, elev_ft, sort_order in TRAIL_DEFS:
            # Resolve geometry
            geometry_json = None
            distance_miles = dist_override

            if gpx_key == "SHAFFER_NORTH":
                if north_shaffer_pts:
                    geometry_json = json.dumps(north_shaffer_pts)
            elif gpx_key == "SHAFFER_SOUTH":
                if south_shaffer_pts:
                    geometry_json = json.dumps(south_shaffer_pts)
            elif gpx_key is not None:
                track = gpx_lookup.get(gpx_key)
                if track:
                    geometry_json = json.dumps(track["points"])
                    if distance_miles is None:
                        distance_miles = track.get("distance_miles")
                else:
                    print(f"  WARN: no GPX match for '{display_name}' (looked for '{gpx_key}')")

            # Check if trail already exists
            existing = conn.execute(
                text("SELECT trail_id FROM strava_trails WHERE name = :name"),
                {"name": display_name}
            ).fetchone()

            if existing:
                # Update geometry and distance if available
                params = {
                    "trail_id": existing[0],
                    "geometry": geometry_json,
                    "distance_miles": distance_miles,
                    "sort_order": sort_order,
                    "updated": now_str,
                }
                conn.execute(text("""
                    UPDATE strava_trails
                    SET geometry       = :geometry,
                        distance_miles = COALESCE(:distance_miles, distance_miles),
                        sort_order     = :sort_order,
                        updated        = :updated
                    WHERE trail_id     = :trail_id
                """), params)
                status = "geo" if geometry_json else "no-geo"
                print(f"  UPDATE [{status:6s}] {display_name}")
                updated += 1
            else:
                params = {
                    "name": display_name,
                    "distance_miles": distance_miles,
                    "elevation_feet": elev_ft,
                    "geometry": geometry_json,
                    "sort_order": sort_order,
                    "is_active": 1,
                    "created": now_str,
                    "updated": now_str,
                }
                conn.execute(text("""
                    INSERT INTO strava_trails
                        (name, distance_miles, elevation_feet, geometry, sort_order, is_active, created, updated)
                    VALUES
                        (:name, :distance_miles, :elevation_feet, :geometry, :sort_order, :is_active, :created, :updated)
                """), params)
                status = "geo" if geometry_json else "no-geo"
                print(f"  CREATE [{status:6s}] {display_name}")
                created += 1

    print(f"\nDone: {created} created, {updated} updated, {skipped} skipped")
    print("Next: run the migration SQL, then verify row count:")
    print("  SELECT COUNT(*) FROM strava_trails;  -- expect 34")


if __name__ == "__main__":
    main()
