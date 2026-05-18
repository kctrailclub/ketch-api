-- Migration: add nl_query_usage table
-- Closes: SEC-0011 (per-admin daily NL Query quota)
-- Run BEFORE deploying the matching code change.
-- Safe to re-run: CREATE TABLE IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS nl_query_usage (
    user_id    INT  NOT NULL,
    query_date DATE NOT NULL,
    count      INT  NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, query_date),
    CONSTRAINT fk_nlq_user FOREIGN KEY (user_id)
        REFERENCES users (user_id) ON DELETE CASCADE
);
