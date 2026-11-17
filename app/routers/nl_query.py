import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

import sqlglot
import sqlglot.expressions as exp

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.orm import Session

import anthropic

from app.core.audit import log_action
from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import get_current_admin
from app.models.models import NlQueryUsage, User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reports", tags=["reports"])


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Only these tables may be queried.  The schema context sent to the LLM
# deliberately omits sensitive columns (password_hash, invite_token, etc.).
SCHEMA_CONTEXT = """
You are a SQL assistant for a MySQL database that tracks volunteer hours for a trail club.
Generate a single SELECT query to answer the user's question. Use only these tables and columns:

TABLE households (
  household_id   INT PRIMARY KEY,
  household_code VARCHAR(10),
  name           VARCHAR(100),
  address        VARCHAR(255),
  primary_user_id INT  -- FK to users.user_id
);

TABLE users (
  user_id      INT PRIMARY KEY,
  firstname    VARCHAR(100),
  lastname     VARCHAR(100),
  email        VARCHAR(255),
  phone        VARCHAR(20),
  household_id INT,          -- FK to households.household_id
  is_admin     INT,          -- 1=admin, 0=regular
  is_active    INT,          -- 1=active, 0=inactive
  waiver       DATE,
  youth        INT           -- 1=youth member, 0=adult
);

TABLE projects (
  project_id   INT PRIMARY KEY,
  name         VARCHAR(255),
  notes        TEXT,
  project_type ENUM('one_time','ongoing'),
  end_date     DATE
);

TABLE hours (
  hour_id      INT PRIMARY KEY,
  member_id    INT,          -- FK to users.user_id (the volunteer)
  project_id   INT,          -- FK to projects.project_id
  logged_by    INT,          -- FK to users.user_id (who submitted the entry)
  service_date DATE,
  hours        DECIMAL(5,2),
  notes        VARCHAR(255),
  status       ENUM('pending','approved','rejected'),
  status_note  VARCHAR(255),
  status_by    INT,          -- FK to users.user_id (admin who reviewed)
  status_updated DATETIME,
  created      DATETIME,
  updated      DATETIME
);

Rules:
- Generate ONLY a single SELECT statement. No INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, or TRUNCATE.
- Do NOT use subqueries that modify data.
- When asked about "hours" or "total hours", use only hours with status = 'approved' unless the user specifically asks about pending or rejected hours.
- To get a member's full name, use CONCAT(users.firstname, ' ', users.lastname).
- Join hours to users via hours.member_id = users.user_id.
- Join hours to projects via hours.project_id = projects.project_id.
- Join users to households via users.household_id = households.household_id.
- When filtering by project name, member name, or household name, use LIKE with wildcards (e.g. WHERE p.name LIKE '%keyword%') instead of exact matching, unless the user clearly provides the full exact name.
- Never use SELECT * or table.*; always list the columns you need explicitly.
- If you use SELECT DISTINCT, every column or expression in ORDER BY must also appear in the SELECT list; otherwise omit DISTINCT.
- Always include a LIMIT clause (max 500 rows).
- Return ONLY the raw SQL query, no explanation, no markdown fences, no extra text.
""".strip()

ALLOWED_TABLES = {"hours", "users", "projects", "households"}

MAX_ROWS = 500

# Functions that are dangerous even inside SELECT — rejected at AST level.
_FORBIDDEN_FUNCS = frozenset({"load_file", "benchmark", "sleep", "get_lock"})

# Credential columns on allowlisted tables (SEC-0020).  Rejected wherever the
# identifier appears: projection, WHERE, JOIN ... ON/USING, GROUP BY, ORDER BY,
# function arguments, subqueries.  invite_token doubles as the password-reset
# token, so reading it allows account takeover.  Add any new secret-bearing
# column here.
_FORBIDDEN_COLUMNS = frozenset({"password_hash", "invite_token", "invite_expires"})


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class NLQueryRequest(BaseModel):
    question: str


class NLQueryResponse(BaseModel):
    sql: str
    columns: list[str]
    rows: list[list]
    row_count: int
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# MySQL ER_FIELD_IN_ORDER_NOT_SELECT: "Expression #N of ORDER BY clause is not in
# SELECT list ... incompatible with DISTINCT".  A model-authored statement can
# pass validation and still be rejected by MySQL for this reason (BR-0011).
_MYSQL_ERR_ORDER_BY_NOT_IN_DISTINCT_SELECT = 3065


def _generate_sql(question: str, failed_sql: Optional[str] = None) -> str:
    """Call Claude API to convert a natural-language question into SQL.

    ``failed_sql`` is set only on the single MySQL 3065 retry: the earlier,
    already-validated statement is shown to the model with a fixed instruction.
    The database error text is deliberately not forwarded.
    """
    if not settings.anthropic_api_key:
        raise HTTPException(
            status_code=503,
            detail="Natural language query is not configured. "
                   "Set ANTHROPIC_API_KEY in the server environment.",
        )

    content = f"{SCHEMA_CONTEXT}\n\nQuestion: {question}"
    if failed_sql is not None:
        content += (
            "\n\nYour previous query was rejected by MySQL because it used SELECT DISTINCT "
            "with ORDER BY columns that are not in the SELECT list:\n"
            f"{failed_sql}\n"
            "Rewrite it so every ORDER BY column also appears in the SELECT list, "
            "or remove DISTINCT. Follow all the rules above."
        )

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    message = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1024,
        messages=[
            {
                "role": "user",
                "content": content,
            }
        ],
    )

    sql = message.content[0].text.strip()

    # Strip markdown code fences if the model included them
    if sql.startswith("```"):
        sql = re.sub(r"^```(?:sql)?\s*", "", sql)
        sql = re.sub(r"\s*```$", "", sql)

    return sql.strip()


def _validate_sql(sql: str) -> str:
    """Validate that the SQL is a safe, read-only SELECT statement using AST analysis."""
    # Parse — reject unparseable SQL immediately.
    try:
        statements = sqlglot.parse(sql, dialect="mysql", error_level=sqlglot.ErrorLevel.RAISE)
    except sqlglot.errors.ParseError:
        raise HTTPException(status_code=400, detail="Generated SQL could not be parsed.")

    # Reject stacked statements (defense-in-depth against statement-terminator tricks).
    if len(statements) != 1 or statements[0] is None:
        raise HTTPException(status_code=400, detail="Only a single SELECT statement is allowed.")

    stmt = statements[0]

    # Must be a top-level SELECT.
    if not isinstance(stmt, exp.Select):
        raise HTTPException(status_code=400, detail="Only SELECT queries are allowed.")

    # Block SELECT ... INTO OUTFILE / INTO DUMPFILE.
    if stmt.args.get("into"):
        raise HTTPException(status_code=400, detail="INTO clause is not allowed.")

    # Walk every table reference at any depth: subqueries, CTEs, WHERE EXISTS, etc.
    # sqlglot normalises backtick-quoted identifiers so `audit_logs` → name="audit_logs".
    for table in stmt.find_all(exp.Table):
        if table.db:
            # Schema-qualified reference: information_schema.columns → db="information_schema".
            raise HTTPException(
                status_code=400,
                detail="Schema-qualified table references are not allowed.",
            )
        if table.name.lower() not in ALLOWED_TABLES:
            raise HTTPException(
                status_code=400,
                detail=f"Table '{table.name}' is not allowed. "
                       f"Allowed: {', '.join(sorted(ALLOWED_TABLES))}.",
            )

    # Reject credential columns at any depth (SEC-0020).  Checking every Identifier
    # (not just exp.Column) also covers JOIN ... USING (invite_token), which sqlglot
    # does not parse as a Column.  Backticks are normalised away and MySQL column
    # names are case-insensitive, so compare stripped and lower-cased.  An alias
    # with a forbidden name is rejected too; that fails closed.
    for ident in stmt.find_all(exp.Identifier):
        if ident.name.strip().lower() in _FORBIDDEN_COLUMNS:
            raise HTTPException(
                status_code=400,
                detail=f"Column '{ident.name}' is not allowed.",
            )

    # Reject wildcard projections (SELECT *, SELECT t.*) at any depth: they would
    # return credential columns without naming them.  COUNT(*) is the only
    # permitted use of a star.
    for star in stmt.find_all(exp.Star):
        if not isinstance(star.parent, exp.Count):
            raise HTTPException(
                status_code=400,
                detail="SELECT * is not allowed; name the columns explicitly.",
            )

    # Reject dangerous functions (parsed as Anonymous nodes in sqlglot/MySQL dialect).
    for node in stmt.find_all(exp.Anonymous):
        if node.name.lower() in _FORBIDDEN_FUNCS:
            raise HTTPException(
                status_code=400,
                detail=f"Function '{node.name}' is not allowed.",
            )

    # Ensure a LIMIT clause exists; cap it at MAX_ROWS if present but too large.
    limit_node = stmt.find(exp.Limit)
    if limit_node is None:
        stmt = stmt.limit(MAX_ROWS)
    else:
        try:
            if int(limit_node.expression.this) > MAX_ROWS:
                limit_node.set("expression", exp.Literal.number(MAX_ROWS))
        except (AttributeError, ValueError, TypeError):
            limit_node.set("expression", exp.Literal.number(MAX_ROWS))

    # Return the AST-regenerated SQL — normalises away encoding tricks.
    return stmt.sql(dialect="mysql")


def _generate_sql_or_http_error(question: str, failed_sql: Optional[str] = None) -> str:
    """_generate_sql with provider failures mapped to HTTP errors."""
    try:
        return _generate_sql(question, failed_sql)
    except HTTPException:
        raise
    except anthropic.AuthenticationError as e:
        raise HTTPException(status_code=502, detail=f"Anthropic API authentication failed: {str(e)}")
    except anthropic.APIConnectionError as e:
        raise HTTPException(status_code=502, detail=f"Cannot reach Anthropic API: {str(e)}")
    except anthropic.APIStatusError as e:
        raise HTTPException(status_code=502, detail=f"Anthropic API error ({e.status_code}): {e.message}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to generate query: {type(e).__name__}: {str(e)}")


def _validate_or_400(raw_sql: str) -> str:
    """_validate_sql with any unexpected failure mapped to a generic 400."""
    try:
        return _validate_sql(raw_sql)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Generated SQL could not be validated.")


def _run_select(db: Session, safe_sql: str) -> tuple[list[str], list[list]]:
    """Execute validated SQL read-only and return (columns, rows).

    Rolls back in all cases; nothing is ever committed.  Exceptions from the
    driver propagate to the caller.
    """
    try:
        result = db.execute(text(safe_sql))
        columns = list(result.keys())
        rows = [list(row) for row in result.fetchall()]

        # Convert non-serialisable types to strings
        for i, row in enumerate(rows):
            for j, val in enumerate(row):
                if hasattr(val, "isoformat"):
                    rows[i][j] = val.isoformat()
                elif isinstance(val, (bytes, bytearray)):
                    rows[i][j] = val.decode("utf-8", errors="replace")
                elif val is not None and not isinstance(val, (str, int, float, bool)):
                    rows[i][j] = str(val)
        return columns, rows
    finally:
        db.rollback()  # never commit SQL side effects


def _is_order_by_not_in_distinct_error(exc: Exception) -> bool:
    """True if exc wraps MySQL error 3065 (driver error number in orig.args[0])."""
    args = getattr(getattr(exc, "orig", None), "args", None)
    return bool(args) and args[0] == _MYSQL_ERR_ORDER_BY_NOT_IN_DISTINCT_SELECT


def _check_quota(db: Session, user_id: int) -> None:
    """Raise HTTP 429 if this admin has reached their daily NL Query limit."""
    today = date.today()
    row = db.execute(
        select(NlQueryUsage).where(
            NlQueryUsage.user_id == user_id,
            NlQueryUsage.query_date == today,
        )
    ).scalar_one_or_none()

    limit = settings.nl_query_daily_limit
    if row is not None and row.count >= limit:
        midnight = datetime.combine(today + timedelta(days=1), time.min, tzinfo=timezone.utc)
        retry_after = max(0, int((midnight - datetime.now(timezone.utc)).total_seconds()))
        raise HTTPException(
            status_code=429,
            detail=f"Daily NL Query limit of {limit} reached. Resets at UTC midnight.",
            headers={"Retry-After": str(retry_after)},
        )


def _increment_quota(db: Session, user_id: int) -> None:
    """Increment this admin's daily NL Query counter (call after successful query)."""
    today = date.today()
    row = db.execute(
        select(NlQueryUsage).where(
            NlQueryUsage.user_id == user_id,
            NlQueryUsage.query_date == today,
        )
    ).scalar_one_or_none()

    if row is None:
        db.add(NlQueryUsage(user_id=user_id, query_date=today, count=1))
    else:
        row.count += 1


def _check_daily_alert(db: Session) -> None:
    """Log a warning when total daily NL Query calls across all admins hit the alert threshold."""
    today = date.today()
    total = db.execute(
        text("SELECT COALESCE(SUM(count), 0) FROM nl_query_usage WHERE query_date = :d"),
        {"d": today},
    ).scalar() or 0

    threshold = settings.nl_query_alert_threshold
    if total >= threshold:
        logger.warning(
            "NL Query alert: %d total calls today (%s) across all admins has reached the "
            "configured threshold of %d. Review Anthropic API usage at console.anthropic.com.",
            total,
            today,
            threshold,
        )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/query/health")
def query_health_check(
    _admin: User = Depends(get_current_admin),
):
    """Diagnostic endpoint to test Anthropic API connectivity."""
    result = {
        "api_key_set": bool(settings.anthropic_api_key),
        "api_key_prefix": settings.anthropic_api_key[:12] + "..." if settings.anthropic_api_key else None,
        "model": "claude-haiku-4-5",
    }

    if not settings.anthropic_api_key:
        result["error"] = "ANTHROPIC_API_KEY is not set"
        return result

    try:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        message = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=10,
            messages=[{"role": "user", "content": "Reply with OK"}],
        )
        result["status"] = "ok"
        result["response"] = message.content[0].text
    except anthropic.AuthenticationError as e:
        result["error"] = f"AuthenticationError: {str(e)}"
    except anthropic.APIConnectionError as e:
        result["error"] = f"APIConnectionError: {str(e)}"
    except anthropic.APIStatusError as e:
        result["error"] = f"APIStatusError ({e.status_code}): {e.message}"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {str(e)}"

    return result


@router.post("/query")
def natural_language_query(
    payload: NLQueryRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question cannot be empty.")
    if len(question) > 1000:
        raise HTTPException(status_code=400, detail="Question is too long (max 1000 characters).")

    # Step 1 — Quota check (before spending Anthropic credits)
    _check_quota(db, admin.user_id)

    # Step 2 — Generate SQL from the question
    raw_sql = _generate_sql_or_http_error(question)

    # Step 3 — Validate the generated SQL
    safe_sql = _validate_or_400(raw_sql)

    # Step 4 — Execute read-only (rolled back inside _run_select).  A statement that
    # passes validation can still be rejected by MySQL with error 3065 (DISTINCT with
    # ORDER BY on unselected columns; BR-0011).  Only that error gets ONE regeneration;
    # the replacement goes through Steps 3-4 in full, so every statement that reaches
    # db.execute has passed _validate_sql().  The retry is not a loop and does not
    # touch the quota: usage is incremented once, in Step 5, on success only.
    try:
        columns, rows = _run_select(db, safe_sql)
    except Exception as e:
        if not _is_order_by_not_in_distinct_error(e):
            raise HTTPException(status_code=400, detail=f"Query execution failed: {str(e)}")
        # WARNING, not INFO: the app configures no logging, so under uvicorn only
        # WARNING+ from this logger reaches the platform log (EP-0054).  Fixed text and
        # the admin id only: no SQL, question, or driver message.  A "regenerating once"
        # line with no outcome line after it means the retry failed before execution
        # (generation or validation raised an HTTP error).
        logger.warning("NL Query: MySQL 3065 on first attempt; regenerating once (admin %s)", admin.user_id)
        raw_sql = _generate_sql_or_http_error(question, failed_sql=safe_sql)
        safe_sql = _validate_or_400(raw_sql)
        try:
            columns, rows = _run_select(db, safe_sql)
        except Exception as e2:
            logger.warning("NL Query: 3065 retry failed at execution (admin %s)", admin.user_id)
            raise HTTPException(status_code=400, detail=f"Query execution failed: {str(e2)}")
        logger.warning("NL Query: 3065 retry succeeded (admin %s)", admin.user_id)

    # Step 5 — Record quota usage and audit log (after successful execution and rollback)
    _increment_quota(db, admin.user_id)
    log_action(
        db,
        user_id=admin.user_id,
        action="nl_query",
        entity_type="reports",
        details={"question": question, "sql": safe_sql, "row_count": len(rows)},
    )
    db.commit()
    _check_daily_alert(db)

    return NLQueryResponse(
        sql=safe_sql,
        columns=columns,
        rows=rows,
        row_count=len(rows),
    )
