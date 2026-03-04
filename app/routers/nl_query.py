import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

import anthropic

from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import get_current_admin
from app.models.models import User

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
- Always include a LIMIT clause (max 500 rows).
- Return ONLY the raw SQL query, no explanation, no markdown fences, no extra text.
""".strip()

ALLOWED_TABLES = {"hours", "users", "projects", "households"}

# Patterns that indicate a write operation — rejected outright.
FORBIDDEN_PATTERNS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|REPLACE|RENAME"
    r"|GRANT|REVOKE|CALL|EXEC|EXECUTE|LOAD"
    r"|INTO\s+OUTFILE|INTO\s+DUMPFILE)\b",
    re.IGNORECASE,
)

MAX_ROWS = 500


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

def _generate_sql(question: str) -> str:
    """Call Claude API to convert a natural-language question into SQL."""
    if not settings.anthropic_api_key:
        raise HTTPException(
            status_code=503,
            detail="Natural language query is not configured. "
                   "Set ANTHROPIC_API_KEY in the server environment.",
        )

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    message = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1024,
        messages=[
            {
                "role": "user",
                "content": f"{SCHEMA_CONTEXT}\n\nQuestion: {question}",
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
    """Validate that the SQL is a safe, read-only SELECT statement."""
    # Must start with SELECT
    if not re.match(r"^\s*SELECT\b", sql, re.IGNORECASE):
        raise HTTPException(status_code=400, detail="Only SELECT queries are allowed.")

    # Check for forbidden write operations
    if FORBIDDEN_PATTERNS.search(sql):
        raise HTTPException(status_code=400, detail="Query contains forbidden operations.")

    # Only allowed tables may appear in FROM / JOIN clauses
    table_refs = re.findall(r"\b(?:FROM|JOIN)\s+(\w+)", sql, re.IGNORECASE)
    for table in table_refs:
        if table.lower() not in ALLOWED_TABLES:
            raise HTTPException(
                status_code=400,
                detail=f"Query references table '{table}' which is not allowed. "
                       f"Allowed tables: {', '.join(sorted(ALLOWED_TABLES))}.",
            )

    # Ensure a LIMIT clause exists; if not, append one
    if not re.search(r"\bLIMIT\s+\d+", sql, re.IGNORECASE):
        sql = sql.rstrip(";").strip() + f" LIMIT {MAX_ROWS}"

    # Cap an existing LIMIT if it exceeds MAX_ROWS
    limit_match = re.search(r"\bLIMIT\s+(\d+)", sql, re.IGNORECASE)
    if limit_match and int(limit_match.group(1)) > MAX_ROWS:
        sql = re.sub(r"\bLIMIT\s+\d+", f"LIMIT {MAX_ROWS}", sql, flags=re.IGNORECASE)

    return sql


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
    _admin: User = Depends(get_current_admin),
):
    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question cannot be empty.")
    if len(question) > 1000:
        raise HTTPException(status_code=400, detail="Question is too long (max 1000 characters).")

    # Step 1 — Generate SQL from the question
    try:
        raw_sql = _generate_sql(question)
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

    # Step 2 — Validate the generated SQL
    try:
        safe_sql = _validate_sql(raw_sql)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Generated SQL could not be validated.")

    # Step 3 — Execute read-only
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

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Query execution failed: {str(e)}")
    finally:
        # Never commit on this endpoint
        db.rollback()

    return NLQueryResponse(
        sql=safe_sql,
        columns=columns,
        rows=rows,
        row_count=len(rows),
    )
