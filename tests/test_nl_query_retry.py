"""BL-0051 / BR-0011 — MySQL 3065 handling in POST /reports/query.

Drives the real endpoint with _generate_sql replaced by a scripted sequence and
a DB session whose execute() raises a MySQL-shaped 3065 for chosen statements.
No network, no MySQL.  These tests prove the control flow (one retry, validation
of the retried SQL, quota, error text); they cannot prove what the live model
writes or that MySQL accepts a given statement.
"""
import pymysql.err
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from app.core.database import get_db
from app.core.dependencies import get_current_admin
from app.main import app
from app.models.models import AuditLog, NlQueryUsage, User
from app.routers import nl_query

BAD = ("SELECT DISTINCT u.user_id, CONCAT(u.firstname, ' ', u.lastname) AS member_name "
       "FROM users AS u ORDER BY u.lastname, u.firstname LIMIT 500")
GOOD = ("SELECT DISTINCT u.user_id, u.lastname, u.firstname FROM users AS u "
        "ORDER BY u.lastname, u.firstname LIMIT 500")


def _mysql_error(number):
    orig = pymysql.err.OperationalError(number, "driver message")
    return OperationalError("stmt", {}, orig)


class _Session:
    """Delegates to the real session; raises for statements in `fail` (by text)."""

    def __init__(self, real, fail):
        self._real, self._fail, self.executed = real, fail, []

    def execute(self, stmt, *a, **kw):
        sql = str(stmt)
        if isinstance(stmt, type(text(""))):
            if "nl_query_usage" not in sql:   # ignore the daily-alert bookkeeping query
                self.executed.append(sql)
            if sql in self._fail:
                raise self._fail[sql]
        return self._real.execute(stmt, *a, **kw)

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture
def admin(db_session):
    u = User(firstname="A", lastname="Admin", email="a@example.com",
             password_hash="x", is_active=1, is_admin=1)
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture
def harness(db_session, admin, monkeypatch):
    """Returns a function(generated, fail) -> (client_response_fn, calls, session)."""
    from fastapi.testclient import TestClient

    def build(generated, fail):
        calls = []
        seq = iter(generated)

        def fake_generate(question, failed_sql=None):
            calls.append(failed_sql)
            return next(seq)

        monkeypatch.setattr(nl_query, "_generate_sql", fake_generate)
        session = _Session(db_session, fail)

        def _db():
            yield session

        app.dependency_overrides[get_db] = _db
        app.dependency_overrides[get_current_admin] = lambda: admin
        client = TestClient(app)
        return client, calls, session

    yield build
    app.dependency_overrides.clear()


def _post(client):
    return client.post("/reports/query", json={"question": "Member contact list"})


def _count(db_session):
    row = db_session.execute(select(NlQueryUsage)).scalars().first()
    return row.count if row else 0


def _norm(sql):
    return nl_query._validate_sql(sql)


def test_3065_retries_once_and_succeeds(harness, db_session):
    client, calls, session = harness([BAD, GOOD], {_norm(BAD): _mysql_error(3065)})
    r = _post(client)
    assert r.status_code == 200
    assert r.json()["sql"] == _norm(GOOD)
    # Second generation received the first (validated) statement, nothing else.
    assert calls == [None, _norm(BAD)]
    assert session.executed == [_norm(BAD), _norm(GOOD)]


def test_retry_uses_one_quota_unit_and_one_audit_row(harness, db_session):
    client, _, _ = harness([BAD, GOOD], {_norm(BAD): _mysql_error(3065)})
    assert _post(client).status_code == 200
    assert _count(db_session) == 1
    logs = db_session.execute(select(AuditLog).where(AuditLog.action == "nl_query")).scalars().all()
    assert len(logs) == 1
    assert logs[0].details["sql"] == _norm(GOOD)


def test_second_3065_is_400_with_existing_text_and_no_quota(harness, db_session):
    client, calls, session = harness(
        [BAD, BAD], {_norm(BAD): _mysql_error(3065)})
    r = _post(client)
    assert r.status_code == 400
    assert r.json()["detail"].startswith("Query execution failed: ")
    assert len(calls) == 2            # exactly one retry, no loop
    assert len(session.executed) == 2
    assert _count(db_session) == 0    # failures never count against the limit


def test_other_mysql_error_is_not_retried(harness):
    client, calls, session = harness([BAD], {_norm(BAD): _mysql_error(1054)})
    r = _post(client)
    assert r.status_code == 400
    assert r.json()["detail"].startswith("Query execution failed: ")
    assert len(calls) == 1
    assert len(session.executed) == 1


def test_non_driver_exception_is_not_retried(harness):
    client, calls, _ = harness([BAD], {_norm(BAD): RuntimeError("boom")})
    assert _post(client).status_code == 400
    assert len(calls) == 1


@pytest.mark.parametrize("evil", [
    "SELECT password_hash FROM users",
    "SELECT * FROM users",
    "SELECT u.invite_token FROM users u",
    "DELETE FROM users",
    "SELECT email FROM audit_logs",
])
def test_retried_sql_is_validated_and_never_executed(harness, evil):
    client, calls, session = harness([BAD, evil], {_norm(BAD): _mysql_error(3065)})
    r = _post(client)
    assert r.status_code == 400
    assert not r.json()["detail"].startswith("Query execution failed")
    assert len(calls) == 2
    assert session.executed == [_norm(BAD)]   # the retried statement never reached execute


def test_retried_sql_limit_is_still_capped(harness):
    over = GOOD.replace("LIMIT 500", "LIMIT 100000")
    client, _, session = harness([BAD, over], {_norm(BAD): _mysql_error(3065)})
    assert _post(client).status_code == 200
    assert session.executed[-1].endswith("LIMIT 500")


def test_retry_generation_failure_maps_to_502(harness, monkeypatch):
    client, _, _ = harness([BAD], {_norm(BAD): _mysql_error(3065)})
    # Second call to the (patched) generator raises StopIteration -> generic 502 path.
    r = _post(client)
    assert r.status_code == 502
    assert r.json()["detail"].startswith("Failed to generate query")


class TestPrompt:
    def test_distinct_rule_and_existing_guards_present(self):
        ctx = nl_query.SCHEMA_CONTEXT
        assert "SELECT DISTINCT" in ctx and "ORDER BY" in ctx
        assert "Never use SELECT * or table.*" in ctx

    def test_retry_prompt_carries_fixed_text_and_no_db_error(self, monkeypatch):
        captured = {}

        class _Msgs:
            def create(self, **kw):
                captured.update(kw)

                class M:
                    content = [type("T", (), {"text": "SELECT 1"})()]
                return M()

        class _Client:
            def __init__(self, **kw):
                self.messages = _Msgs()

        monkeypatch.setattr(nl_query.anthropic, "Anthropic", _Client)
        monkeypatch.setattr(nl_query.settings, "anthropic_api_key", "k")
        nl_query._generate_sql("q", failed_sql=_norm(BAD))
        body = captured["messages"][0]["content"]
        assert _norm(BAD) in body and "rejected by MySQL" in body
        assert "driver message" not in body
        captured.clear()
        nl_query._generate_sql("q")
        assert "rejected by MySQL" not in captured["messages"][0]["content"]


# --- EP-0054: the retry must leave a marker that survives an unconfigured logger ---

def _nl_records(caplog):
    return [r for r in caplog.records if r.name == nl_query.logger.name]


def test_retry_success_logs_start_and_outcome_at_warning(harness, caplog):
    client, _, _ = harness([BAD, GOOD], {_norm(BAD): _mysql_error(3065)})
    with caplog.at_level("WARNING", logger=nl_query.logger.name):
        assert _post(client).status_code == 200
    recs = _nl_records(caplog)
    assert [r.levelname for r in recs] == ["WARNING", "WARNING"]
    msgs = [r.getMessage() for r in recs]
    assert "regenerating once" in msgs[0] and "3065" in msgs[0]
    assert "3065 retry succeeded" in msgs[1]


def test_retry_failure_logs_outcome(harness, caplog):
    client, _, _ = harness([BAD, BAD], {_norm(BAD): _mysql_error(3065)})
    with caplog.at_level("WARNING", logger=nl_query.logger.name):
        assert _post(client).status_code == 400
    msgs = [r.getMessage() for r in _nl_records(caplog)]
    assert len(msgs) == 2
    assert "regenerating once" in msgs[0]
    assert "3065 retry failed at execution" in msgs[1]


def test_retry_log_lines_carry_no_sql_question_or_driver_text(harness, caplog):
    client, _, _ = harness([BAD, BAD], {_norm(BAD): _mysql_error(3065)})
    with caplog.at_level("DEBUG"):
        _post(client)
    text_out = "\n".join(r.getMessage() + str(r.args) for r in _nl_records(caplog))
    for forbidden in ("SELECT", "DISTINCT", "Member contact list", "driver message"):
        assert forbidden not in text_out


def test_no_marker_without_a_3065(harness, caplog):
    client, _, _ = harness([GOOD], {})
    with caplog.at_level("DEBUG"):
        assert _post(client).status_code == 200
    assert _nl_records(caplog) == []


def test_non_3065_error_logs_no_marker(harness, caplog):
    client, _, _ = harness([BAD], {_norm(BAD): _mysql_error(1064)})
    with caplog.at_level("DEBUG"):
        assert _post(client).status_code == 400
    assert _nl_records(caplog) == []
