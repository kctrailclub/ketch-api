"""Guards on the staging-only test-password step of scripts/sync_prod_to_staging.sh (EP-0045).

mysql and mysqldump are replaced by stubs on PATH, so nothing connects to a database.
"""
import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync_prod_to_staging.sh"
HASHES = ["$2b$12$" + c * 53 for c in "abc"]
EMAILS = ["a+t@example.test", "b+t@example.test", "c+t@example.test"]

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


def write_file(tmp_path, target="staging.example.test:3306", accounts=None, mode=0o600):
    accounts = accounts if accounts is not None else list(zip(EMAILS, HASHES))
    lines = ["# comment", f"target {target}"] + [f"account {e} {h}" for e, h in accounts]
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
    (lambda p: write_file(p, accounts=list(zip(EMAILS, HASHES))[:2]), 1),
    (lambda p: write_file(p, accounts=[("x';DROP TABLE users;--@example.test", HASHES[0])] + list(zip(EMAILS, HASHES))[:2]), 1),
    (lambda p: write_file(p, accounts=[(EMAILS[0], "plaintext")] + list(zip(EMAILS, HASHES))[1:]), 1),
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


def test_account_missing_on_production_is_refused(env):
    result = run({**env, "STUB_COUNT": "0"}, "--apply-test-passwords-only", passwords=write_file(Path(env["TMP_ROOT"])))
    assert result.returncode == 1
    assert writes(env) == []


def test_apply_only_writes_to_staging_only_on_stdin_and_prints_nothing(env):
    result = run(env, "--apply-test-passwords-only", passwords=write_file(Path(env["TMP_ROOT"])))
    assert result.returncode == 0, result.stderr
    assert_no_values_printed(result)
    updates = [c for c in calls(env) if "UPDATE users" in c]
    assert len(updates) == 1
    assert updates[0].startswith("mysql|staging|")
    for email, hashed in zip(EMAILS, HASHES):
        assert f"password_hash='{hashed}' WHERE email='{email}'" in updates[0]
    assert not any(c.startswith("mysqldump") or "DROP" in c for c in calls(env))
    assert not any("UPDATE" in c for c in calls(env) if c.startswith("mysql|prod|"))


def test_full_sync_reapplies_after_the_restore(env):
    result = run(env, passwords=write_file(Path(env["TMP_ROOT"])))
    assert result.returncode == 0, result.stderr
    assert_no_values_printed(result)
    log = calls(env)
    restore = next(i for i, c in enumerate(log) if c.startswith("mysql|staging|") and "SET FOREIGN_KEY_CHECKS=0;" in c and "DROP" not in c)
    update = next(i for i, c in enumerate(log) if "UPDATE users" in c)
    assert update > restore


def test_dry_run_changes_nothing(env):
    result = run(env, "--apply-test-passwords-only", "--dry-run", passwords=write_file(Path(env["TMP_ROOT"])))
    assert result.returncode == 0, result.stderr
    assert writes(env) == []


def test_failed_verification_exits_7(env):
    # Pre-flight needs COUNT=1 on prod, so make the stub fail verification on staging only.
    bin_mysql = Path(env["PATH"].split(os.pathsep)[0]) / "mysql"
    bin_mysql.write_text(STUB.replace(
        '*"SELECT COUNT"*) echo "${STUB_COUNT:-1}" ;;',
        '*"AND password_hash="*) echo 0 ;;\n  *"SELECT COUNT"*) echo 1 ;;',
    ))
    result = run(env, "--apply-test-passwords-only", passwords=write_file(Path(env["TMP_ROOT"])))
    assert result.returncode == 7
    assert_no_values_printed(result)
