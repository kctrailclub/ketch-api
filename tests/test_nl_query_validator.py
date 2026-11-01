"""SEC-0020 — NL Query validator: credential columns and wildcard projections.

Exercises the real _validate_sql() (no mocks, no DB).  Includes SEC-0012
table-allowlist regressions and the standard report shapes that must keep working.
"""
import pytest
from fastapi import HTTPException

from app.routers.nl_query import MAX_ROWS, _validate_sql


def _rejected(sql):
    with pytest.raises(HTTPException) as exc:
        _validate_sql(sql)
    assert exc.value.status_code == 400
    return exc.value.detail


class TestCredentialColumnsRejected:
    """Credential columns are unreachable regardless of query shape."""

    @pytest.mark.parametrize("sql", [
        # projection
        "SELECT password_hash FROM users",
        "SELECT email, invite_token, invite_expires FROM users WHERE is_admin = 1",
        # case and quoting
        "SELECT `PASSWORD_HASH` AS x FROM users",
        "SELECT PassWord_Hash FROM users",
        "SELECT users.`Invite_Token` FROM users",
        "SELECT mydb.users.password_hash FROM users",
        # alias-qualified
        "SELECT u.invite_token FROM users u",
        "SELECT t.password_hash FROM users AS t",
        # function arguments and aggregates
        "SELECT CONCAT(email, ':', password_hash) FROM users",
        "SELECT GROUP_CONCAT(password_hash) FROM users",
        "SELECT JSON_OBJECT('a', invite_token) FROM users",
        "SELECT CAST(password_hash AS CHAR) FROM users",
        "SELECT COUNT(*) OVER (PARTITION BY invite_token) FROM users",
        # subqueries
        "SELECT a FROM (SELECT password_hash AS a FROM users) t",
        "SELECT email FROM users WHERE (SELECT invite_token FROM users LIMIT 1) = 'x'",
        "SELECT a FROM (SELECT email AS a FROM users UNION SELECT password_hash FROM users) t",
        # predicate and clause positions (boolean/blind-extraction oracles)
        "SELECT email FROM users WHERE password_hash LIKE '$2b$%'",
        "SELECT email FROM users WHERE invite_token IS NOT NULL",
        "SELECT COUNT(*) FROM users WHERE invite_token LIKE 'a%'",
        "SELECT email FROM users WHERE ASCII(SUBSTRING(invite_token, 1, 1)) > 90",
        "SELECT email FROM users ORDER BY invite_token",
        "SELECT email FROM users GROUP BY email, invite_token",
        "SELECT email FROM users GROUP BY email HAVING MAX(invite_token) > 'a'",
        "SELECT a.email FROM users a JOIN users b ON a.invite_token = b.invite_token",
        # JOIN ... USING is an Identifier, not a Column, in sqlglot
        "SELECT a.email FROM users a JOIN users b USING (invite_token)",
        # forbidden name used as an alias fails closed
        "SELECT email AS invite_token FROM users",
    ])
    def test_rejected(self, sql):
        assert "not allowed" in _rejected(sql)


class TestWildcardProjectionRejected:
    """SELECT * would return credential columns without naming them."""

    @pytest.mark.parametrize("sql", [
        "SELECT * FROM users",
        "SELECT u.* FROM users u",
        "SELECT users.* FROM users",
        "SELECT `users`.* FROM `users`",
        "SELECT DISTINCT * FROM users",
        "SELECT * FROM projects",
        "SELECT name FROM (SELECT * FROM users) t",
        "SELECT x.* FROM (SELECT user_id FROM users) x",
        "SELECT email FROM users WHERE user_id IN (SELECT * FROM users)",
        "SELECT email, (SELECT * FROM users LIMIT 1) FROM users",
        "SELECT * FROM users u JOIN hours h ON h.member_id = u.user_id",
        "SELECT users.*, hours.hours FROM users JOIN hours ON 1 = 1",
    ])
    def test_rejected(self, sql):
        assert "SELECT *" in _rejected(sql)

    def test_count_star_allowed(self):
        assert "COUNT(*)" in _validate_sql("SELECT COUNT(*) FROM users WHERE is_active = 1")


class TestTableAllowlist:
    """SEC-0012 regression — table references checked at any depth.

    Credential-bearing tables outside the allowlist (refresh_tokens,
    strava_connections) stay unreachable.
    """

    @pytest.mark.parametrize("sql", [
        "SELECT action FROM audit_logs",
        "SELECT action FROM `audit_logs`",
        "WITH t AS (SELECT action FROM audit_logs) SELECT action FROM t",
        "WITH t AS (SELECT email FROM users) SELECT email FROM t",
        "SELECT x FROM (SELECT action AS x FROM audit_logs) sub",
        "SELECT column_name FROM information_schema.columns",
        "SELECT user FROM mysql.user",
        "SELECT email FROM users WHERE EXISTS (SELECT 1 FROM refresh_tokens)",
        "SELECT 1 FROM users WHERE EXISTS (SELECT 1 FROM strava_connections)",
        "SELECT access_token FROM strava_connections",
    ])
    def test_rejected(self, sql):
        _rejected(sql)


class TestStatementShape:
    @pytest.mark.parametrize("sql", [
        "DELETE FROM hours",
        "SELECT name FROM projects; DELETE FROM hours",
        "SELECT SLEEP(5)",
        "SELECT name FROM projects INTO OUTFILE '/tmp/x'",
        "SELECT email FROM users UNION SELECT invite_token FROM users",
    ])
    def test_rejected(self, sql):
        _rejected(sql)


class TestCommentsAreInert:
    """MySQL executes /*! ... */ comments; the regenerated SQL must not preserve that form."""

    @pytest.mark.parametrize("sql", [
        "SELECT email FROM users /*! , invite_token */",
        "SELECT email /*!50000 ,password_hash */ FROM users",
        "SELECT email FROM users -- x */ UNION SELECT invite_token FROM users",
    ])
    def test_comment_neutralised(self, sql):
        out = _validate_sql(sql)
        assert "/*!" not in out
        assert out.count("*/") == out.count("/*")


class TestLiteralsNotConfusedWithColumns:
    def test_string_literal_mentioning_column_name_allowed(self):
        _validate_sql("SELECT email FROM users WHERE firstname LIKE '%password_hash%'")


class TestStandardReportQuestions:
    """The report shapes admins actually ask for must keep validating."""

    def test_hours_by_member(self):
        sql = _validate_sql(
            "SELECT CONCAT(u.firstname, ' ', u.lastname) AS member, SUM(h.hours) AS total "
            "FROM hours h JOIN users u ON h.member_id = u.user_id "
            "WHERE h.status = 'approved' GROUP BY member ORDER BY total DESC LIMIT 10"
        )
        assert "LIMIT 10" in sql

    def test_hours_by_project(self):
        _validate_sql(
            "SELECT p.name, SUM(h.hours) AS total FROM hours h "
            "JOIN projects p ON h.project_id = p.project_id "
            "WHERE h.status = 'approved' GROUP BY p.name ORDER BY total DESC"
        )

    def test_ytd_totals(self):
        _validate_sql(
            "SELECT SUM(hours) AS total, COUNT(DISTINCT member_id) AS members FROM hours "
            "WHERE status = 'approved' AND YEAR(service_date) = 2026"
        )

    def test_member_contact_list(self):
        _validate_sql(
            "SELECT u.firstname, u.lastname, u.email, u.phone, h.name AS household "
            "FROM users u LEFT JOIN households h ON u.household_id = h.household_id "
            "WHERE u.is_active = 1 ORDER BY u.lastname"
        )

    def test_member_contact_columns_unqualified(self):
        _validate_sql("SELECT firstname, lastname, email, phone, waiver FROM users LIMIT 20")

    def test_limit_added_when_missing(self):
        assert f"LIMIT {MAX_ROWS}" in _validate_sql("SELECT name FROM projects")

    def test_limit_capped(self):
        assert f"LIMIT {MAX_ROWS}" in _validate_sql("SELECT name FROM projects LIMIT 100000")
