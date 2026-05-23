-- Migration: Trails Challenge v2 — RSK-0004 schema refactor
-- EP-0031 / ADR-0003  2026-05-22
-- Run against staging DB first; prod deploy gated on Tester sign-off (R-0004).
--
-- Order matters: drop dependent tables before parent tables,
-- add new tables after their FK targets exist.

-- ── 1. Drop old segment-based tables (ToS-violating raw Strava data) ─────────

DROP TABLE IF EXISTS strava_segment_efforts;
DROP TABLE IF EXISTS strava_trail_segments;
DROP TABLE IF EXISTS strava_segments;

-- ── 2. Migrate strava_trails ──────────────────────────────────────────────────

-- Add geometry column (stores JSON [[lat,lon],…] decoded from GPX)
ALTER TABLE strava_trails
    ADD COLUMN geometry LONGTEXT NULL AFTER elevation_feet;

-- Widen distance_miles to accommodate merged-trail precision
ALTER TABLE strava_trails
    MODIFY COLUMN distance_miles DECIMAL(6,3) NULL;

-- Drop the year column (challenge is open-ended; no annual reset in v1)
ALTER TABLE strava_trails
    DROP COLUMN year;

-- ── 3. Add trail_completions table ───────────────────────────────────────────

CREATE TABLE trail_completions (
    completion_id INT          NOT NULL AUTO_INCREMENT,
    user_id       INT          NOT NULL,
    trail_id      INT          NOT NULL,
    completed     TINYINT(1)   NOT NULL DEFAULT 0,
    last_synced   DATETIME     NULL,
    PRIMARY KEY (completion_id),
    UNIQUE KEY uq_user_trail (user_id, trail_id),
    CONSTRAINT fk_tc_user  FOREIGN KEY (user_id)  REFERENCES users(user_id)         ON DELETE CASCADE,
    CONSTRAINT fk_tc_trail FOREIGN KEY (trail_id) REFERENCES strava_trails(trail_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ── 4. Seed challenge settings (idempotent) ───────────────────────────────────

INSERT INTO settings (key_name, value, description)
VALUES
    ('challenge_start_date',         '2026-01-01', 'Trails Challenge evaluation start date (YYYY-MM-DD)'),
    ('challenge_coverage_threshold', '95',         'Minimum % of trail geometry an activity union must cover'),
    ('challenge_buffer_distance_m',  '15',         'GPS buffer radius in metres for trail coverage matching')
ON DUPLICATE KEY UPDATE key_name = key_name;
