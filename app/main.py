import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.routers import auth, households, hours, nl_query, notifications, projects, users, registrations
from app.routers import settings as settings_router

app = FastAPI(
    title=settings.app_name,
    description="Volunteer hours tracking API for KCTC",
    version="1.0.0",
)

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


@app.get("/health")
def health():
    return {"status": "ok"}
