-- Migration: Trails Challenge v2 — PRODUCTION (fresh install)
-- EP-0031 / ADR-0003  2026-05-23
--
-- Use this file against PRODUCTION. Do NOT use it on staging (staging already
-- has the old Strava tables and was upgraded via 2026-05-22_trails_challenge_v2.sql).
--
-- Prod has no Strava tables at all (feature was staging-only). This migration
-- creates the three required tables from scratch and seeds challenge settings.
--
-- Run order:
--   1. Run this SQL against the prod MySQL service.
--   2. Cherry-pick the seven API+app commits onto main.
--   3. Let Railway redeploy (auto on main push).
--   4. Run scripts/seed_trails.py against the prod DB (with DATABASE_URL set).
--
-- Pre-conditions verified (SA, 2026-05-23):
--   - `strava_connections`, `strava_trails`, `trail_completions` do NOT exist on prod.
--   - `users` and `settings` tables exist (required by FKs and settings insert).

-- ── 1. Strava OAuth connections ───────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS strava_connections (
    connection_id      INT          NOT NULL AUTO_INCREMENT,
    user_id            INT          NOT NULL,
    strava_athlete_id  BIGINT       NOT NULL,
    access_token       VARCHAR(255) NOT NULL,
    refresh_token      VARCHAR(255) NOT NULL,
    token_expires_at   DATETIME     NOT NULL,
    athlete_firstname  VARCHAR(100) NULL,
    athlete_lastname   VARCHAR(100) NULL,
    created            DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated            DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (connection_id),
    UNIQUE KEY uq_sc_user      (user_id),
    UNIQUE KEY uq_sc_athlete   (strava_athlete_id),
    CONSTRAINT fk_sc_user FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ── 2. Challenge trails (geometry-first; no year column) ─────────────────────

CREATE TABLE IF NOT EXISTS strava_trails (
    trail_id       INT            NOT NULL AUTO_INCREMENT,
    name           VARCHAR(200)   NOT NULL,
    distance_miles DECIMAL(6,3)   NULL,
    elevation_feet INT            NULL,
    geometry       LONGTEXT       NULL,          -- JSON [[lat,lon],…] decoded from GPX
    sort_order     INT            NOT NULL DEFAULT 0,
    is_active      INT            NOT NULL DEFAULT 1,
    created_by     INT            NULL,
    created        DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated        DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (trail_id),
    CONSTRAINT fk_st_creator FOREIGN KEY (created_by) REFERENCES users(user_id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ── 3. Per-member trail completion records ────────────────────────────────────

CREATE TABLE IF NOT EXISTS trail_completions (
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
