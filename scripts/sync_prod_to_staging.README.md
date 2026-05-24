# `sync_prod_to_staging.sh` — Prod → Staging DB Sync

**Version:** 1.0.0
**Date:** 2026-05-19
**Project:** Ken-Caryl Trail Club Application (KCTC)
**Owner:** Solution Architect (script); Champion (executes)
**Backlog item:** BL-0022
**Inherits from:** Versioning & Disclosure Standards v1.1.0

---

## 1. Purpose

Refresh the staging MySQL database with a copy of production data so staging stops drifting from prod, while **preserving** the staging-only Strava Trails Challenge tables (which prod does not have until [BL-0007](../../backlog/Backlog_Index.md) clears).

Conceptually:

```
[staging strava data] → side-save → [restore prod over staging] → reload strava → smoke checks
```

## 2. Scope

- **In scope:** A repeatable, idempotent shell script that the champion runs from his workstation. Save-restore of the 5 Strava tables. FK-orphan handling for the saved Strava rows. Smoke verification.
- **Out of scope:** Automated scheduling; BL-0019 dormancy deactivation (sequenced separately); any code change in `kctc-api` or `kctc-app`; running the script against prod as the *write target* (the script is one-directional, prod → staging, and refuses to run the other way).

## 3. Decisions baked in

| # | Decision | Source |
|---|---|---|
| D-1 | **Strava handling: save & restore** — dump the 5 staging-only Strava tables before the prod restore, then reload them after. | Champion direction, SA session 2026-05-19. |
| D-2 | **FK-orphan policy: silent skip + log** — any saved Strava row whose `user_id` (or, where the FK exists, `created_by`) does not resolve in the freshly-restored `users` table is **skipped silently**, with the count and the row identifiers written to a log file the script prints at the end. The script does not abort. | Champion direction, SA session 2026-05-19 (option "Save & restore (Recommended)" — silent skip variant). |
| D-3 | **BL-0019 sequencing: sync now, deactivate later** — BL-0022 runs against current prod (no dormancy deactivation in place yet). When BL-0019 eventually runs, staging will be re-synced or take its own deactivation pass. | Champion direction, SA session 2026-05-19. |
| D-4 | **Runtime model: local bash script** — champion runs from his workstation using `mysql` / `mysqldump` clients against prod (read) and staging (read+write) endpoints. No Railway-side automation. | Champion direction, SA session 2026-05-19. |
| D-5 | **Verification: smoke checks only** — script auto-validates row-count parity on a handful of critical tables and confirms Strava reload matches pre-sync counts (modulo skipped orphans). Champion separately logs in with 2–3 test accounts post-run for a 5–10 min spot check on waiver dates + household primary contacts. No full Deploy Sign-off Checklist run. | Champion direction, SA session 2026-05-19. |
| D-6 | **Safety net: full staging dump before any destructive action** — the script's first action after the Strava side-save is a complete `mysqldump` of staging into a timestamped file, so a botched run can be reverted. | SA recommendation; standard hygiene. |
| D-7 | **One-way directionality, host-equality only** — script refuses to run if the prod host:port matches the staging host:port (same DB on both sides — catastrophic misconfiguration). No name-based rule, since Railway names both DBs `railway` and a name rule would just produce friction. Champion is trusted to place the right `.cnf` file in the right `--prod-cnf` / `--staging-cnf` flag. | Champion direction, SA session 2026-05-19. |

## 4. Prerequisites

| # | Item | How to obtain |
|---|---|---|
| 1 | `mysql` and `mysqldump` clients installed locally | `brew install mysql-client` on macOS. Ensure both binaries are on `$PATH`. |
| 2 | Prod MySQL connection details (host, port, user, pass, db name) | Railway → production project → MySQL service → Connect tab. |
| 3 | Staging MySQL connection details | Railway → staging project → API service (combined with MySQL per [MEMORY.md](../../../.claude/projects/-Users-johnhamilton-Downloads-KCTCAppDocs/memory/MEMORY.md)) → MySQL panel. |
| 4 | Two MySQL credential files: `~/.my.kctc-prod.cnf` and `~/.my.kctc-staging.cnf` | Created by the champion (see §5 below). The script uses `--defaults-extra-file=` to avoid putting credentials on the command line. |
| 5 | Local disk space ~3× the prod DB size in `/tmp` (or wherever `--workdir` points) | Three intermediate dump files live for the duration of the run, then can be auto-cleaned. |
| 6 | A brief window of staging unavailability (~5–15 minutes depending on DB size) | Run after-hours. |

## 5. Credential file format

Two files, mode `0600`, owned by champion:

```ini
# ~/.my.kctc-prod.cnf
[client]
host=<prod-mysql-host>
port=<prod-mysql-port>
user=<prod-mysql-user>
password=<prod-mysql-password>
```

```ini
# ~/.my.kctc-staging.cnf
[client]
host=<staging-mysql-host>
port=<staging-mysql-port>
user=<staging-mysql-user>
password=<staging-mysql-password>
```

The DB *name* is passed separately on the command line (typically `railway` on both, but verify in Railway).

## 6. Usage

```bash
cd kctc-api
./scripts/sync_prod_to_staging.sh \
  --prod-cnf ~/.my.kctc-prod.cnf \
  --prod-db railway \
  --staging-cnf ~/.my.kctc-staging.cnf \
  --staging-db railway \
  --workdir /tmp/kctc-sync \
  [--dry-run] \
  [--no-cleanup] \
  [--yes]
```

Flags:

- `--dry-run` — run all `mysqldump` operations and orphan-detection queries, but **do not** apply changes to staging. Prints what would happen. Use this first, every time.
- `--no-cleanup` — keep all intermediate dump files in `--workdir` after success. Default is to keep only the staging-safety-net dump and the Strava save; the prod dump is deleted on success.
- `--yes` — non-interactive; assume yes to the "are you sure?" prompt. Default behavior pauses for confirmation before any destructive step.

## 7. What the script does, in order

```
0. Pre-flight
   - Verify both .cnf files readable, mode 0600.
   - Verify `mysql` and `mysqldump` on PATH.
   - SELECT COUNT(*) from a sentinel table on each side to confirm connectivity.
   - Refuse if staging host:port matches prod host:port (D-7).

1. Strava side-save (staging → file)
   - mysqldump from staging: only the 5 Strava tables (schema + data).
   - Output: $workdir/strava_save_$timestamp.sql
   - Record per-table row counts to $workdir/strava_save_counts_$timestamp.txt

2. Staging safety-net dump (staging → file)
   - mysqldump full staging DB. Output: $workdir/staging_backup_$timestamp.sql
   - Kept on disk regardless of --no-cleanup. This is the rollback artifact.

3. Prod dump (prod → file)
   - mysqldump full prod DB with --single-transaction --no-tablespaces.
   - Output: $workdir/prod_dump_$timestamp.sql

4. Confirmation prompt (unless --yes)
   - Print: workdir, prod dump size, staging row counts that will change.
   - Wait for "yes" typed verbatim.

5. Drop Strava tables on staging (FK-safe order)
   - SET FOREIGN_KEY_CHECKS=0;
   - DROP TABLE IF EXISTS strava_segment_efforts, strava_trail_segments,
     strava_trails, strava_connections, strava_segments;
   - SET FOREIGN_KEY_CHECKS=1;

6. Restore prod dump onto staging
   - mysql staging < prod_dump_$timestamp.sql
   - This drops + recreates every prod table (not Strava — they're not in the prod dump).
   - Strava data was lost during step 5; that's fine — we'll reload from $strava_save.

7. Create a temporary schema to hold the Strava save
   - CREATE DATABASE strava_save_$timestamp;
   - mysql strava_save_$timestamp < strava_save_$timestamp.sql

8. Re-create empty Strava tables on staging
   - mysqldump --no-data strava_save_$timestamp | mysql staging
   - Strava tables now exist on staging with original schema + FKs.

9. Orphan diagnostics (before insert)
   - Run SELECT COUNT(*) JOIN queries against staging.users + strava_save_$timestamp.*
     to count orphans per table (connections, segments, trails).
   - Print summary; write detail to $workdir/orphans_$timestamp.log.

10. Insert filtered Strava data
    - SET FOREIGN_KEY_CHECKS = 0 (defense; we filter explicitly).
    - INSERT INTO strava_connections   SELECT ... INNER JOIN users (drop orphan user_id rows).
    - INSERT INTO strava_segments      SELECT ... LEFT JOIN users (NULL created_by OR valid).
    - INSERT INTO strava_trails        SELECT ... LEFT JOIN users (NULL created_by OR valid).
    - INSERT INTO strava_trail_segments SELECT ... INNER JOIN strava_trails + strava_segments.
    - INSERT INTO strava_segment_efforts SELECT ... INNER JOIN strava_connections + strava_segments.
    - SET FOREIGN_KEY_CHECKS = 1.

11. Smoke checks
    - Row-count parity (staging vs prod) on: users, households, projects, hours,
      audit_logs, refresh_tokens, settings. Tolerances: zero variance expected
      (sync was just done), warn if any row differs.
    - Row-count consistency (post-reload vs pre-save) on each Strava table:
      pre-save count - orphans = post-restore count.
    - Staging API health: curl https://<staging-api>/health → expect 200.
      (Endpoint URL passed via --staging-api-url or env var; soft-fail if not set.)

12. Cleanup
    - DROP DATABASE strava_save_$timestamp; (unless --no-cleanup)
    - rm prod_dump_$timestamp.sql (unless --no-cleanup)
    - Keep staging_backup_$timestamp.sql + strava_save_$timestamp.sql + orphan log
      regardless (rollback artifacts).

13. Final report
    - Print summary: timestamp, durations, row-count parity, orphan counts,
      paths to all retained files. Exit 0.
```

## 8. Failure modes and recovery

| Failure | Recovery |
|---|---|
| Pre-flight fails | Script refuses to start. No state change. Fix prereq and re-run. |
| Strava save fails | No destructive action has happened yet; staging still has live Strava data. Investigate and re-run. |
| Prod dump fails | Same — no destructive action yet. Investigate (Railway access? credentials?). |
| Drop Strava tables succeeds but prod restore fails | Staging now has prod tables in inconsistent state and no Strava tables. **Recovery:** restore the staging-safety-net dump (`mysql staging < staging_backup_$timestamp.sql`). Investigate the prod dump file (corrupt? incompatible MySQL version?). |
| Strava reload INSERT fails | Prod data is on staging fine; only Strava reload is incomplete. **Recovery:** the script's filtered INSERTs are wrapped in a transaction; on failure it rolls back the partial reload and prints the offending statement. Inspect, fix, re-run just step 10 manually from the temp schema (`strava_save_$timestamp` is still present unless `--no-cleanup` was off and previous run already cleaned). |
| Smoke checks fail row-count parity | The restore happened but something is off. Inspect the divergent tables manually before declaring success. The staging-safety-net dump is the rollback path. |
| Total rollback needed | `mysql staging < $workdir/staging_backup_$timestamp.sql` puts staging back to its pre-run state. The safety-net dump is kept on disk indefinitely (until champion cleans it). |

## 9. Post-run champion checklist

After the script reports success, the champion runs a brief manual spot-check (per D-5 verification scope):

- [ ] Log into staging admin (`kctcstaging.netlify.app`) with the admin test account from [PL test credentials](../../../.claude/projects/-Users-johnhamilton-Downloads-KCTCAppDocs/memory/project_pl_test_credentials.md).
- [ ] Open Members admin → confirm waiver dates appear for ~3 known accounts (match what's on prod).
- [ ] Open Households admin → confirm primary contact (P badge) is set for ~3 known households (match what's on prod).
- [ ] Open Reports → Activity Log → confirm rows from this morning's prod activity appear.
- [ ] Open Resources page (member view) as the family test account → confirm Strava Trails Challenge cards still render with last-known progress data.
- [ ] Spot-check one connected Strava athlete: do they still see their segment efforts post-sync? (If their `user_id` was an orphan, they would have been silent-skipped — confirm against `orphans_*.log`.)

If any check fails, do not declare the sync successful. Investigate; restore the safety-net dump if needed.

## 10. Test impact

No application-code tests are affected. The script lives outside the test surface. The script's own correctness is validated by:

- The mandatory `--dry-run` first pass before any real run (champion can compare planned operations to expectations).
- The smoke-check step inside the script.
- The post-run manual checklist (§9).

The first real run is itself the integration test. If the run fails any smoke check, the script does not exit 0 and the safety-net dump is on disk.

## 11. Doc impact

- [Backlog_Index.md](../../../backlog/Backlog_Index.md) — BL-0022 row Status flips to *In session* when champion starts a run, *Closed* on success. Both transitions are PL work in a grooming session.
- [MEMORY.md](../../../.claude/projects/-Users-johnhamilton-Downloads-KCTCAppDocs/memory/MEMORY.md) — "What's on Prod vs Staging" section updated once staging is converged (the existing line "**Staging has all of the above PLUS:** Strava Trails Challenge" stays accurate but is reaffirmed).
- [Technical_Debt_Register.md](../../architecture/Technical_Debt_Register.md) — no new TD entry expected; this script intentionally is *not* an architectural change.
- No Tech Architecture Guide or Admin Manual update needed (the sync is an operational task, not a user-facing change).

## 12. Future / V2

If sync frequency rises to "more than twice a year," consider:

- Move to a Railway-side scheduled job (the option the champion declined in 2026-05-19 SA session) so syncs don't depend on the champion's workstation being available.
- Add anonymization of PII for use cases where a developer needs prod-like data locally — out of scope for the current trust model (champion is the only direct DB consumer), but worth noting.
- Capture as an ADR if sync model becomes a regular ops practice — currently this is a one-off script not a durable architectural decision.

These are explicitly *not* in scope for v1.0.0.

## 13. Source citations

### 13.1 Repo state at session start (Standards §18)

| Repo | HEAD SHA | Branch | Date captured |
|---|---|---|---|
| `kctc-api` | `797c51d795f1697405abc2806c581ddac2e2c02c` | `main` | 2026-05-19 |
| `kctc-app` | `34b2aa0b27a8b1dc56f9e8970fc18c17857e14ed` | `main` | 2026-05-19 |

### 13.2 Sources

- [Backlog_Index.md](../../../backlog/Backlog_Index.md) v1.28.0 — BL-0022 row (open decisions captured at file).
- `kctc-api/app/models/models.py` lines 14–243 @ `797c51d` — table inventory on `main` (16 tables; no Strava).
- `kctc-api/app/models/models.py` lines 14–293 @ `staging` (`bc27688`) — table inventory on `staging` (21 tables; +5 Strava).
- SA session 2026-05-19 — four open BL-0022 decisions settled (D-1 through D-5; D-6 + D-7 added by SA).
- [Solution Architect role prompt](../../../kickoff/05_Solution_Architect_role_prompt.md) v1.2.0 — §3 Patch-and-review push model; §5 outputs.
- [Versioning & Disclosure Standards](../../../kickoff/02_Versioning_and_Disclosure_Standards.md) v1.1.0 — §10 incremental approval; §18 repo state.
- MEMORY.md — Strava staging-only fact ("Strava Trails Challenge — staging only, waiting on Strava response before prod deploy").

## 14. Change Log

| Version | Date | Author | Change |
|---|---|---|---|
| 1.0.0 | 2026-05-19 | Solution Architect | Initial version. Captures four champion decisions (D-1 through D-5) + two SA additions accepted by champion (D-6 safety-net dump, D-7 host-equality directionality guard simplified from name-rule per champion direction in same session). Produced before the script itself per Standards §10 incremental approval. |

---

*Disclosure: This document was drafted with the assistance of a large language model (LLM). The project champion has reviewed it. Version: 1.0.0 — Date: 2026-05-19.*
