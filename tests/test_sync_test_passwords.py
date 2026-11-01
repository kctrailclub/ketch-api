"""Guards on the staging test data and password step of scripts/sync_prod_to_staging.sh (EP-0045).

mysql and mysqldump are replaced by stubs on PATH, so nothing connects to a database.
"""
import os
import sqlite3
import stat
import subprocess
from pathlib import Path

import pytest
import sqlglot

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync_prod_to_staging.sh"
HASHES = ["$2b$12$" + c * 53 for c in "abc"]
EMAILS = ["a+t@example.test", "b+t@example.test", "c+t@example.test"]
ROLES = ["admin", "family", "solo"]

STUB = r"""#!/usr/bin/env bash
side=other
for arg in "$@"; do
  case "$arg" in
    *prod.cnf) side=prod ;;
    *staging.cnf) side=staging ;;
  esac
done
input="$*"
if [ ! -t 0 ]; then input="$input $(cat)"; fi
input="$(printf '%s' "$input" | tr '\n' ' ')"
printf '%s|%s|%s\n' "$(basename "$0")" "$side" "$input" >> "$STUB_LOG"
case "$input" in
  *@@hostname*) echo "$side-host:3306" ;;
  *"SELECT password_hash"*) echo "${STUB_PROD_HASH:-prodhash}" ;;
  *"SELECT COUNT"*) echo "${STUB_COUNT:-1}" ;;
  *) echo 1 ;;
esac
exit "${STUB_EXIT:-0}"
"""


@pytest.fixture
def env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("mysql", "mysqldump"):
        stub = bin_dir / name
        stub.write_text(STUB)
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    for side, host in (("prod", "prod.example.test"), ("staging", "staging.example.test")):
        cnf = tmp_path / f"{side}.cnf"
        cnf.write_text(f"[client]\nhost={host}\nport=3306\nuser=u\npassword=p\n")
        cnf.chmod(0o600)
    return {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "STUB_LOG": str(tmp_path / "calls.log"),
        "TMP_ROOT": str(tmp_path),
    }


def write_file(tmp_path, target="staging.example.test:3306", accounts=None, mode=0o600, extra_lines=None):
    accounts = accounts if accounts is not None else list(zip(ROLES, EMAILS, HASHES))
    lines = [
        "# comment",
        f"target {target}",
        "project Fake Project",
        "household admin HH-A Fake Admin Home",
        "household solo HH-S Fake Solo Home",
    ]
    lines += [f"account {r} {e} {h} Last First Name" for r, e, h in accounts]
    lines += extra_lines or []
    path = tmp_path / "passwords"
    path.write_text("\n".join(lines) + "\n")
    path.chmod(mode)
    return path


def run(env, *extra, passwords=None):
    root = Path(env["TMP_ROOT"])
    args = [
        "bash", str(SCRIPT),
        "--prod-cnf", str(root / "prod.cnf"), "--prod-db", "railway",
        "--staging-cnf", str(root / "staging.cnf"), "--staging-db", "railway",
        "--workdir", str(root / "work"), "--yes",
    ]
    if passwords is not None:
        args += ["--test-passwords-file", str(passwords)]
    run_env = {k: v for k, v in env.items() if k != "KCTC_STAGING_TEST_PASSWORDS_FILE"}
    return subprocess.run(
        [*args, *extra], env=run_env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30, check=False,
    )


def calls(env):
    path = Path(env["STUB_LOG"])
    return path.read_text().splitlines() if path.exists() else []


def writes(env):
    return [c for c in calls(env) if "UPDATE" in c or "DROP" in c or c.startswith("mysqldump")]


def assert_no_values_printed(result):
    output = result.stdout + result.stderr
    for value in [*EMAILS, *HASHES]:
        assert value not in output


def test_missing_file_fails_before_touching_anything(env):
    result = run(env)
    assert result.returncode == 1
    assert "test-passwords-file" in result.stderr
    assert calls(env) == []


@pytest.mark.parametrize("build, code", [
    (lambda p: p / "absent", 1),
    (lambda p: write_file(p, mode=0o644), 1),
    (lambda p: write_file(p, accounts=list(zip(ROLES, EMAILS, HASHES))[:2]), 1),
    (lambda p: write_file(p, accounts=[("admin", "x';DROP TABLE users;--@example.test", HASHES[0])] + list(zip(ROLES, EMAILS, HASHES))[1:]), 1),
    (lambda p: write_file(p, accounts=[("admin", EMAILS[0], "plaintext")] + list(zip(ROLES, EMAILS, HASHES))[1:]), 1),
    (lambda p: write_file(p, accounts=[("admin", EMAILS[0], HASHES[0]), ("admin", EMAILS[1], HASHES[1]), ("solo", EMAILS[2], HASHES[2])]), 1),
    (lambda p: write_file(p, extra_lines=["project Second Project"]), 1),
    (lambda p: write_file(p, extra_lines=["household solo HH-X Fake' OR 1=1"]), 1),
    (lambda p: write_file(p, target="prod.example.test:3306"), 3),
    (lambda p: write_file(p, target="other.example.test:3306"), 3),
])
def test_bad_input_is_refused_before_any_write(env, build, code):
    result = run(env, passwords=build(Path(env["TMP_ROOT"])))
    assert result.returncode == code
    assert writes(env) == []
    assert_no_values_printed(result)


def test_file_inside_a_git_work_tree_is_refused(env):
    root = Path(env["TMP_ROOT"])
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    result = run(env, passwords=write_file(root))
    assert result.returncode == 1
    assert writes(env) == []


def test_hash_equal_to_production_is_refused(env):
    result = run({**env, "STUB_PROD_HASH": HASHES[0]}, "--apply-test-passwords-only", passwords=write_file(Path(env["TMP_ROOT"])))
    assert result.returncode == 1
    assert writes(env) == []
    assert_no_values_printed(result)


def test_accounts_need_not_exist_on_production(env):
    result = run({**env, "STUB_PROD_HASH": ""}, "--apply-test-passwords-only", passwords=write_file(Path(env["TMP_ROOT"])))
    assert result.returncode == 0, result.stderr


def applied_sql(env):
    updates = [c for c in calls(env) if "UPDATE users" in c]
    assert len(updates) == 1
    return updates[0]


def test_missing_accounts_households_and_project_are_created_idempotently(env):
    result = run(env, "--apply-test-passwords-only", passwords=write_file(Path(env["TMP_ROOT"])))
    assert result.returncode == 0, result.stderr
    sql = applied_sql(env)
    for email in EMAILS:
        assert f"WHERE NOT EXISTS (SELECT 1 FROM users WHERE email='{email}')" in sql
    for code in ("HH-A", "HH-S"):
        assert f"WHERE NOT EXISTS (SELECT 1 FROM households WHERE household_code='{code}')" in sql
    assert "WHERE NOT EXISTS (SELECT 1 FROM projects WHERE name='Fake Project')" in sql
    assert sql.index("INSERT INTO households") < sql.index("INSERT INTO users")
    for email, hashed in zip(EMAILS, HASHES):
        assert f"'{email}', '', '{hashed}'" in sql


def test_flags_are_set_on_staging_whatever_production_holds(env):
    result = run(env, "--apply-test-passwords-only", passwords=write_file(Path(env["TMP_ROOT"])))
    assert result.returncode == 0, result.stderr
    sql = " ".join(applied_sql(env).split())
    for email, hashed, is_admin in zip(EMAILS, HASHES, (1, 0, 0)):
        assert f"UPDATE users SET password_hash='{hashed}', is_admin={is_admin}, is_active=1, is_tester=1," in sql
        assert f"WHERE email='{email}';" in sql


def test_applied_sql_parses_as_mysql(env):
    result = run(env, "--apply-test-passwords-only", passwords=write_file(Path(env["TMP_ROOT"])))
    assert result.returncode == 0, result.stderr
    sql = applied_sql(env)
    parsed = sqlglot.parse(sql[sql.index("USE `railway`"):], read="mysql", error_level=sqlglot.ErrorLevel.RAISE)
    assert len([st for st in parsed if st is not None]) > 10


def sqlite_with_admin_only():
    """A restored staging: the admin row came from production with its own hash and odd flags; nothing else exists."""
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE households (household_id INTEGER PRIMARY KEY, household_code TEXT UNIQUE, name TEXT, address TEXT,
            primary_user_id INTEGER, created TEXT, updated TEXT);
        CREATE TABLE projects (project_id INTEGER PRIMARY KEY, name TEXT, project_type TEXT, member_credit_pct INTEGER,
            admin_only INTEGER, created TEXT, updated TEXT);
        CREATE TABLE users (user_id INTEGER PRIMARY KEY, firstname TEXT, lastname TEXT, email TEXT UNIQUE, phone TEXT,
            password_hash TEXT, household_id INTEGER, is_admin INTEGER, is_active INTEGER, is_tester INTEGER, youth INTEGER,
            created TEXT, updated TEXT);
    """)
    db.execute(
        "INSERT INTO users (firstname, lastname, email, phone, password_hash, is_admin, is_active, is_tester, youth)"
        " VALUES ('X', 'Y', ?, '', 'prodhash', 0, 0, 0, 0)",
        (EMAILS[0],),
    )
    return db


def test_applied_sql_converges_from_a_restored_staging_and_is_idempotent(env):
    """Behaviour on SQLite, not MySQL: the parse test above covers the dialect."""
    result = run(env, "--apply-test-passwords-only", passwords=write_file(Path(env["TMP_ROOT"])))
    assert result.returncode == 0, result.stderr
    sql = applied_sql(env)
    sql = sql[sql.index("START TRANSACTION;") + len("START TRANSACTION;"):].replace(" FROM DUAL", "").replace("NOW()", "'now'")
    sql = sql.rsplit("COMMIT;", 1)[0]
    db = sqlite_with_admin_only()
    for _ in range(2):
        db.executescript(sql)
        users = {r[0]: r[1:] for r in db.execute("SELECT email, password_hash, is_admin, is_active, is_tester FROM users")}
        assert users == {
            EMAILS[0]: (HASHES[0], 1, 1, 1),
            EMAILS[1]: (HASHES[1], 0, 1, 1),
            EMAILS[2]: (HASHES[2], 0, 1, 1),
        }
        assert db.execute("SELECT COUNT(*) FROM projects").fetchone() == (1,)
        homes = dict(db.execute(
            "SELECT u.email, h.household_code FROM users u JOIN households h ON h.household_id = u.household_id"
        ))
        assert homes == {EMAILS[0]: "HH-A", EMAILS[1]: "HH-A", EMAILS[2]: "HH-S"}
        primaries = dict(db.execute(
            "SELECT h.household_code, u.email FROM households h JOIN users u ON u.user_id = h.primary_user_id"
        ))
        assert primaries == {"HH-A": EMAILS[0], "HH-S": EMAILS[2]}


def test_apply_only_writes_to_staging_only_and_prints_nothing(env):
    result = run(env, "--apply-test-passwords-only", passwords=write_file(Path(env["TMP_ROOT"])))
    assert result.returncode == 0, result.stderr
    assert_no_values_printed(result)
    assert applied_sql(env).startswith("mysql|staging|")
    assert not any(c.startswith("mysqldump") or "DROP" in c for c in calls(env))
    for call in calls(env):
        if call.startswith("mysql|prod|"):
            assert "UPDATE" not in call and "INSERT" not in call


def test_writes_are_a_single_transaction(env):
    run(env, "--apply-test-passwords-only", passwords=write_file(Path(env["TMP_ROOT"])))
    sql = applied_sql(env)
    assert "START TRANSACTION;" in sql
    assert sql.rstrip().endswith("COMMIT;")


def test_full_sync_applies_after_the_restore_and_the_parity_check(env):
    result = run(env, passwords=write_file(Path(env["TMP_ROOT"])))
    assert result.returncode == 0, result.stderr
    assert_no_values_printed(result)
    log = calls(env)
    restore = next(i for i, c in enumerate(log) if c.startswith("mysql|staging|") and "SET FOREIGN_KEY_CHECKS=0;" in c and "DROP" not in c)
    parity = max(i for i, c in enumerate(log) if "SELECT COUNT(*) FROM `users`" in c)
    update = next(i for i, c in enumerate(log) if "UPDATE users" in c)
    assert restore < parity < update


def test_dry_run_changes_nothing(env):
    result = run(env, "--apply-test-passwords-only", "--dry-run", passwords=write_file(Path(env["TMP_ROOT"])))
    assert result.returncode == 0, result.stderr
    assert writes(env) == []


def stub_verification(env, pattern, answer):
    """Make the stub answer one verification SELECT with a chosen count, on staging only."""
    bin_mysql = Path(env["PATH"].split(os.pathsep)[0]) / "mysql"
    bin_mysql.write_text(STUB.replace(
        'case "$input" in\n',
        f'case "$input" in\n  *UPDATE*) ;;\n  *"{pattern}"*) echo {answer} ;;\n',
    ))


@pytest.mark.parametrize("pattern, answer", [
    ("AND password_hash=", 0),   # the zero-row outcome: account missing, or hash not as expected
    ("FROM households WHERE household_code=", 0),
    ("FROM projects WHERE name=", 0),
])
@pytest.mark.parametrize("extra", [("--apply-test-passwords-only",), ()])
def test_zero_row_outcome_fails_with_exit_7(env, pattern, answer, extra):
    stub_verification(env, pattern, answer)
    result = run(env, *extra, passwords=write_file(Path(env["TMP_ROOT"])))
    assert result.returncode == 7, result.stdout + result.stderr
    assert_no_values_printed(result)


def test_failed_transaction_exits_7(env):
    bin_mysql = Path(env["PATH"].split(os.pathsep)[0]) / "mysql"
    bin_mysql.write_text(STUB.replace('exit "${STUB_EXIT:-0}"', 'case "$input" in *"UPDATE users"*) exit 1 ;; esac\nexit 0'))
    result = run(env, "--apply-test-passwords-only", passwords=write_file(Path(env["TMP_ROOT"])))
    assert result.returncode == 7
    assert_no_values_printed(result)


@pytest.mark.parametrize("extra", [("--apply-test-passwords-only",), ()])
def test_guard_refuses_a_non_staging_target_in_both_modes(env, extra):
    result = run(env, *extra, passwords=write_file(Path(env["TMP_ROOT"]), target="prod.example.test:3306"))
    assert result.returncode == 3
    assert writes(env) == []
    assert not any("INSERT" in c for c in calls(env))
