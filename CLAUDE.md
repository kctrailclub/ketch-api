# ketch-api — KCTC backend

FastAPI + SQLAlchemy + MySQL, Python 3.12. Backend for the Ken-Caryl Trail Club volunteer-management app. Project operating model, roles, registers, and standards live in the sibling docs repo `kctrailclub/kctc-docs` (locally `../`); read its `CLAUDE.md` first.

## Branches and deploys
- `main` → production (Railway service `web-production-c3b2.up.railway.app`, serves `kctrailclub.org`). **Champion-only.** Never push `main`; open a PR from a branch (`ep-NNNN/<slug>`, `sec-NNNN/<slug>`, `fix/<slug>`).
- `staging` → staging (Railway `web-production-b6591.up.railway.app`, separate MySQL copy of prod). Weekday work lands here (DEC-0015). Sync with `git checkout staging && git merge main`; keep both sides on conflict. Never `git push origin staging:main`.
- Release closure requires Tester Deploy Sign-off; see `kctc-docs/testing/Deploy_Sign-off_Checklist.md`.

## Run and test
```bash
pip install -r requirements-dev.txt
python -m pytest -q            # tests/ — conftest sets DATABASE_URL=sqlite in-memory, SECRET_KEY, ENVIRONMENT=staging
uvicorn app.main:app --reload  # needs .env (never commit it)
```
Add a test for every router change. Tests must not need MySQL.

## Layout
`app/main.py` (app, CORS, router registration — new routers must be registered here), `app/core/` (config, security, database, audit, email), `app/routers/` (one file per resource), `app/models/models.py` (all ORM models). Migrations are hand-written SQL run against Railway MySQL before deploy; document them in the release notes.

## Conventions and traps
- Every mutating endpoint calls `log_action()` from `app/core/audit.py`; `action` strings are the canonical vocabulary (e.g. `approved`).
- `is_admin` is always loaded from DB, never trusted from the JWT.
- Refresh token is an opaque rotating value in an HttpOnly cookie (ADR-0001); access token is short-lived JWT.
- Swagger / ReDoc are disabled when `ENVIRONMENT=production`.
- Email: `_send()` tries ZeptoMail → Resend → SMTP. Railway blocks SMTP.
- `nl_query.py` has a hand-maintained `SCHEMA_CONTEXT` that drifts from the ORM (TD-0007); `FORBIDDEN_PATTERNS` + `ALLOWED_TABLES` are security-relevant (SEC-0012).
- Hours: 24h per-entry and per-day caps; future service dates rejected; `credit_year` drives all reports.
- Security findings → `kctc-docs/security/Security_Recommendations_Register.md`; tech debt → `kctc-docs/architecture/Technical_Debt_Register.md`. Don't fix in passing; file first.
