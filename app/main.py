import logging
import os
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

from app.core.config import settings
from app.core.database import SessionLocal
from app.routers import audit, auth, households, hours, nl_query, notifications, projects, push, resources, strava, users, registrations
from app.routers import settings as settings_router

log = logging.getLogger(__name__)


def _run_daily_sync():
    """APScheduler job: sync all connected members' trail completions."""
    from app.routers.strava import _sync_all_members
    db = SessionLocal()
    try:
        _sync_all_members(db)
    except Exception as exc:
        log.error("Daily Trails Challenge sync failed: %s", exc)
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        _run_daily_sync,
        CronTrigger(hour=3, minute=0, timezone="UTC"),
        id="daily_trails_sync",
        replace_existing=True,
    )
    scheduler.start()
    log.info("APScheduler started — daily Trails Challenge sync at 03:00 UTC")
    yield
    scheduler.shutdown(wait=False)
    log.info("APScheduler stopped")


class BodySizeLimitMiddleware:
    """Reject requests whose declared Content-Length exceeds ``max_bytes`` with 413 (SEC-0026).

    Pure ASGI (not ``BaseHTTPMiddleware``) so the body is never buffered: the check
    reads only the request headers and runs before routing, so FastAPI never parses a
    multipart/form body for an oversized request (the parse happens before auth
    dependencies run).

    Known limitation: requests sent with ``Transfer-Encoding: chunked`` carry no
    Content-Length and are NOT bounded by this cap. That path is addressed by the
    starlette / python-multipart upgrade (Dependabot group PR), not by this middleware.
    """

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            for name, value in scope["headers"]:
                if name.lower() == b"content-length":
                    try:
                        length = int(value)
                    except ValueError:
                        break  # malformed value: leave it to the server / framework
                    if length > self.max_bytes:
                        response = JSONResponse(
                            {"detail": "Request body too large"},
                            status_code=413,
                            headers={"Connection": "close"},
                        )
                        await response(scope, receive, send)
                        return
                    break
        await self.app(scope, receive, send)


# Swagger UI enabled on staging only; disabled in production for security.
# To re-enable: set ENVIRONMENT=staging in Railway env vars.
_enable_docs = settings.environment != "production"

app = FastAPI(
    title=settings.app_name,
    description="Volunteer hours tracking API for KCTC",
    version="1.0.0",
    docs_url="/docs" if _enable_docs else None,
    redoc_url="/redoc" if _enable_docs else None,
    lifespan=lifespan,
)

# Body-size cap (SEC-0026). Added BEFORE CORSMiddleware so it sits inside it: the 413
# still carries CORS headers and the browser can read it instead of a CORS error.
app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)

# CORS -- origins controlled by environment variables.
# Set EXTRA_CORS_ORIGINS as comma-separated for local dev.
# Set CORS_ORIGIN_REGEX for LAN/mobile testing.
_extra_origins = [o.strip() for o in os.getenv("EXTRA_CORS_ORIGINS", "").split(",") if o.strip()]
_allowed_origins = [settings.frontend_url] + _extra_origins

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_origin_regex=os.getenv("CORS_ORIGIN_REGEX") or None,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(households.router)
app.include_router(hours.router)
app.include_router(projects.router)
app.include_router(notifications.router)
app.include_router(settings_router.router)
app.include_router(registrations.router)
app.include_router(nl_query.router)
app.include_router(audit.router)
app.include_router(push.router)
app.include_router(resources.router)
app.include_router(strava.router)


@app.get("/health")
def health():
    # `sha` is the commit Railway built from (RAILWAY_GIT_COMMIT_SHA). The deploy-verify
    # GitHub Actions workflow compares it to origin/main — Deploy Sign-off Checks 1 + 2.
    return {
        "status": "ok",
        "sha": os.getenv("RAILWAY_GIT_COMMIT_SHA", "unknown"),
        "environment": settings.environment,
    }
