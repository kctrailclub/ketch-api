#!/usr/bin/env bash
#
# sync_prod_to_staging.sh — Refresh the KCTC staging MySQL DB from production,
# preserving the staging-only Strava Trails Challenge tables.
#
# Design + decisions: scripts/sync_prod_to_staging.README.md v1.0.0
# Backlog item:      BL-0022
# Owner:             Solution Architect (script); Champion (executes)
# Version:           1.0.0  (2026-05-19)
#
# Usage:
#   ./scripts/sync_prod_to_staging.sh \
#     --prod-cnf ~/.my.kctc-prod.cnf \
#     --prod-db railway \
#     --staging-cnf ~/.my.kctc-staging.cnf \
#     --staging-db railway \
#     --workdir /tmp/kctc-sync \
#     [--dry-run] [--no-cleanup] [--yes] [--staging-api-url URL]
#
# Exit codes:
#   0  success
#   1  pre-flight failure (bad args, missing tools, unreadable .cnf, etc.)
#   2  connectivity failure (prod or staging DB unreachable)
#   3  direction guard tripped (prod host == staging host)
#   4  user declined confirmation
#   5  destructive step failed (rollback artifact is staging_backup_$ts.sql)
#   6  smoke check failed (sync completed but verification did not pass)

set -Eeuo pipefail
IFS=$'\n\t'

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

PROD_CNF=""
PROD_DB=""
STAGING_CNF=""
STAGING_DB=""
WORKDIR=""
DRY_RUN=0
NO_CLEANUP=0
ASSUME_YES=0
STAGING_API_URL="${KCTC_STAGING_API_URL:-}"

usage() {
  sed -n '/^# Usage:/,/^$/p' "$0" | sed 's/^# \{0,1\}//'
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prod-cnf)         PROD_CNF="$2"; shift 2;;
    --prod-db)          PROD_DB="$2"; shift 2;;
    --staging-cnf)      STAGING_CNF="$2"; shift 2;;
    --staging-db)       STAGING_DB="$2"; shift 2;;
    --workdir)          WORKDIR="$2"; shift 2;;
    --staging-api-url)  STAGING_API_URL="$2"; shift 2;;
    --dry-run)          DRY_RUN=1; shift;;
    --no-cleanup)       NO_CLEANUP=1; shift;;
    --yes)              ASSUME_YES=1; shift;;
    -h|--help)          usage;;
    *) echo "ERROR: unknown argument: $1" >&2; usage;;
  esac
done

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG_PREFIX=""
log()    { printf '[%s] %s%s\n' "$(date -u +%H:%M:%S)" "$LOG_PREFIX" "$*"; }
log_h()  { printf '\n[%s] ===== %s%s =====\n' "$(date -u +%H:%M:%S)" "$LOG_PREFIX" "$*"; }
die()    { local code="$1"; shift; echo "ERROR: $*" >&2; exit "$code"; }

trap 'rc=$?; if [[ $rc -ne 0 ]]; then echo "FAILED at line $LINENO (exit $rc)" >&2; fi' ERR

# ---------------------------------------------------------------------------
# Step 0: Pre-flight
# ---------------------------------------------------------------------------

log_h "Step 0 — Pre-flight"

# Required args
[[ -n "$PROD_CNF"    ]] || die 1 "--prod-cnf is required"
[[ -n "$PROD_DB"     ]] || die 1 "--prod-db is required"
[[ -n "$STAGING_CNF" ]] || die 1 "--staging-cnf is required"
[[ -n "$STAGING_DB"  ]] || die 1 "--staging-db is required"
[[ -n "$WORKDIR"     ]] || die 1 "--workdir is required"

# Credential files
[[ -r "$PROD_CNF"    ]] || die 1 ".cnf not readable: $PROD_CNF"
[[ -r "$STAGING_CNF" ]] || die 1 ".cnf not readable: $STAGING_CNF"
prod_mode="$(stat -f '%Lp' "$PROD_CNF" 2>/dev/null || stat -c '%a' "$PROD_CNF")"
stag_mode="$(stat -f '%Lp' "$STAGING_CNF" 2>/dev/null || stat -c '%a' "$STAGING_CNF")"
[[ "$prod_mode" == "600" ]] || log "WARN: $PROD_CNF mode is $prod_mode (recommend 600)"
[[ "$stag_mode" == "600" ]] || log "WARN: $STAGING_CNF mode is $stag_mode (recommend 600)"

# Tools
command -v mysql      >/dev/null || die 1 "mysql client not on PATH"
command -v mysqldump  >/dev/null || die 1 "mysqldump not on PATH"

# Workdir
mkdir -p "$WORKDIR"
[[ -w "$WORKDIR" ]] || die 1 "workdir not writable: $WORKDIR"
log "workdir: $WORKDIR"

# Helpers that wrap the credentials
prod_mysql()     { mysql     --defaults-extra-file="$PROD_CNF"    "$@"; }
prod_dump()      { mysqldump --defaults-extra-file="$PROD_CNF"    "$@"; }
staging_mysql()  { mysql     --defaults-extra-file="$STAGING_CNF" "$@"; }
staging_dump()   { mysqldump --defaults-extra-file="$STAGING_CNF" "$@"; }

# Connectivity (also resolves host:port via INFORMATION_SCHEMA / @@hostname check)
log "Checking prod connectivity..."
prod_host="$(prod_mysql -N -B -e "SELECT CONCAT(@@hostname, ':', @@port);" 2>/dev/null)" \
  || die 2 "cannot connect to prod"
log "Checking staging connectivity..."
staging_host="$(staging_mysql -N -B -e "SELECT CONCAT(@@hostname, ':', @@port);" 2>/dev/null)" \
  || die 2 "cannot connect to staging"

# Direction guard (D-7): host:port equality refusal
log "prod    host:port = $prod_host"
log "staging host:port = $staging_host"
[[ "$prod_host" != "$staging_host" ]] || die 3 "DIRECTION GUARD: prod and staging resolve to the same host:port. Refusing."

# Sanity: confirm the named DBs exist
prod_mysql -N -B -e "USE \`$PROD_DB\`; SELECT 1;" >/dev/null \
  || die 2 "prod db '$PROD_DB' unreachable"
staging_mysql -N -B -e "USE \`$STAGING_DB\`; SELECT 1;" >/dev/null \
  || die 2 "staging db '$STAGING_DB' unreachable"

log "Pre-flight OK."

# ---------------------------------------------------------------------------
# Path conventions
# ---------------------------------------------------------------------------

STRAVA_SAVE_SQL="$WORKDIR/strava_save_$TIMESTAMP.sql"
STAGING_BACKUP_SQL="$WORKDIR/staging_backup_$TIMESTAMP.sql"
PROD_DUMP_SQL="$WORKDIR/prod_dump_$TIMESTAMP.sql"
STRAVA_COUNTS_PRE="$WORKDIR/strava_save_counts_$TIMESTAMP.txt"
ORPHAN_LOG="$WORKDIR/orphans_$TIMESTAMP.log"
SUMMARY_LOG="$WORKDIR/summary_$TIMESTAMP.log"
SAVE_SCHEMA="strava_save_$TIMESTAMP"

STRAVA_TABLES=(
  strava_connections
  strava_segments
  strava_trails
  strava_trail_segments
  strava_segment_efforts
)

# In drop order (children first, then parents)
STRAVA_DROP_ORDER=(
  strava_segment_efforts
  strava_trail_segments
  strava_trails
  strava_connections
  strava_segments
)

# ---------------------------------------------------------------------------
# Step 1: Strava side-save
# ---------------------------------------------------------------------------

log_h "Step 1 — Strava side-save (staging → file)"

# Record pre-save counts (used for verification later)
: > "$STRAVA_COUNTS_PRE"
for tbl in "${STRAVA_TABLES[@]}"; do
  cnt="$(staging_mysql -N -B -e "USE \`$STAGING_DB\`; SELECT COUNT(*) FROM \`$tbl\`;")"
  printf '%s\t%s\n' "$tbl" "$cnt" >> "$STRAVA_COUNTS_PRE"
  log "  $tbl: $cnt rows"
done

if [[ $DRY_RUN -eq 1 ]]; then
  log "DRY RUN: would mysqldump 5 strava tables → $STRAVA_SAVE_SQL"
else
  staging_dump \
    --single-transaction \
    --no-tablespaces \
    --skip-add-locks \
    --skip-lock-tables \
    "$STAGING_DB" "${STRAVA_TABLES[@]}" \
    > "$STRAVA_SAVE_SQL"
  log "wrote $STRAVA_SAVE_SQL ($(wc -c < "$STRAVA_SAVE_SQL") bytes)"
fi

# ---------------------------------------------------------------------------
# Step 2: Staging safety-net dump
# ---------------------------------------------------------------------------

log_h "Step 2 — Staging safety-net dump (rollback artifact)"

if [[ $DRY_RUN -eq 1 ]]; then
  log "DRY RUN: would mysqldump full staging → $STAGING_BACKUP_SQL"
else
  staging_dump \
    --single-transaction \
    --no-tablespaces \
    --routines --triggers \
    "$STAGING_DB" \
    > "$STAGING_BACKUP_SQL"
  log "wrote $STAGING_BACKUP_SQL ($(wc -c < "$STAGING_BACKUP_SQL") bytes)"
fi

# ---------------------------------------------------------------------------
# Step 3: Prod dump
# ---------------------------------------------------------------------------

log_h "Step 3 — Prod dump"

if [[ $DRY_RUN -eq 1 ]]; then
  log "DRY RUN: would mysqldump full prod → $PROD_DUMP_SQL"
else
  prod_dump \
    --single-transaction \
    --no-tablespaces \
    --routines --triggers \
    "$PROD_DB" \
    > "$PROD_DUMP_SQL"
  log "wrote $PROD_DUMP_SQL ($(wc -c < "$PROD_DUMP_SQL") bytes)"
fi

# ---------------------------------------------------------------------------
# Step 4: Confirmation
# ---------------------------------------------------------------------------

log_h "Step 4 — Confirmation"

prod_users=$(prod_mysql    -N -B -e "USE \`$PROD_DB\`;    SELECT COUNT(*) FROM users;")
stag_users=$(staging_mysql -N -B -e "USE \`$STAGING_DB\`; SELECT COUNT(*) FROM users;")

cat <<EOF

  About to restore PROD over STAGING.

    prod      $prod_host  db=$PROD_DB     users=$prod_users
    staging   $staging_host  db=$STAGING_DB  users=$stag_users

    safety net : $STAGING_BACKUP_SQL
    strava save: $STRAVA_SAVE_SQL
    prod dump  : $PROD_DUMP_SQL

EOF

if [[ $DRY_RUN -eq 1 ]]; then
  log "DRY RUN: would prompt for confirmation here. Skipping destructive steps. Done."
  exit 0
fi

if [[ $ASSUME_YES -ne 1 ]]; then
  read -rp "Type 'yes' to proceed (anything else aborts): " reply
  [[ "$reply" == "yes" ]] || die 4 "User declined confirmation"
fi

# ---------------------------------------------------------------------------
# Step 5: Drop Strava tables on staging
# ---------------------------------------------------------------------------

log_h "Step 5 — Drop Strava tables on staging"

drop_sql="SET FOREIGN_KEY_CHECKS=0;"
for tbl in "${STRAVA_DROP_ORDER[@]}"; do
  drop_sql="${drop_sql} DROP TABLE IF EXISTS \`$tbl\`;"
done
drop_sql="${drop_sql} SET FOREIGN_KEY_CHECKS=1;"
staging_mysql -e "USE \`$STAGING_DB\`; $drop_sql"
log "dropped 5 Strava tables on staging"

# ---------------------------------------------------------------------------
# Step 6: Restore prod dump onto staging
# ---------------------------------------------------------------------------

log_h "Step 6 — Restore prod dump onto staging"

# Wrap in FK_CHECKS=0 in case mysqldump output order requires it
{
  echo "SET FOREIGN_KEY_CHECKS=0;"
  echo "USE \`$STAGING_DB\`;"
  cat "$PROD_DUMP_SQL"
  echo "SET FOREIGN_KEY_CHECKS=1;"
} | staging_mysql \
  || die 5 "prod restore failed — staging may be inconsistent. Rollback: mysql < $STAGING_BACKUP_SQL"
log "prod data restored on staging"

# ---------------------------------------------------------------------------
# Step 7: Load Strava save into a temporary schema on staging
# ---------------------------------------------------------------------------

log_h "Step 7 — Load Strava save into temporary schema $SAVE_SCHEMA"

staging_mysql -e "CREATE DATABASE \`$SAVE_SCHEMA\`;"
{
  echo "USE \`$SAVE_SCHEMA\`;"
  cat "$STRAVA_SAVE_SQL"
} | staging_mysql
log "loaded Strava save into $SAVE_SCHEMA"

# ---------------------------------------------------------------------------
# Step 8: Re-create empty Strava tables on staging (schema only)
# ---------------------------------------------------------------------------

log_h "Step 8 — Re-create empty Strava tables on staging"

staging_dump --no-data "$SAVE_SCHEMA" "${STRAVA_TABLES[@]}" \
  | staging_mysql "$STAGING_DB"
log "Strava tables re-created on $STAGING_DB (empty)"

# ---------------------------------------------------------------------------
# Step 9: Orphan diagnostics
# ---------------------------------------------------------------------------

log_h "Step 9 — Orphan diagnostics"

: > "$ORPHAN_LOG"

run_orphan_query() {
  local label="$1" query="$2"
  echo "==== $label ====" >> "$ORPHAN_LOG"
  staging_mysql -B -e "$query" >> "$ORPHAN_LOG"
  echo "" >> "$ORPHAN_LOG"
}

# Connections: user_id must resolve in users (INNER JOIN below).
run_orphan_query "strava_connections orphans (user_id not in users)" \
  "SELECT c.connection_id, c.user_id, c.strava_athlete_id
     FROM \`$SAVE_SCHEMA\`.strava_connections c
     LEFT JOIN \`$STAGING_DB\`.users u ON c.user_id = u.user_id
     WHERE u.user_id IS NULL;"

# Segments: created_by may be NULL or must resolve. (Will be coerced to NULL on insert.)
run_orphan_query "strava_segments rows with orphan created_by (coerced to NULL on insert)" \
  "SELECT s.segment_id, s.created_by, s.name
     FROM \`$SAVE_SCHEMA\`.strava_segments s
     LEFT JOIN \`$STAGING_DB\`.users u ON s.created_by = u.user_id
     WHERE s.created_by IS NOT NULL AND u.user_id IS NULL;"

# Trails: created_by may be NULL or must resolve.
run_orphan_query "strava_trails rows with orphan created_by (coerced to NULL on insert)" \
  "SELECT t.trail_id, t.created_by, t.name
     FROM \`$SAVE_SCHEMA\`.strava_trails t
     LEFT JOIN \`$STAGING_DB\`.users u ON t.created_by = u.user_id
     WHERE t.created_by IS NOT NULL AND u.user_id IS NULL;"

log "orphan diagnostics written to $ORPHAN_LOG"

# ---------------------------------------------------------------------------
# Step 10: Insert filtered Strava data
# ---------------------------------------------------------------------------

log_h "Step 10 — Insert filtered Strava data"

# Build column lists dynamically so any future column additions don't break
# the script (column list is the same on both sides — same schema we just
# re-created from the save).
get_cols() {
  local tbl="$1"
  staging_mysql -N -B -e \
    "SELECT GROUP_CONCAT(CONCAT('\`', COLUMN_NAME, '\`') ORDER BY ORDINAL_POSITION SEPARATOR ', ')
     FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = '$STAGING_DB' AND TABLE_NAME = '$tbl';"
}

cols_conn=$(get_cols strava_connections)
cols_seg=$(get_cols strava_segments)
cols_trail=$(get_cols strava_trails)
cols_ts=$(get_cols strava_trail_segments)
cols_eff=$(get_cols strava_segment_efforts)

# Build per-row SELECT column lists for the FILTERED inserts. For segments and
# trails, coerce orphan created_by to NULL via a CASE. For others, straight copy.
# For connections, INNER JOIN drops orphans.

# Helper: build "src.col1, src.col2, ..." with optional override for created_by.
build_select_cols() {
  local cols_csv="$1" prefix="$2" override_col="$3" override_expr="$4"
  awk -v pfx="$prefix" -v ocol="$override_col" -v oexpr="$override_expr" '
    BEGIN { RS = ", "; ORS = "" }
    {
      gsub(/`/, "", $0)
      gsub(/[\n\r ]/, "", $0)
      col=$0
      if (col == "") next
      if (out != "") printf ", "
      if (col == ocol) printf "%s", oexpr
      else             printf "%s.`%s`", pfx, col
      out = "x"
    }
  ' <<< "$cols_csv"
}

sel_conn=$(  build_select_cols "$cols_conn"  "c" "" "")
sel_seg=$(   build_select_cols "$cols_seg"   "s" "created_by" "CASE WHEN u.user_id IS NULL THEN NULL ELSE s.\`created_by\` END")
sel_trail=$( build_select_cols "$cols_trail" "t" "created_by" "CASE WHEN u.user_id IS NULL THEN NULL ELSE t.\`created_by\` END")
sel_ts=$(    build_select_cols "$cols_ts"    "ts" "" "")
sel_eff=$(   build_select_cols "$cols_eff"   "e" "" "")

# Run all five INSERTs in one connection inside a single transaction so a
# failure mid-way rolls back the partial reload.
staging_mysql <<SQL
USE \`$STAGING_DB\`;
SET FOREIGN_KEY_CHECKS = 0;
START TRANSACTION;

INSERT INTO strava_connections ($cols_conn)
  SELECT $sel_conn
    FROM \`$SAVE_SCHEMA\`.strava_connections c
    INNER JOIN \`$STAGING_DB\`.users u ON c.user_id = u.user_id;

INSERT INTO strava_segments ($cols_seg)
  SELECT $sel_seg
    FROM \`$SAVE_SCHEMA\`.strava_segments s
    LEFT JOIN \`$STAGING_DB\`.users u ON s.created_by = u.user_id;

INSERT INTO strava_trails ($cols_trail)
  SELECT $sel_trail
    FROM \`$SAVE_SCHEMA\`.strava_trails t
    LEFT JOIN \`$STAGING_DB\`.users u ON t.created_by = u.user_id;

INSERT INTO strava_trail_segments ($cols_ts)
  SELECT $sel_ts
    FROM \`$SAVE_SCHEMA\`.strava_trail_segments ts
    INNER JOIN \`$STAGING_DB\`.strava_trails t   ON ts.trail_id   = t.trail_id
    INNER JOIN \`$STAGING_DB\`.strava_segments s ON ts.segment_id = s.segment_id;

INSERT INTO strava_segment_efforts ($cols_eff)
  SELECT $sel_eff
    FROM \`$SAVE_SCHEMA\`.strava_segment_efforts e
    INNER JOIN \`$STAGING_DB\`.strava_connections c ON e.connection_id = c.connection_id
    INNER JOIN \`$STAGING_DB\`.strava_segments s    ON e.segment_id    = s.segment_id;

COMMIT;
SET FOREIGN_KEY_CHECKS = 1;
SQL

log "filtered Strava data inserted"

# ---------------------------------------------------------------------------
# Step 11: Smoke checks
# ---------------------------------------------------------------------------

log_h "Step 11 — Smoke checks"

: > "$SUMMARY_LOG"
fail=0

# 11a: Row-count parity prod vs staging on core tables
declare -a CORE_TABLES=(users households projects hours audit_logs refresh_tokens settings reward_emails reward_tags)
echo "== prod vs staging row counts ==" | tee -a "$SUMMARY_LOG"
for tbl in "${CORE_TABLES[@]}"; do
  p=$(prod_mysql    -N -B -e "USE \`$PROD_DB\`;    SELECT COUNT(*) FROM \`$tbl\`;")
  s=$(staging_mysql -N -B -e "USE \`$STAGING_DB\`; SELECT COUNT(*) FROM \`$tbl\`;")
  diff=$(( p - s ))
  printf '  %-18s prod=%-8s staging=%-8s diff=%s\n' "$tbl" "$p" "$s" "$diff" | tee -a "$SUMMARY_LOG"
  if [[ "$p" != "$s" ]]; then
    log "WARN: $tbl row count differs (prod=$p, staging=$s)"
    fail=1
  fi
done

# 11b: Strava reload counts vs pre-save (minus orphans)
echo "== strava reload counts (pre-save vs post-reload) ==" | tee -a "$SUMMARY_LOG"
while IFS=$'\t' read -r tbl pre; do
  post=$(staging_mysql -N -B -e "USE \`$STAGING_DB\`; SELECT COUNT(*) FROM \`$tbl\`;")
  delta=$(( pre - post ))
  printf '  %-26s pre=%-6s post=%-6s skipped=%s\n' "$tbl" "$pre" "$post" "$delta" | tee -a "$SUMMARY_LOG"
done < "$STRAVA_COUNTS_PRE"

# 11c: Staging API health
if [[ -n "$STAGING_API_URL" ]]; then
  echo "== staging API health ==" | tee -a "$SUMMARY_LOG"
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$STAGING_API_URL" || true)
  echo "  GET $STAGING_API_URL  →  HTTP $code" | tee -a "$SUMMARY_LOG"
  [[ "$code" == "200" ]] || { log "WARN: staging API did not return 200"; fail=1; }
else
  echo "  (skipped — no --staging-api-url / KCTC_STAGING_API_URL set)" | tee -a "$SUMMARY_LOG"
fi

# ---------------------------------------------------------------------------
# Step 12: Cleanup
# ---------------------------------------------------------------------------

log_h "Step 12 — Cleanup"

if [[ $NO_CLEANUP -eq 1 ]]; then
  log "skipping cleanup (--no-cleanup)"
else
  log "dropping temporary schema $SAVE_SCHEMA"
  staging_mysql -e "DROP DATABASE \`$SAVE_SCHEMA\`;"
  log "removing prod dump $PROD_DUMP_SQL"
  rm -f "$PROD_DUMP_SQL"
fi

log "retained artifacts:"
log "  safety-net dump : $STAGING_BACKUP_SQL"
log "  strava save      : $STRAVA_SAVE_SQL"
log "  orphan log       : $ORPHAN_LOG"
log "  summary log      : $SUMMARY_LOG"

# ---------------------------------------------------------------------------
# Step 13: Final report
# ---------------------------------------------------------------------------

log_h "Step 13 — Final report"

if [[ $fail -eq 0 ]]; then
  log "SUCCESS — sync completed; smoke checks passed."
  log "Now run the post-run manual checklist from scripts/sync_prod_to_staging.README.md §9."
  exit 0
else
  log "COMPLETED WITH WARNINGS — sync ran but one or more smoke checks did not pass."
  log "Review $SUMMARY_LOG and the post-run manual checklist before declaring success."
  exit 6
fi
